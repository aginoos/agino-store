# SPDX-License-Identifier: GPL-3.0-or-later
"""One build task: the state machine from `starting` to `done`, `failed` or `cancelled`.

starting -> installing_deps -> building -> packaging -> [testing ->] collecting -> done. Whatever
happens (a failing step, a dead VM, a guest agent timeout, a cancel, a bug) the task ends in exactly one
terminal state with the reason in result.json, the VM is gone and its run files are removed: nothing is
left in `building`.

Time: `limits.cpu_minutes` bounds boot + dependency install + all steps together (one deadline). Packaging and
collecting have fixed extra allowances (PACKAGE_TIMEOUT, COLLECT_TIMEOUT), so a task can run at most
cpu_minutes + 25 minutes plus the boot wait. The optional testing phase (recipe v2 `test`) has its own allowance
(TEST_INSTALL_TIMEOUT plus TEST_TIMEOUT for each command).

result.json carries the failure class: ok, recipe (a step, test, packaging or source problem), infra (VM, guest
agent, resources, a cancel or a runner stop), timeout (a time limit) or model (AI mode only).
"""

import hashlib
import os
import shutil
import stat
import threading
import time

from . import build_vm
from .build_guest import EXTRA_SCRIPTS, BuildStepError, GuestBuild, render_control
from .build_out import OutDir
from .build_task import PKG_NAME_RE
from .util import SandboxError, clean, log, now

GIB = 1 << 30
BOOT_TIMEOUT = 180
DEPS_TIMEOUT = 1800
PACKAGE_TIMEOUT = 600
TEST_INSTALL_TIMEOUT = 600
TEST_TIMEOUT = 120
COLLECT_TIMEOUT = 900
DISK_CHECK_SECONDS = 5
CANCEL_FILE = "cancel"
SOURCE_FILE = "source.tar"


class _Stopped(Exception):
    """The job was asked to end (cancel, runner shutdown)."""


class Deps:
    """Everything the job takes from the outside; tests replace these."""

    def __init__(self, vm_factory=None, clock=time.monotonic, sleep=time.sleep, free_bytes=None):
        self.vm_factory = vm_factory or build_vm.make_vm
        self.clock, self.sleep = clock, sleep
        self.free_bytes = free_bytes or (lambda path: shutil.disk_usage(path).free)


