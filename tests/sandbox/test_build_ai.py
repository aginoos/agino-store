# SPDX-License-Identifier: GPL-3.0-or-later
"""The AI mode job (state machine, guest preparation, agent run, deliverables) with the fake VM and guest agent."""
import json
import os
import stat
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_helpers import DEB, TASK_ID, WORKSPACE, VmFactory, ai_task_doc, builder_cfg, write_ai_task  # noqa: E402
from test_build_job import GIB, JobCase  # noqa: E402

from agino_store_sandbox import build_ai as A  # noqa: E402
from agino_store_sandbox import build_ai_guest as AG  # noqa: E402
from agino_store_sandbox import build_guest as G  # noqa: E402
from agino_store_sandbox import build_job as J  # noqa: E402
from agino_store_sandbox import config  # noqa: E402
from agino_store_sandbox.build_task import read_task_file  # noqa: E402
from agino_store_sandbox.util import InfraError  # noqa: E402

DEBNAME = "demo-agino_1.0+agino1_amd64.deb"
DELIVERED = {"recipe.yaml": b"recipe_version: 1\n", "app.yaml": b"id: demo\n", "upstream.yaml": b"id: demo\n",
             "NOTES.md": b"# notes\n", DEBNAME: DEB}


def event(**fields):
    return (json.dumps(fields) + "\n").encode()


def assistant(text):
    return event(type="assistant", message={"content": [{"type": "text", "text": text}]})


def retry(status):
    return event(type="system", subtype="api_retry", attempt=2, max_retries=5, error_status=status, error="x")


GOOD_OUT = (event(type="system", subtype="init") + assistant("building") +
            event(type="result", subtype="success", is_error=False, num_turns=3, result="delivered"))


def deliver(files=None, out=GOOD_OUT, exit_code=0, **qga):
    """A configure() for the fake VM: the agent leaves `files` in /work/out and prints `out`."""
    def configure(vm):
        vm.qga.work_out = dict(DELIVERED if files is None else files)
        vm.qga.ai_out, vm.qga.ai_exit = out, exit_code
        for key, value in qga.items():
            setattr(vm.qga, key, value)
    return configure


class AiCase(JobCase):
    def make_ai(self, doc=None, workspace=WORKSPACE, configure=None, free_bytes=None, **builder):
        self.cfg = builder_cfg(self.tmp.name, **builder)
        write_ai_task(self.cfg, doc=doc, workspace=workspace)
        b = self.cfg.builder
        self.task = read_task_file(b.queue, TASK_ID, b.limits, b.ai_limits)
        self.factory = VmFactory(configure)

        def sleep(n):
            self.clock.sleep(n)
            if self.sleep_hook:
                self.sleep_hook()

        deps = J.Deps(self.factory, self.clock, sleep, free_bytes or (lambda path: 1 << 50))
        self.job = A.AiJob(self.cfg, self.task, b.ai_slot, deps)
        return self.job

    def ai_path(self, name):
        return self.out("ai/" + name)

    def out_reads(self):
        """The guest files read from /work/out (the log file of the agent is read as well)."""
        return [p for p in self.factory.vms[0].qga.read_paths if p.startswith(AG.OUT_DIR + "/")]

    def ai_files(self):
        try:
            return sorted(os.listdir(self.out("ai")))
        except FileNotFoundError:
            return []

    def assertFailed(self, error_part, klass):
        res = self.result()
        self.assertFalse(res["ok"])
        self.assertIn(error_part, res["error"])
        self.assertEqual(res["class"], klass)
        self.assertEqual(self.status()["state"], "failed")


