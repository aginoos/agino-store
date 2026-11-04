# SPDX-License-Identifier: GPL-3.0-or-later
import dataclasses
import hashlib
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_helpers import (DEB, SOURCE, TASK_ID, Clock, VmFactory, builder_cfg, read_json,  # noqa: E402
                           task_doc, write_task)

from agino_store_sandbox import build_guest as G  # noqa: E402
from agino_store_sandbox import build_job as J  # noqa: E402
from agino_store_sandbox import build_out  # noqa: E402
from agino_store_sandbox.build_task import read_task_file  # noqa: E402
from agino_store_sandbox.util import InfraError, SandboxError  # noqa: E402

GIB = 1 << 30


class JobCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.states = []
        original = build_out.OutDir.write_status

        def spy(out, state, *args, **kwargs):
            self.states.append(state)
            return original(out, state, *args, **kwargs)

        patcher = mock.patch.object(build_out.OutDir, "write_status", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sleep_hook = None

    def make(self, doc=None, source=SOURCE, configure=None, free_bytes=None, **builder):
        self.cfg = builder_cfg(self.tmp.name, **builder)
        write_task(self.cfg, doc=doc, source=source)
        self.task = read_task_file(self.cfg.builder.queue, TASK_ID, self.cfg.builder.limits)
        self.factory = VmFactory(configure)

        def sleep(n):
            self.clock.sleep(n)
            if self.sleep_hook:
                self.sleep_hook()

        deps = J.Deps(self.factory, self.clock, sleep, free_bytes or (lambda path: 1 << 50))
        self.job = J.BuildJob(self.cfg, self.task, 0, deps)
        return self.job

    def out(self, name):
        return os.path.join(self.cfg.builder.out, TASK_ID, name)

    def result(self):
        return read_json(self.out("result.json"))

    def status(self):
        return read_json(self.out("status.json"))

    def log_text(self):
        with open(self.out("build.log"), encoding="utf-8") as f:
            return f.read()


class HappyPathTests(JobCase):
    def test_full_run(self):
        job = self.make()
        self.assertEqual(job.run(), "done")
        self.assertEqual(self.states, ["starting", "starting", "installing_deps", "building", "building",
                                       "building", "building", "packaging", "collecting", "done"])
        res = self.result()
        self.assertTrue(res["ok"])
        self.assertEqual(res["error"], "")
        self.assertEqual([s["n"] for s in res["steps"]], [1, 2, 3])
        self.assertEqual([s["exit"] for s in res["steps"]], [0, 0, 0])
        self.assertEqual(res["steps"][0]["cmd"], "./configure --prefix=/usr")
        name = "demo-agino_1.0+agino1_amd64.deb"
        self.assertEqual(res["artifacts"], [{"file": name, "sha256": hashlib.sha256(DEB).hexdigest(),
                                             "size": len(DEB)}])
        with open(self.out(name), "rb") as f:
            self.assertEqual(f.read(), DEB)
        self.assertEqual(self.status()["state"], "done")
        self.assertIn("ran: make -j4", self.log_text())
        self.assertIn("apt install done", self.log_text())
        vm = self.factory.vms[0]
        self.assertTrue(vm.stopped)
        self.assertTrue(job.finished.is_set())

    def test_permissions(self):
        self.make().run()
        directory = os.path.join(self.cfg.builder.out, TASK_ID)
        mode = os.stat(directory).st_mode
        self.assertEqual(stat.S_IMODE(mode), 0o750)
        self.assertFalse(mode & stat.S_ISGID)  # the group comes from the parent, the process never sets the bit
        names = sorted(os.listdir(directory))
        self.assertEqual(names, ["build.log", "demo-agino_1.0+agino1_amd64.deb", "result.json", "status.json"])
        for name in names:
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(directory, name)).st_mode), 0o640, name)

    def test_deb_name_comes_from_the_recipe_only(self):
        job = self.make()
        self.assertEqual(self.task.deb_name, "demo-agino_1.0+agino1_amd64.deb")
        job.run()
        self.assertEqual(sorted(f for f in os.listdir(os.path.dirname(self.out("x"))) if f.endswith(".deb")),
                         ["demo-agino_1.0+agino1_amd64.deb"])

    def test_guest_gets_fixed_scripts_control_and_apt_list(self):
        self.make(apt_source="[trusted=yes] http://192.0.2.7:8080/ milkyway main").run()
        qga = self.factory.vms[0].qga
        control = qga.files[G.CONTROL].decode()
        self.assertIn("Package: demo-agino\n", control)
        self.assertIn("Version: 1.0+agino1\n", control)
        self.assertIn("Architecture: amd64\n", control)
        self.assertIn("Depends: libfoo1 (>= 1.2)\n", control)
        self.assertEqual(qga.files[G.APT_LIST], b"deb [trusted=yes] http://192.0.2.7:8080/ milkyway main\n")
        self.assertEqual(qga.files[G.PACKAGE].decode(), G.PACKAGE_SCRIPT)
        self.assertEqual(qga.files[G.SRC_TAR], SOURCE)

    def test_apt_uses_only_the_configured_source_and_argv(self):
        self.make().run()
        apt = [a for a in self.factory.vms[0].qga.started if G.APT_GET in a]
        self.assertEqual(len(apt), 2)
        for argv in apt:
            self.assertIn("Dir::Etc::sourcelist=" + G.APT_LIST, argv)
            self.assertIn("Dir::Etc::sourceparts=-", argv)
        install = apt[1]
        tail = install[install.index("install"):]
        self.assertEqual(tail, ["install", "-y", "--no-install-recommends", "build-essential", "libfoo-dev"])

    def test_steps_run_as_builder_with_a_minimal_environment(self):
        self.make().run()
        argv = next(a for a in self.factory.vms[0].qga.started if G.RUN_STEP in a)
        self.assertEqual(argv[:4], [G.RUNUSER, "-u", "builder", "--"])
        self.assertEqual(argv[4:6], [G.ENV_BIN, "-i"])
        env = [a for a in argv[6:argv.index(G.RUN_STEP)] if "=" in a]
        self.assertEqual(env, G.USER_ENV)
        self.assertEqual(argv[-1], "./configure --prefix=/usr")

    def test_step_text_is_one_argument_without_expansion(self):
        doc = task_doc()
        nasty = "echo $(id) `id`; rm -rf / # 'quoted' \"x\" && true"
        doc["recipe"]["steps"] = [nasty]
        self.make(doc).run()
        argv = next(a for a in self.factory.vms[0].qga.started if G.RUN_STEP in a)
        self.assertEqual(argv[-1], nasty)
        self.assertEqual(argv.count(nasty), 1)

    def test_no_build_depends_skips_apt(self):
        doc = task_doc()
        doc["recipe"]["build_depends"] = []
        self.assertEqual(self.make(doc).run(), "done")
        self.assertFalse([a for a in self.factory.vms[0].qga.started if G.APT_GET in a])