class BuildJob:
    def __init__(self, cfg, task, slot, deps=None):
        self.cfg, self.task, self.slot = cfg, task, slot
        self.bcfg = cfg.builder
        self.d = deps or Deps()
        self.out = OutDir(self.bcfg.out, task.id, self.bcfg.max_log_mib << 20)
        self.cancel_path = os.path.join(self.bcfg.queue, task.id, CANCEL_FILE)
        self.stop_event = threading.Event()
        self.stop_state, self.stop_reason = "cancelled", "cancelled"
        self.finished = threading.Event()
        self.state, self.started = "starting", now()
        self.vm = self.guest = self._source = None
        self.steps, self.artifacts = [], []
        self.deadline = self.phase_end = 0.0
        self.phase_error, self.phase_class = "", "timeout"
        self.klass = "infra"  # the failure class if the run ends in an error; set where the error is classified
        self.files = None  # AI tasks: the delivered files (a list); None for builds, which have artifacts
        self._last_disk = 0.0

    # ---- control from the daemon thread ----

    def cancel_requested(self):
        return os.path.lexists(self.cancel_path)

    def request_stop(self, state, reason):
        """Ask the job to end as `state`; kills the qemu we started so a blocked guest call returns."""
        if not self.stop_event.is_set():
            self.stop_state, self.stop_reason = state, reason
            self.stop_event.set()
        vm = self.vm
        if vm is not None:
            vm.kill()

    # ---- the run ----

    def run(self):
        """Run to a terminal state; returns it. Never raises."""
        state, error = "failed", ""
        try:
            self.out.create()
            self._set("starting")
            self._execute()
            state = "done"
        except Exception as e:  # noqa: BLE001 - every outcome must end in a terminal state
            if self.stop_event.is_set():
                state, error = self.stop_state, self.stop_reason
            else:
                error = self._describe(e)
                self.klass = e.klass if isinstance(e, BuildStepError) else "infra"
        finally:
            self._teardown()
        try:
            self._finish(state, error)
        finally:
            self.finished.set()
        return state

    def _describe(self, exc):
        if isinstance(exc, BuildStepError):
            return clean(str(exc), 300)
        if isinstance(exc, SandboxError):
            return "sandbox: " + clean(str(exc), 280)
        log("build %s: unexpected %s: %s" % (self.task.id, type(exc).__name__, exc))
        return "internal error: %s" % type(exc).__name__

    def _teardown(self):
        for closer in (lambda: self._source and self._source.close(), lambda: self.vm and self.vm.stop()):
            try:
                closer()
            except Exception as e:  # noqa: BLE001 - clean up the rest whatever one part did
                log("build %s: cleanup failed: %s" % (self.task.id, e))
        self.vm = None

    def _finish(self, state, error):
        ok = state == "done"
        try:
            if self.out.sink is not None:
                self.out.sink.note("== %s%s ==" % (state, ": " + error if error else ""))
            self.out.write_result(ok, error, self.steps, self.artifacts, "ok" if ok else self.klass, self.files)
            self.out.write_status(state, self.started, len(self.steps), len(self.task.steps), error)
        except OSError as e:
            log("build %s: cannot write the result: %s" % (self.task.id, e.strerror))
        finally:
            self.out.close()
        self.state = state

    def _set(self, state, step=0):
        """Enter a state: write status.json and set the allowance of the phase."""
        self.state = state
        clock = self.d.clock()
        if state == "packaging":
            self.phase_end, self.phase_error = clock + PACKAGE_TIMEOUT, "packaging timed out"
        elif state == "testing":
            allowance = TEST_INSTALL_TIMEOUT + (TEST_TIMEOUT + 70) * len(self.task.tests)
            self.phase_end, self.phase_error = clock + allowance, "testing timed out"
        elif state == "collecting":
            self.phase_end, self.phase_error = clock + COLLECT_TIMEOUT, "collecting the result timed out"
        else:
            self.phase_end = self.deadline
            self.phase_error = "time limit of %d minutes reached" % self.task.limits.cpu_minutes
        self.out.write_status(state, self.started, step, len(self.task.steps))

    def _check(self):
        """Called while waiting for the guest: stop request, dead VM, time and disk limits."""
        if self.stop_event.is_set():
            raise _Stopped()
        vm, clock = self.vm, self.d.clock()
        if vm is not None and vm.exited():
            raise BuildStepError("the build VM exited unexpectedly: %s" % clean(vm.log_tail(), 200), klass="infra")
        if clock > self.phase_end:
            raise BuildStepError(self.phase_error, klass=self.phase_class)
        if vm is not None and clock - self._last_disk >= DISK_CHECK_SECONDS:
            self._last_disk = clock
            if vm.disk_bytes() > self.task.limits.disk_gib * GIB:
                raise BuildStepError("disk limit of %d GiB reached" % self.task.limits.disk_gib, klass="infra")

    def _remaining(self):
        left = int(self.deadline - self.d.clock())
        if left < 1:
            raise BuildStepError(self.phase_error, klass=self.phase_class)
        return left

    # ---- phases ----

    def _execute(self):
        task, bcfg = self.task, self.bcfg
        self._check_free_space()
        self._source = self._open_source()
        self.deadline = self.d.clock() + task.limits.cpu_minutes * 60
        self._set("starting")
        self.vm = self.d.vm_factory(self.cfg, self.slot, task.limits)
        self.vm.boot()
        self._check()
        self.vm.qga.wait_ready(BOOT_TIMEOUT)
        self.guest = GuestBuild(self.vm.qga, self.out.sink, self._check, self.d.clock, self.d.sleep)
        self.guest.prepare(render_control(task), bcfg.apt_source, EXTRA_SCRIPTS if task.files or task.tests else ())
        self.guest.upload_source(self._source, task.source_sha256)
        self.guest.extract_source()
        if task.files:
            self.out.sink.note("== writing %d recipe file(s) ==" % len(task.files))
            self.guest.place_files(task.files)
        self._set("installing_deps")
        self._install_deps()
        self._set("building")
        self._run_steps()
        self._set("packaging")
        self._package()
        if task.tests:
            self._set("testing")
            self._run_tests()
        self._set("collecting")
        self._collect()

    def _check_free_space(self):
        os.makedirs(self.cfg.run, mode=0o755, exist_ok=True)
        need = (self.task.limits.disk_gib + self.cfg.session.min_free_gib) * GIB
        if self.d.free_bytes(self.cfg.run) < need:
            raise BuildStepError("not enough free disk space on the build host", klass="infra")

    def _open_source(self):
        """queue/<id>/source.tar, opened without following links, size and sha256 verified."""
        task = self.task
        path = os.path.join(self.bcfg.queue, task.id, SOURCE_FILE)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as e:
            raise BuildStepError("cannot open source.tar: %s" % e.strerror, klass="infra") from None
        f = os.fdopen(fd, "rb")
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise BuildStepError("source.tar is not a regular file", klass="infra")
            if info.st_size > self.bcfg.max_source_mib << 20:
                raise BuildStepError("source.tar is larger than %d MiB" % self.bcfg.max_source_mib, klass="infra")
            digest = hashlib.sha256()
            while True:
                block = f.read(1 << 20)
                if not block:
                    break
                digest.update(block)
            if info.st_size != task.source_size or digest.hexdigest() != task.source_sha256:
                raise BuildStepError("source.tar does not match the size and sha256 in task.json", klass="infra")
            f.seek(0)
            return f
        except BaseException:
            f.close()
            raise

    def _install_deps(self):
        depends = self.task.build_depends
        if not depends:
            return
        for name in depends:  # the server validated these; the guest runs apt-get on them, so check again
            if not PKG_NAME_RE.match(name):
                raise BuildStepError("invalid build dependency name")
        label, res = self.guest.install_deps(depends, min(DEPS_TIMEOUT, self._remaining()))
        if res is not None:
            klass = "timeout" if res.timed_out or res.exitcode == 124 else "infra" if label == "update" else "recipe"
            raise BuildStepError("apt-get %s failed (exit %d)" % (label, res.exitcode), klass=klass)

    def _run_steps(self):
        total = len(self.task.steps)
        for n, cmd in enumerate(self.task.steps, 1):
            self._set("building", n)
            first_line = cmd.splitlines()[0][:200] if cmd.strip() else ""
            self.out.sink.note("== step %d/%d: %s ==" % (n, total, first_line))
            started = self.d.clock()
            res = self.guest.run_step(n, cmd, self._remaining(), self.task.agino_env)
            timed_out = res.timed_out or res.exitcode == 124
            self.steps.append({"n": n, "cmd": clean(cmd, 512), "exit": 124 if timed_out else res.exitcode,
                               "seconds": round(self.d.clock() - started, 1)})
            if timed_out:
                raise BuildStepError("step %d timed out" % n, klass="timeout")
            if not res.ok:
                raise BuildStepError("step %d failed with exit code %d" % (n, res.exitcode))

    def _run_tests(self):
        """Recipe v2 `test`: install the deb, then run every command under xvfb; any failure fails the build."""
        task = self.task
        self.out.sink.note("== installing the package for the tests ==")
        label, res = self.guest.install_package(TEST_INSTALL_TIMEOUT)
        if res is not None:
            if res.timed_out or res.exitcode == 124:
                raise BuildStepError("apt-get %s timed out" % label, klass="timeout")
            raise BuildStepError("apt-get %s failed (exit %d)" % (label, res.exitcode),
                                 klass="infra" if label == "update" else "recipe")
        total = len(task.tests)
        for n, cmd in enumerate(task.tests, 1):
            self.out.sink.note("== test %d/%d: %s ==" % (n, total, clean(cmd.splitlines()[0], 200)))
            res = self.guest.run_test(n, cmd, TEST_TIMEOUT, task.agino_env)
            if not res.ok:
                self.out.sink.note("== test %d: exit code %d%s ==" % (n, res.exitcode, ", timed out" if res.timed_out
                                                                     or res.exitcode == 124 else ""))
                raise BuildStepError("test %d failed" % n)

    def _package(self):
        self.out.sink.note("== packaging ==")
        res = self.guest.package(PACKAGE_TIMEOUT)
        if res.ok:
            return
        reasons = {20: "pkgroot is missing or not a directory",
                   21: "pkgroot contains a DEBIAN directory (maintainer scripts are not accepted)"}
        if res.timed_out or res.exitcode == 124:
            raise BuildStepError("packaging timed out", klass="timeout")
        raise BuildStepError(reasons.get(res.exitcode, "packaging failed (exit %d)" % res.exitcode))

    def _collect(self):
        size = self.guest.artifact_size()
        name = self.task.deb_name
        writer = self.out.artifact(name)
        try:
            self.guest.read_artifact(writer, size, self.bcfg.max_artifact_mib << 20)
            sha256, written = writer.commit()
        except BaseException:
            writer.abort()
            raise
        self.artifacts.append({"file": name, "sha256": sha256, "size": written})