class HappyPathTests(AiCase):
    def test_full_run(self):
        job = self.make_ai(configure=deliver())
        self.assertEqual(job.run(), "done")
        self.assertEqual(self.states, ["starting", "starting", "preparing", "running", "collecting", "done"])
        res = self.result()
        self.assertEqual((res["ok"], res["error"], res["class"]), (True, "", "ok"))
        self.assertEqual(sorted(res["files"]), sorted(DELIVERED))
        self.assertEqual(res["artifacts"], [])
        self.assertEqual([(s["n"], s["cmd"], s["exit"]) for s in res["steps"]], [(1, "claude", 0)])
        for name, data in DELIVERED.items():
            with open(self.ai_path(name), "rb") as f:
                self.assertEqual(f.read(), data, name)
        self.assertEqual(self.ai_files(), sorted(DELIVERED))
        vm = self.factory.vms[0]
        self.assertTrue(vm.stopped)
        self.assertTrue(job.finished.is_set())
        self.assertEqual(self.status()["state"], "done")

    def test_permissions(self):
        self.make_ai(configure=deliver()).run()
        ai = self.out("ai")
        self.assertEqual(stat.S_IMODE(os.stat(ai).st_mode), 0o750)
        for name in self.ai_files():
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(ai, name)).st_mode), 0o640, name)

    def test_the_vm_uses_the_ai_slot_cpus_and_limits(self):
        self.make_ai(configure=deliver()).run()
        vm = self.factory.vms[0]
        self.assertEqual(vm.slot, 1)
        self.assertEqual(vm.cpus, 8)
        self.assertEqual(vm.limits, config.BuildLimits(cpu_minutes=30, memory_gib=16, disk_gib=40))

    def test_a_configured_ai_slot_is_used(self):
        self.make_ai(configure=deliver(), ai_slot=3, ai_limits="cpu=2 memory_gib=4 disk_gib=10 minutes=60")
        self.assertEqual(self.job.slot, 3)
        self.job.run()
        self.assertEqual((self.factory.vms[0].slot, self.factory.vms[0].cpus), (3, 2))

    def test_the_task_can_lower_memory_and_disk(self):
        doc = ai_task_doc(limits={"memory_gib": 4, "disk_gib": 10})
        self.make_ai(doc, configure=deliver()).run()
        self.assertEqual(self.factory.vms[0].limits, config.BuildLimits(30, 4, 10))