class FailureTests(JobCase):
    def test_failing_step_stops_the_build(self):
        def configure(vm):
            vm.qga.step_exit["make -j4"] = 2
        job = self.make(configure=configure)
        self.assertEqual(job.run(), "failed")
        res = self.result()
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "step 2 failed with exit code 2")
        self.assertEqual([s["exit"] for s in res["steps"]], [0, 2])
        self.assertEqual(res["artifacts"], [])
        self.assertEqual(self.factory.vms[0].qga.step_commands(), ["./configure --prefix=/usr", "make -j4"])
        self.assertEqual(self.status()["state"], "failed")
        self.assertEqual(self.states[-1], "failed")
        self.assertTrue(self.factory.vms[0].stopped)
        self.assertFalse([f for f in os.listdir(os.path.dirname(self.out("x"))) if f.endswith(".deb")])

    def test_step_exit_124_is_a_timeout(self):
        def configure(vm):
            vm.qga.step_exit["make -j4"] = 124
        self.make(configure=configure).run()
        self.assertEqual(self.result()["error"], "step 2 timed out")
        self.assertEqual(self.result()["steps"][1]["exit"], 124)

    def test_hanging_step_hits_the_time_limit(self):
        doc = task_doc()
        doc["limits"] = {"cpu_minutes": 1}

        def configure(vm):
            vm.qga.hang_steps.add("make -j4")
        job = self.make(doc, configure=configure)
        self.assertEqual(job.run(), "failed")
        self.assertIn("time limit of 1 minutes", self.result()["error"])
        self.assertTrue(self.factory.vms[0].stopped)
        self.assertEqual(self.status()["state"], "failed")

    def test_cpu_minutes_bounds_the_step_timeout(self):
        doc = task_doc()
        doc["limits"] = {"cpu_minutes": 2}
        self.make(doc).run()
        argv = next(a for a in self.factory.vms[0].qga.started if G.RUN_STEP in a)
        self.assertLessEqual(int(argv[argv.index(G.RUN_STEP) + 2]), 120)

    def test_debian_directory_in_pkgroot_fails(self):
        def configure(vm):
            vm.qga.package_exit = 21
        self.make(configure=configure).run()
        self.assertIn("DEBIAN", self.result()["error"])
        self.assertEqual(self.status()["state"], "failed")
        self.assertIn('pkgroot/DEBIAN', G.PACKAGE_SCRIPT)
        self.assertIn("exit 21", G.PACKAGE_SCRIPT)

    def test_package_script_never_takes_upstream_control(self):
        text = G.PACKAGE_SCRIPT
        self.assertIn("cat /home/builder/control", text)
        self.assertLess(text.index("-e pkgroot/DEBIAN"), text.index("mkdir -m 0755 pkgroot/DEBIAN"))
        self.assertIn("--root-owner-group", text)

    def test_missing_pkgroot_message(self):
        def configure(vm):
            vm.qga.package_exit = 20
        self.make(configure=configure).run()
        self.assertIn("pkgroot", self.result()["error"])

    def test_bad_build_depends_never_reach_apt(self):
        job = self.make()
        bad = dataclasses.replace(self.task, build_depends=("ok-pkg", "evil; id"))
        job.task = bad
        self.assertEqual(job.run(), "failed")
        self.assertIn("invalid build dependency", self.result()["error"])
        self.assertFalse([a for a in self.factory.vms[0].qga.started if G.APT_GET in a])

    def test_apt_failure(self):
        def configure(vm):
            vm.qga.apt_exit["install"] = 100
        self.make(configure=configure).run()
        self.assertEqual(self.result()["error"], "apt-get install failed (exit 100)")

    def test_unexpected_exception_ends_failed(self):
        def configure(vm):
            vm.qga.fail_on["write_stream"] = RuntimeError("boom")
        job = self.make(configure=configure)
        self.assertEqual(job.run(), "failed")
        self.assertEqual(self.result()["error"], "internal error: RuntimeError")
        self.assertEqual(self.status()["state"], "failed")
        self.assertTrue(self.factory.vms[0].stopped)

    def test_guest_agent_error_ends_failed(self):
        def configure(vm):
            vm.qga.fail_on["status"] = InfraError("guest agent guest-exec-status timed out")
        self.make(configure=configure).run()
        self.assertTrue(self.result()["error"].startswith("sandbox: guest agent"))
        self.assertEqual(self.status()["state"], "failed")

    def test_boot_failure_still_cleans_up(self):
        def configure(vm):
            vm.boot_error = InfraError("qemu exited early")
        self.make(configure=configure).run()
        self.assertEqual(self.result()["error"], "sandbox: qemu exited early")
        self.assertTrue(self.factory.vms[0].stopped)

    def test_vm_exit_is_detected(self):
        def configure(vm):
            vm.qga.hang_steps.add("make -j4")
        self.make(configure=configure)
        self.sleep_hook = lambda: setattr(self.factory.vms[0], "exit_now", True)
        self.job.run()
        self.assertIn("exited unexpectedly", self.result()["error"])

    def test_disk_limit(self):
        doc = task_doc()
        doc["limits"] = {"disk_gib": 1}

        def configure(vm):
            vm.disk = 2 * GIB
            vm.qga.hang_steps.add("make -j4")
        self.make(doc, configure=configure).run()
        self.assertEqual(self.result()["error"], "disk limit of 1 GiB reached")

    def test_source_mismatch_fails_before_any_vm(self):
        self.make(source=SOURCE + b"tampered").run()
        self.assertEqual(self.factory.calls, 0)
        self.assertIn("does not match", self.result()["error"])

    def test_source_too_large(self):
        self.make(max_source_mib=1, source=b"x" * (2 << 20)).run()
        self.assertEqual(self.factory.calls, 0)
        self.assertIn("larger than 1 MiB", self.result()["error"])

    def test_source_damaged_in_guest(self):
        def configure(vm):
            vm.qga.corrupt_source = True
        self.make(configure=configure).run()
        self.assertIn("damaged", self.result()["error"])

    def test_not_enough_host_disk(self):
        self.make(free_bytes=lambda path: 1 << 20).run()
        self.assertEqual(self.factory.calls, 0)
        self.assertIn("free disk space", self.result()["error"])

    def test_seccomp_unavailable_fails_the_task(self):
        job = self.make()
        job.d.vm_factory = mock.Mock(side_effect=SandboxError("seccomp sandbox unavailable"))
        self.assertEqual(job.run(), "failed")
        self.assertIn("seccomp", self.result()["error"])


