# SPDX-License-Identifier: GPL-3.0-or-later
"""One AI task (`mode: "ai"`): an agent CLI builds and packages a project inside a build VM (docs/sandbox.md 10.7).

starting -> preparing -> running -> collecting -> done | failed | cancelled. The VM is the isolation boundary: no
credentials in it, the network allows only the host's apt, model proxy and download proxy ports (the infrastructure
sets that up per tap, this code only picks the AI slot). The task ends with the white-listed files the agent left in
/work/out copied to out/<id>/ai/ and a result.json with `ok`, `files` and the failure `class`.

Failure classes: timeout (the agent hit `ai.minutes`), model (the CLI died while the model service answered with
401/402/429/5xx or the proxy was paused), recipe (it finished without a usable recipe.yaml and app.yaml), infra (VM,
guest agent, resources, a cancel).
"""

import hashlib
import os
import stat

from .build_ai_guest import MAX_TEXT_BYTES, AiGuest, BadDeliverable, deliverable_kind
from .build_ai_stream import AiStream
from .build_guest import BuildStepError
from .build_job import BOOT_TIMEOUT, COLLECT_TIMEOUT, BuildJob
from .util import clean

WORKSPACE_FILE = "workspace.tar"
PREPARE_TIMEOUT = 900  # boot, guest agent, upload of the workspace
AI_KILL_GRACE = 10  # `timeout -k` in the guest
AI_BACKSTOP = 45  # extra host-side wait after the guest's own limit before the host gives up
REQUIRED = ("recipe.yaml", "app.yaml")


class AiJob(BuildJob):
    def __init__(self, cfg, task, slot, deps=None):
        super().__init__(cfg, task, slot, deps)
        self.files = []
        self.stream = None

    # ---- phases ----

    def _set(self, state, step=0):
        """Enter a state: write status.json and set the allowance of the phase."""
        self.state = state
        clock, minutes = self.d.clock(), self.task.ai.minutes
        if state == "running":
            self.phase_end = clock + minutes * 60 + AI_KILL_GRACE + AI_BACKSTOP
            self.phase_error, self.phase_class = "time limit of %d minutes reached" % minutes, "timeout"
        elif state == "collecting":
            self.phase_end = clock + COLLECT_TIMEOUT
            self.phase_error, self.phase_class = "collecting the result timed out", "timeout"
        else:
            self.phase_end = clock + PREPARE_TIMEOUT
            self.phase_error, self.phase_class = "starting the AI guest timed out", "infra"
        self.out.write_status(state, self.started, 0, 0)

    def _execute(self):
        task, bcfg = self.task, self.bcfg
        self._check_free_space()
        self._source, digest = self._open_workspace()
        self._set("starting")
        self.vm = self.d.vm_factory(self.cfg, self.slot, task.limits, cpus=bcfg.ai_limits.cpu)
        self.vm.boot()
        self._check()
        self.vm.qga.wait_ready(BOOT_TIMEOUT)
        self._set("preparing")
        self.stream = AiStream(self.out.sink)
        self.guest = AiGuest(self.vm.qga, self.stream, self.out.sink, self._check, self.d.clock, self.d.sleep)
        agent = task.ai.agent
        env = self._env(agent)
        self.guest.prepare_ai(bcfg.apt_source, env, agent, bcfg.ai_codex_toml)
        self.guest.upload_workspace(self._source, digest)
        self._set("running")
        exit_code, timed_out = self._run_agent(agent, env)
        self._set("collecting")
        self._collect()
        self._judge(agent, exit_code, timed_out)

    def _env(self, agent):
        bcfg = self.bcfg
        return list(bcfg.ai_env) + list(bcfg.ai_claude_env if agent == "claude" else bcfg.ai_codex_env)

    def _open_workspace(self):
        """queue/<id>/workspace.tar, opened without following links, size checked; returns (file, sha256)."""
        path = os.path.join(self.bcfg.queue, self.task.id, WORKSPACE_FILE)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as e:
            raise BuildStepError("cannot open workspace.tar: %s" % e.strerror, klass="infra") from None
        f = os.fdopen(fd, "rb")
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise BuildStepError("workspace.tar is not a regular file", klass="infra")
            if info.st_size > self.bcfg.max_source_mib << 20:
                raise BuildStepError("workspace.tar is larger than %d MiB" % self.bcfg.max_source_mib, klass="infra")
            digest = hashlib.sha256()
            while True:
                block = f.read(1 << 20)
                if not block:
                    break
                digest.update(block)
            if info.st_size == 0:
                raise BuildStepError("workspace.tar is empty", klass="infra")
            f.seek(0)
            return f, digest.hexdigest()
        except BaseException:
            f.close()
            raise

    def _run_agent(self, agent, env):
        """Run the CLI to its end; returns (exit code, timed out)."""
        minutes = self.task.ai.minutes
        snippet = self.bcfg.ai_claude_cmd if agent == "claude" else self.bcfg.ai_codex_cmd
        seconds = minutes * 60
        self.out.sink.note("== %s (%s), limit %d minutes ==" % (agent, self.task.ai.kind, minutes))
        started = self.d.clock()
        res = self.guest.run_agent(snippet, seconds, env)
        self.stream.finish()
        elapsed = self.d.clock() - started
        timed_out = res.timed_out or res.exitcode == 124 or (res.exitcode == 137 and elapsed >= seconds)
        self.steps.append({"n": 1, "cmd": agent, "exit": 124 if timed_out else res.exitcode,
                           "seconds": round(elapsed, 1)})
        self.out.sink.note("== %s exited with code %d%s ==" % (agent, res.exitcode, " (time limit)" if timed_out
                                                              else ""))
        return res.exitcode, timed_out

    def _collect(self):
        """Copy the white-listed files of /work/out to out/<id>/ai/ (best effort per file)."""
        chosen, deb_seen = [], False
        for name, size in sorted(self.guest.list_deliverables()):
            if deliverable_kind(name) == "deb":
                if deb_seen:
                    self.out.sink.note("ignored an extra deb: %s" % clean(name, 100))
                    continue
                deb_seen = True
            chosen.append((name, size))
        for name, size in chosen:
            limit = MAX_TEXT_BYTES if deliverable_kind(name) == "text" else self.bcfg.max_artifact_mib << 20
            if size == 0 or size > limit:
                self.out.sink.note("skipped %s: %d bytes (the limit is %d)" % (clean(name, 100), size, limit))
                continue
            writer = self.out.ai_artifact(name)
            try:
                self.guest.read_deliverable(name, size, writer)
                writer.commit()
            except BadDeliverable as e:
                writer.abort()
                self.out.sink.note("skipped %s: %s" % (clean(name, 100), e))
                continue
            except BaseException:
                writer.abort()
                raise
            self.files.append(name)
        self.out.sink.note("== delivered: %s ==" % (", ".join(self.files) or "nothing"))

    def _judge(self, agent, exit_code, timed_out):
        """Raise BuildStepError (with the failure class) unless the agent delivered a usable result."""
        if timed_out:
            raise BuildStepError("%s hit the time limit of %d minutes" % (agent, self.task.ai.minutes),
                                 klass="timeout")
        if exit_code != 0 and self.stream.model_failure:
            statuses = ",".join(str(s) for s in sorted(self.stream.retry_statuses))
            raise BuildStepError("the model service failed%s" % (" (status %s)" % statuses if statuses else ""),
                                 klass="model")
        missing = [name for name in REQUIRED if name not in self.files]
        if missing:
            raise BuildStepError("%s exited with code %d and left no %s" % (agent, exit_code, " and no ".join(missing)),
                                 klass="recipe")