class GuestPreparationTests(AiCase):
    def prepared(self, doc=None, **builder):
        self.make_ai(doc, configure=deliver(), **builder).run()
        return self.factory.vms[0].qga

    def test_workspace_is_uploaded_checked_and_unpacked_as_builder(self):
        qga = self.prepared()
        self.assertEqual(qga.files[AG.WORKSPACE_TAR], WORKSPACE)
        tar = next(a for kind, a in qga.execs if kind == "exec" and "/bin/tar" in a)
        self.assertEqual(tar[:4], [G.RUNUSER, "-u", "builder", "--"])
        self.assertEqual(tar[tar.index("-C") + 1], "/work")
        self.assertIn("--no-same-owner", tar)
        mkdir = [a for kind, a in qga.execs if kind == "exec" and "/bin/mkdir" in a]
        self.assertEqual(mkdir[0][-1], "/work/out")

    def test_scripts_apt_source_and_apt_configuration(self):
        qga = self.prepared(apt_source="[trusted=yes] http://192.0.2.7:8080/ milkyway main")
        self.assertEqual(qga.files[AG.AI_RUN].decode(), AG.AI_RUN_SCRIPT)
        self.assertEqual(qga.modes[AG.AI_RUN], "755")
        self.assertEqual(qga.files[G.APT_LIST], b"deb [trusted=yes] http://192.0.2.7:8080/ milkyway main\n")
        self.assertIn(b'Dir::Etc::sourcelist "/etc/apt/agino-build.list"', qga.files[AG.APT_CONF])
        self.assertNotIn(G.CONTROL, qga.files)
        self.assertNotIn(G.SRC_TAR, qga.files)

    def test_builder_gets_sudo_only_in_the_ai_guest(self):
        qga = self.prepared()
        self.assertEqual(qga.files[AG.SUDOERS_AI], b"builder ALL=(root) NOPASSWD: ALL\n")
        self.assertEqual(qga.modes[AG.SUDOERS_AI], "440")

    def test_environment_for_claude(self):
        qga = self.prepared()
        lines = qga.files[AG.ENVIRONMENT_FILE].decode().splitlines()
        self.assertIn("HTTPS_PROXY=http://10.231.0.1:3128", lines)
        self.assertIn("HTTP_PROXY=http://10.231.0.1:3128", lines)
        self.assertIn("NO_PROXY=10.231.0.1,localhost,127.0.0.1", lines)
        self.assertIn("ANTHROPIC_BASE_URL=http://10.231.0.1:8490/anthropic", lines)
        self.assertIn("ANTHROPIC_API_KEY=agino-placeholder", lines)
        self.assertIn("ANTHROPIC_MODEL=agino", lines)
        self.assertIn("DISABLE_AUTOUPDATER=1", lines)
        self.assertIn("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1", lines)
        self.assertIn("DISABLE_TELEMETRY=1", lines)
        self.assertFalse([l for l in lines if "OPENAI" in l or "CODEX" in l])
        self.assertNotIn(AG.CODEX_CONFIG, qga.files)

    def test_environment_and_configuration_for_codex(self):
        qga = self.prepared(ai_task_doc(ai={"agent": "codex", "kind": "import", "minutes": 30}))
        lines = qga.files[AG.ENVIRONMENT_FILE].decode().splitlines()
        self.assertIn("CODEX_HOME=/home/builder/.codex", lines)
        self.assertIn("OPENAI_API_KEY=agino-placeholder", lines)
        self.assertFalse([l for l in lines if "ANTHROPIC" in l])
        toml = qga.files[AG.CODEX_CONFIG].decode()
        self.assertIn('base_url = "http://10.231.0.1:8490/openai/v1"', toml)
        self.assertIn('wire_api = "responses"', toml)
        self.assertIn('env_key = "OPENAI_API_KEY"', toml)
        self.assertEqual(qga.modes[AG.CODEX_CONFIG], "600")
        chown = [a for kind, a in qga.execs if kind == "exec" and "/bin/chown" in a]
        self.assertEqual(chown, [["/bin/chown", "builder:builder", AG.CODEX_CONFIG]])

    def test_no_real_secret_is_ever_written(self):
        qga = self.prepared()
        text = b"\n".join(qga.files.values()).decode("utf-8", "replace")
        for line in text.splitlines():
            if "KEY" in line or "TOKEN" in line:
                self.assertTrue(line.endswith("=agino-placeholder"), line)

    def test_overridden_configuration_is_used(self):
        qga = self.prepared(ai_env="A=1", ai_claude_env="B=2", ai_claude_cmd="claude -p hello")
        self.assertEqual(qga.files[AG.ENVIRONMENT_FILE], b"A=1\nB=2\n")
        argv = next(a for a in qga.started if AG.AI_RUN in a)
        self.assertEqual(argv[-1], "claude -p hello")