class ArtifactTests(JobCase):
    def test_oversized_artifact_is_refused_before_reading(self):
        def configure(vm):
            vm.qga.deb = DEB + b"y" * (2 << 20)
        self.make(configure=configure, max_artifact_mib=1).run()
        self.assertIn("over the limit of 1 MiB", self.result()["error"])
        self.assertFalse([f for f in os.listdir(os.path.dirname(self.out("x"))) if "deb" in f])

    def test_not_a_deb(self):
        def configure(vm):
            vm.qga.deb = b"#!/bin/sh\necho pwned\n"
        self.make(configure=configure).run()
        self.assertIn("not a deb", self.result()["error"])
        self.assertFalse([f for f in os.listdir(os.path.dirname(self.out("x"))) if "deb" in f])

    def test_artifact_must_be_a_regular_file(self):
        def configure(vm):
            vm.qga.stat_override = "4096 directory\n"
        self.make(configure=configure).run()
        self.assertIn("no deb", self.result()["error"])

    def test_large_artifact_is_read_in_chunks(self):
        big = DEB + b"z" * (3 << 20)

        def configure(vm):
            vm.qga.deb = big
        self.make(configure=configure).run()
        self.assertEqual(self.result()["artifacts"][0]["sha256"], hashlib.sha256(big).hexdigest())
        self.assertEqual(self.result()["artifacts"][0]["size"], len(big))