class AgentRunTests(AiCase):
    def agent_argv(self, doc=None, **builder):
        self.make_ai(doc, configure=deliver(), **builder).run()
        return next(a for a in self.factory.vms[0].qga.started if AG.AI_RUN in a)

    def test_the_agent_runs_as_builder_in_a_minimal_environment(self):
        argv = self.agent_argv()
        self.assertEqual(argv[:6], [G.RUNUSER, "-u", "builder", "--", G.ENV_BIN, "-i"])
        i = argv.index(AG.AI_RUN)
        env = [a for a in argv[6:i] if "=" in a]
        self.assertEqual(env[:len(G.USER_ENV)], G.USER_ENV)
        self.assertIn("ANTHROPIC_BASE_URL=http://10.231.0.1:8490/anthropic", env)
        self.assertIn("HTTPS_PROXY=http://10.231.0.1:3128", env)
        self.assertEqual(argv[i + 1], AG.AGENT_LOG)
        self.assertEqual(argv[i + 2], "1800")
        self.assertEqual(argv[i + 3], self.cfg.builder.ai_claude_cmd)
        self.assertEqual(len(argv), i + 4)

    def test_default_command_lines(self):
        cmd = self.agent_argv()[-1]
        for part in ("claude", "--bare", '-p "$(cat /work/TASK.md)"', "--output-format stream-json", "--verbose",
                     "--permission-mode bypassPermissions"):
            self.assertIn(part, cmd)
        codex = self.agent_argv(ai_task_doc(ai={"agent": "codex", "kind": "repair", "minutes": 30}))[-1]
        self.assertTrue(codex.startswith("codex exec "))
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", codex)

    def test_the_time_limit_is_ai_minutes(self):
        argv = self.agent_argv(ai_task_doc(ai={"agent": "claude", "kind": "import", "minutes": 7}))
        self.assertEqual(argv[argv.index(AG.AI_RUN) + 2], "420")

    def test_the_wrapper_limits_the_process_group(self):
        text = AG.AI_RUN_SCRIPT
        self.assertIn("cd /work", text)
        self.assertIn('/usr/bin/timeout -k 10 "$2" /bin/sh -c "$3"', text)

    def test_the_snippet_is_one_argument_and_comes_from_the_configuration(self):
        nasty = 'echo $(id) `id`; rm -rf / # "x"'
        argv = self.agent_argv(ai_claude_cmd=nasty)
        self.assertEqual(argv.count(nasty), 1)

    def test_output_is_summarised_in_the_build_log(self):
        out = (event(type="system", subtype="init", api_key="SECRETKEY") +
               event(type="assistant", message={"content": [
                   {"type": "text", "text": "red \x1b[31mtext\x1b[0m"},
                   {"type": "tool_use", "name": "Bash", "input": {"command": "make"}}]}) +
               event(type="stream_event", event={"request_body": "BODYTEXT"}) +
               b"plain line\x00 with nul\n" + GOOD_OUT)
        self.make_ai(configure=deliver(out=out)).run()
        text = self.log_text()
        self.assertIn("system/init\n", text)
        self.assertIn("assistant: red [31mtext[0m\n", text)
        self.assertIn("assistant: tool_use Bash make\n", text)
        self.assertIn("stream_event\n", text)
        self.assertIn("text: plain line with nul\n", text)
        self.assertIn("result/success is_error=False turns=3: delivered", text)
        self.assertIn("== claude exited with code 0 ==", text)
        for bad in ("SECRETKEY", "BODYTEXT", "\x1b", "\x00"):
            self.assertNotIn(bad, text)

    def test_output_streams_while_the_agent_runs(self):
        job = self.make_ai(configure=deliver(ai_hang=True))
        seen = []

        def hook():
            qga = self.factory.vms[0].qga
            qga.files[AG.AGENT_LOG] = assistant("hello from the agent")
            seen.append(self.log_text())
            if len(seen) > 6:
                job.request_stop("cancelled", "cancelled")
        self.sleep_hook = hook
        job.run()
        self.assertTrue(any("assistant: hello from the agent" in text for text in seen))
        self.assertNotIn("hello", seen[0])

    def test_the_log_sink_cap_does_not_stop_the_agent_stream(self):
        out = b"".join(assistant("x" * 250) for _ in range(25000)) + retry(503)
        self.assertGreater(len(out), 5 << 20)
        self.make_ai(configure=deliver(out=out, exit_code=1, files={}), max_log_mib=1).run()
        self.assertLess(os.path.getsize(self.out("build.log")), (1 << 20) + 300)
        self.assertEqual(self.result()["class"], "model")


class ModelFailureTests(AiCase):
    def run_failure(self, out, exit_code=1, files=None):
        self.make_ai(configure=deliver(files={} if files is None else files, out=out, exit_code=exit_code)).run()
        return self.result()

    def test_retry_statuses_that_mean_the_model_service_failed(self):
        for status in (401, 402, 429, 500, 503, 529):
            res = self.run_failure(event(type="system", subtype="init") + retry(status))
            self.assertEqual((res["ok"], res["class"]), (False, "model"), status)
            self.assertIn("model service failed (status %d)" % status, res["error"])
            self.assertEqual(self.status()["state"], "failed")

    def test_proxy_paused(self):
        res = self.run_failure(b'API Error: 503 {"error": {"type": "proxy_paused"}}\n')
        self.assertEqual(res["class"], "model")
        self.assertEqual(res["error"], "the model service failed")

    def test_other_statuses_are_recipe_failures(self):
        for status in (400, 403, 404, None):
            self.assertEqual(self.run_failure(retry(status))["class"], "recipe", status)

    def test_a_model_that_answered_again_is_not_the_cause(self):
        res = self.run_failure(retry(429) + assistant("back again"))
        self.assertEqual(res["class"], "recipe")

    def test_the_exit_code_decides(self):
        res = self.run_failure(retry(503), exit_code=0)
        self.assertEqual(res["class"], "recipe")
        self.assertIn("left no recipe.yaml and no app.yaml", res["error"])

    def test_a_model_failure_wins_over_a_partial_delivery(self):
        res = self.run_failure(retry(503), files=DELIVERED)
        self.assertEqual((res["ok"], res["class"]), (False, "model"))
        self.assertEqual(sorted(res["files"]), sorted(DELIVERED))  # kept for the administrator

    def test_a_delivery_after_a_recovered_retry_is_ok(self):
        self.make_ai(configure=deliver(out=retry(503) + assistant("ok") + GOOD_OUT)).run()
        self.assertEqual(self.result()["class"], "ok")


class TimeoutTests(AiCase):
    def test_the_agent_hitting_the_limit(self):
        self.make_ai(configure=deliver(exit_code=124)).run()
        self.assertFailed("claude hit the time limit of 30 minutes", "timeout")
        self.assertEqual(self.result()["steps"][0]["exit"], 124)
        self.assertIn("(time limit)", self.log_text())

    def test_exit_137_far_from_the_limit_is_not_a_timeout(self):
        self.make_ai(configure=deliver(exit_code=137)).run()  # e.g. the OOM killer: the files decide
        self.assertEqual(self.result()["class"], "ok")
        self.assertEqual(self.result()["steps"][0]["exit"], 137)

    def test_a_killed_agent_at_the_limit_is_a_timeout(self):
        job = self.make_ai(configure=deliver(exit_code=137))
        real = job.d.clock

        def clock():  # once the agent was started, 30 minutes seem to have passed
            vms = self.factory.vms
            started = vms and any(AG.AI_RUN in a for a in vms[0].qga.started)
            return real() + (1800 if started else 0)
        job.d.clock = clock
        job.run()
        self.assertFailed("time limit of 30 minutes", "timeout")
        self.assertEqual(self.result()["steps"][0]["exit"], 124)

    def test_files_are_still_collected_after_a_timeout(self):
        self.make_ai(configure=deliver(exit_code=124)).run()
        self.assertFalse(self.result()["ok"])
        self.assertEqual(sorted(self.result()["files"]), sorted(DELIVERED))
        self.assertEqual(self.ai_files(), sorted(DELIVERED))

    def test_a_hanging_agent_is_given_up_by_the_host(self):
        job = self.make_ai(configure=deliver(ai_hang=True))
        self.assertEqual(job.run(), "failed")
        self.assertFailed("time limit of 30 minutes", "timeout")
        self.assertGreater(self.clock(), 1000 + 1800 + 10)
        self.assertTrue(self.factory.vms[0].stopped)
        self.assertEqual(self.ai_files(), [])