class LogTests(JobCase):
    def test_control_characters_are_removed(self):
        def configure(vm):
            vm.qga.step_out["make -j4"] = b"\x1b[31mred\x1b[0m\x00 nul\ttab\r\nline2\rline3\n\xc2\x85next\xff\n"
        self.make(configure=configure).run()
        text = self.log_text()
        for bad in ("\x1b", "\x00", "\r", "\x85"):
            self.assertNotIn(bad, text)
        self.assertIn("[31mred", text)
        self.assertIn("nul\ttab\nline2\nline3\n", text)

    def test_log_is_truncated_and_marked(self):
        def configure(vm):
            vm.qga.step_out["make -j4"] = b"a" * 80 + b"\n"
            vm.qga.step_out["./configure --prefix=/usr"] = (b"b" * 99 + b"\n") * 30000
        self.make(configure=configure, max_log_mib=1).run()
        path = self.out("build.log")
        self.assertLess(os.path.getsize(path), (1 << 20) + 200)
        self.assertIn("[log truncated: the limit of 1 MiB was reached]", self.log_text())

    def test_log_grows_while_a_step_runs(self):
        def configure(vm):
            vm.qga.hang_steps.add("make -j4")
        self.make(configure=configure)
        seen = []

        def hook():
            self.factory.vms[0].qga.files[G.USER_LOGS + "/step-2.log"] = b"compiling...\n"
            seen.append(self.log_text())
            if len(seen) > 6:
                self.job.request_stop("cancelled", "cancelled")
        self.sleep_hook = hook
        self.job.run()
        self.assertTrue(any("compiling..." in text for text in seen))
        self.assertFalse("compiling..." in seen[0])

    def test_sink_handles_split_utf8(self):
        path = os.path.join(self.tmp.name, "log")
        sink = build_out.LogSink(path, 1 << 20)
        data = "grüße\n".encode()
        sink.feed(data[:3])
        sink.feed(data[3:])
        sink.close()
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "grüße\n")


class CancelTests(JobCase):
    def test_cancel_while_building(self):
        def configure(vm):
            vm.qga.hang_steps.add("make -j4")
        self.make(configure=configure)
        count = []

        def hook():
            count.append(1)
            if len(count) == 3:
                self.job.request_stop("cancelled", "cancelled")
        self.sleep_hook = hook
        self.assertEqual(self.job.run(), "cancelled")
        vm = self.factory.vms[0]
        self.assertTrue(vm.killed)
        self.assertTrue(vm.stopped)
        self.assertEqual(self.status()["state"], "cancelled")
        self.assertFalse(self.result()["ok"])
        self.assertEqual(self.result()["error"], "cancelled")

    def test_cancel_file_is_seen_by_the_job(self):
        self.make()
        self.assertFalse(self.job.cancel_requested())
        open(os.path.join(self.cfg.builder.queue, TASK_ID, "cancel"), "w").close()
        self.assertTrue(self.job.cancel_requested())

    def test_stop_state_is_kept_for_a_runner_shutdown(self):
        def configure(vm):
            vm.qga.hang_steps.add("make -j4")
        self.make(configure=configure)
        self.sleep_hook = lambda: self.job.request_stop("failed", "the build runner was stopped")
        self.assertEqual(self.job.run(), "failed")
        self.assertEqual(self.result()["error"], "the build runner was stopped")

    def test_first_stop_request_wins(self):
        self.make()
        self.job.request_stop("cancelled", "cancelled")
        self.job.request_stop("failed", "other")
        self.assertEqual((self.job.stop_state, self.job.stop_reason), ("cancelled", "cancelled"))


if __name__ == "__main__":
    unittest.main()