class DeliverableTests(AiCase):
    def delivered(self, files, **kw):
        self.make_ai(configure=deliver(files=files, **kw)).run()
        return self.result()

    def test_only_white_listed_names_are_taken(self):
        files = dict(DELIVERED)
        files.update({"evil.sh": b"x", "id_rsa": b"x", "recipe.yaml.bak": b"x", "UPPER.deb": DEB, ".hidden.deb": DEB,
                      "notes.md": b"x", "a b.deb": DEB, "x.deb.sh": b"x", "app.yaml~": b"x"})
        res = self.delivered(files)
        self.assertTrue(res["ok"])
        self.assertEqual(self.ai_files(), sorted(DELIVERED))
        self.assertEqual(sorted(res["files"]), sorted(DELIVERED))

    def test_hostile_listing_records_are_ignored_and_nothing_outside_is_read(self):
        extra = (b"5\t../../etc/passwd\0" b"5\tsub/recipe.yaml\0" b"x\trecipe.yaml\0" b"99999999999999999\tNOTES.md\0"
                 b"5\t\xff.deb\0" b"no tab at all\0" b"\0\0" b"-5\tapp.yaml\0" b"5\t/etc/passwd\0" b"5\trecipe.yaml\n\0"
                 b"7\tapp.yaml\0" b"3\tNOTES.md\0")  # the last two repeat names that were listed before
        res = self.delivered(dict(DELIVERED), list_extra=extra)
        self.assertTrue(res["ok"])
        self.assertEqual(self.ai_files(), sorted(DELIVERED))
        self.assertEqual(sorted(self.out_reads()), sorted("/work/out/" + n for n in DELIVERED))

    def test_a_symbolic_link_is_not_delivered(self):
        files = dict(DELIVERED)
        files["recipe.yaml"] = None  # a link to /etc/shadow: the listing leaves links out
        res = self.delivered(files)
        self.assertFailed("left no recipe.yaml", "recipe")
        self.assertNotIn("recipe.yaml", self.ai_files())
        self.assertNotIn("/work/out/recipe.yaml", self.out_reads())
        self.assertEqual(res["class"], "recipe")

    def test_the_collect_script_kills_the_agents_processes_first_and_lists_files_only(self):
        text = AG.AI_COLLECT_SCRIPT
        self.assertIn("kill -KILL -1", text)
        self.assertLess(text.index("kill_all"), text.index("find /work/out"))
        self.assertIn("-type f", text)
        self.assertIn("-maxdepth 1", text)
        self.assertIn("runuser -u builder", text)

    def test_the_agent_is_stopped_before_the_listing(self):
        self.make_ai(configure=deliver()).run()
        execs = self.factory.vms[0].qga.execs
        self.assertEqual(execs[-1], ("sh", AG.AI_COLLECT_SCRIPT))

    def test_oversized_files_are_skipped_before_reading(self):
        files = dict(DELIVERED)
        files["NOTES.md"] = b"x" * (AG.MAX_TEXT_BYTES + 1)
        files["upstream.yaml"] = b"y" * AG.MAX_TEXT_BYTES  # exactly at the limit: taken
        files[DEBNAME] = DEB + b"z" * (2 << 20)
        self.make_ai(configure=deliver(files=files), max_artifact_mib=1).run()
        res = self.result()
        self.assertTrue(res["ok"])
        self.assertEqual(sorted(res["files"]), ["app.yaml", "recipe.yaml", "upstream.yaml"])
        self.assertEqual(sorted(self.out_reads()),
                         ["/work/out/app.yaml", "/work/out/recipe.yaml", "/work/out/upstream.yaml"])
        self.assertIn("skipped NOTES.md", self.log_text())
        self.assertIn("skipped " + DEBNAME, self.log_text())

    def test_an_oversized_recipe_means_no_delivery(self):
        files = dict(DELIVERED)
        files["recipe.yaml"] = b"x" * (AG.MAX_TEXT_BYTES + 1)
        self.delivered(files)
        self.assertFailed("left no recipe.yaml", "recipe")
        self.assertNotIn("recipe.yaml", self.ai_files())

    def test_empty_files_are_skipped(self):
        files = dict(DELIVERED)
        files["app.yaml"] = b""
        self.delivered(files)
        self.assertFailed("left no app.yaml", "recipe")

    def test_a_deb_above_the_artifact_limit_is_skipped_and_the_rest_is_kept(self):
        files = {"recipe.yaml": b"r", "app.yaml": b"a", "big_1_amd64.deb": DEB + b"z" * (2 << 20)}
        self.make_ai(configure=deliver(files=files), max_artifact_mib=1).run()
        self.assertTrue(self.result()["ok"])
        self.assertEqual(self.result()["files"], ["app.yaml", "recipe.yaml"])
        self.assertEqual(self.ai_files(), ["app.yaml", "recipe.yaml"])

    def test_a_large_deb_within_the_limit_is_copied_in_chunks(self):
        big = DEB + b"z" * (3 << 20)
        res = self.delivered({"recipe.yaml": b"r", "app.yaml": b"a", "big_1_amd64.deb": big})
        self.assertIn("big_1_amd64.deb", res["files"])
        with open(self.ai_path("big_1_amd64.deb"), "rb") as f:
            self.assertEqual(f.read(), big)

    def test_a_bad_deb_and_a_binary_text_file_are_skipped_without_leftovers(self):
        files = dict(DELIVERED)
        files[DEBNAME] = b"#!/bin/sh\necho pwned\n"
        files["NOTES.md"] = b"a\x00b"
        res = self.delivered(files)
        self.assertTrue(res["ok"])
        self.assertEqual(self.ai_files(), ["app.yaml", "recipe.yaml", "upstream.yaml"])
        self.assertIn("skipped NOTES.md", self.log_text())

    def test_only_one_deb_is_taken(self):
        files = {"recipe.yaml": b"r", "app.yaml": b"a", "b_1_amd64.deb": DEB, "a_1_amd64.deb": DEB}
        res = self.delivered(files)
        self.assertEqual(res["files"], ["a_1_amd64.deb", "app.yaml", "recipe.yaml"])
        self.assertIn("ignored an extra deb: b_1_amd64.deb", self.log_text())

    def test_the_recipe_alone_is_not_enough(self):
        self.delivered({"recipe.yaml": b"r", "NOTES.md": b"n"})
        self.assertFailed("left no app.yaml", "recipe")
        self.assertEqual(self.result()["files"], ["NOTES.md", "recipe.yaml"])

    def test_nothing_delivered(self):
        self.delivered({})
        self.assertFailed("claude exited with code 0 and left no recipe.yaml and no app.yaml", "recipe")
        self.assertEqual(self.result()["files"], [])

    def test_the_agents_exit_code_alone_is_not_a_failure(self):
        res = self.delivered(dict(DELIVERED), exit_code=1)
        self.assertTrue(res["ok"])
        self.assertEqual(self.result()["steps"][0]["exit"], 1)

    def test_a_guest_agent_failure_while_reading_is_infra(self):
        def configure(vm):
            deliver()(vm)
            vm.qga.work_out["recipe.yaml"] = None
            vm.qga.list_extra = b"5\trecipe.yaml\0"  # listed as a file, gone when read
        self.make_ai(configure=configure).run()
        self.assertFailed("sandbox: guest agent", "infra")
        self.assertNotIn("recipe.yaml", self.ai_files())
        self.assertFalse([f for f in os.listdir(self.out("ai")) if f.startswith(".")])


class WorkspaceTests(AiCase):
    def test_missing_workspace_fails_before_any_vm(self):
        job = self.make_ai(configure=deliver())
        os.unlink(os.path.join(self.cfg.builder.queue, TASK_ID, "workspace.tar"))
        job.run()
        self.assertEqual(self.factory.calls, 0)
        self.assertFailed("cannot open workspace.tar", "infra")

    def test_symlinked_workspace_is_refused(self):
        job = self.make_ai(configure=deliver())
        path = os.path.join(self.cfg.builder.queue, TASK_ID, "workspace.tar")
        os.unlink(path)
        os.symlink("/etc/passwd", path)
        job.run()
        self.assertEqual(self.factory.calls, 0)
        self.assertFailed("cannot open workspace.tar", "infra")

    def test_oversized_and_empty_workspaces(self):
        self.make_ai(configure=deliver(), max_source_mib=1, workspace=b"x" * (2 << 20)).run()
        self.assertEqual(self.factory.calls, 0)
        self.assertFailed("larger than 1 MiB", "infra")
        self.make_ai(configure=deliver(), workspace=b"").run()
        self.assertEqual(self.factory.calls, 0)
        self.assertFailed("empty", "infra")

    def test_workspace_damaged_in_the_guest(self):
        self.make_ai(configure=deliver(corrupt_source=True)).run()
        self.assertFailed("damaged", "infra")

    def test_workspace_without_task_file(self):
        self.make_ai(configure=deliver(task_md=False)).run()
        self.assertFailed("no TASK.md", "infra")
        self.assertFalse([a for a in self.factory.vms[0].qga.started if AG.AI_RUN in a])

    def test_not_enough_host_disk(self):
        self.make_ai(configure=deliver(), free_bytes=lambda path: 1 << 20).run()
        self.assertEqual(self.factory.calls, 0)
        self.assertFailed("free disk space", "infra")


class StopAndInfraTests(AiCase):
    def test_cancel_while_the_agent_runs(self):
        job = self.make_ai(configure=deliver(ai_hang=True))
        count = []

        def hook():
            count.append(1)
            if len(count) == 3:
                job.request_stop("cancelled", "cancelled")
        self.sleep_hook = hook
        self.assertEqual(job.run(), "cancelled")
        vm = self.factory.vms[0]
        self.assertTrue(vm.killed and vm.stopped)
        self.assertEqual(self.status()["state"], "cancelled")
        self.assertEqual((self.result()["ok"], self.result()["error"], self.result()["class"]),
                         (False, "cancelled", "infra"))

    def test_runner_shutdown_ends_the_task_as_failed(self):
        job = self.make_ai(configure=deliver(ai_hang=True))
        self.sleep_hook = lambda: job.request_stop("failed", "the build runner was stopped")
        self.assertEqual(job.run(), "failed")
        self.assertEqual(self.result()["error"], "the build runner was stopped")

    def test_vm_exit_is_detected(self):
        job = self.make_ai(configure=deliver(ai_hang=True))
        self.sleep_hook = lambda: setattr(self.factory.vms[0], "exit_now", True)
        job.run()
        self.assertFailed("exited unexpectedly", "infra")

    def test_boot_failure_still_cleans_up(self):
        def configure(vm):
            vm.boot_error = InfraError("qemu exited early")
        self.make_ai(configure=configure).run()
        self.assertFailed("qemu exited early", "infra")
        self.assertTrue(self.factory.vms[0].stopped)

    def test_disk_limit(self):
        def configure(vm):
            deliver(ai_hang=True)(vm)
            vm.disk = 41 * GIB
        self.make_ai(configure=configure).run()
        self.assertFailed("disk limit of 40 GiB", "infra")

    def test_unexpected_exception_ends_failed(self):
        def configure(vm):
            vm.qga.fail_on["write_stream"] = RuntimeError("boom")
        self.make_ai(configure=configure).run()
        self.assertFailed("internal error: RuntimeError", "infra")

    def test_guest_agent_error_ends_failed(self):
        def configure(vm):
            vm.qga.fail_on["status"] = InfraError("guest agent guest-exec-status timed out")
        self.make_ai(configure=configure).run()
        self.assertFailed("sandbox: guest agent", "infra")

    def test_the_preparation_has_its_own_allowance(self):
        job = self.make_ai(configure=deliver())
        job.out.create()
        job._set("starting")
        self.assertEqual((job.phase_class, job.phase_error), ("infra", "starting the AI guest timed out"))
        self.clock.t += A.PREPARE_TIMEOUT + 5
        with self.assertRaises(A.BuildStepError) as raised:
            job._check()
        self.assertEqual(raised.exception.klass, "infra")
        job.out.close()

    def test_the_allowances_of_the_running_and_collecting_phases(self):
        job = self.make_ai(ai_task_doc(ai={"agent": "claude", "kind": "import", "minutes": 10}))
        job.out.create()
        job._set("running")
        self.assertEqual(job.phase_end - self.clock(), 600 + A.AI_KILL_GRACE + A.AI_BACKSTOP)
        self.assertEqual(job.phase_class, "timeout")
        job._set("collecting")
        self.assertEqual(job.phase_end - self.clock(), A.COLLECT_TIMEOUT)
        self.assertEqual(job.phase_class, "timeout")
        job.out.close()


class StatusTests(AiCase):
    def test_status_json_has_no_steps(self):
        self.make_ai(configure=deliver()).run()
        status = self.status()
        self.assertEqual((status["state"], status["step"], status["steps_total"]), ("done", 0, 0))


if __name__ == "__main__":
    unittest.main()
