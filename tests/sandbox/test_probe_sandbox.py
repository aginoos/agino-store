# SPDX-License-Identifier: GPL-3.0-or-later
import os
import subprocess
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server"))
from agino_store_sandbox import runner, vm  # noqa: E402
from agino_store_sandbox.util import SandboxError  # noqa: E402


class ProbeSandboxTests(unittest.TestCase):
    cfg = types.SimpleNamespace(qemu="qemu-system-x86_64")

    def test_probe_tells_qemu_to_quit(self):
        # A stopped qemu ignores EOF on its QMP stdin: without a quit command the probe times out and
        # reports "not supported", so -sandbox was never enabled.
        seen = {}

        def fake_run(argv, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        with mock.patch.object(vm.subprocess, "run", fake_run):
            self.assertTrue(vm.probe_sandbox(self.cfg))
        self.assertIn(b'"quit"', seen["input"])
        self.assertIn(b"qmp_capabilities", seen["input"])

    def test_probe_false_on_timeout_or_failure(self):
        sleeps = []
        with mock.patch.object(vm.subprocess, "run", side_effect=subprocess.TimeoutExpired("q", 15)), \
                mock.patch.object(vm, "log"):
            self.assertFalse(vm.probe_sandbox(self.cfg, sleep=sleeps.append))
        with mock.patch.object(vm.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"")), \
                mock.patch.object(vm, "log"):
            self.assertFalse(vm.probe_sandbox(self.cfg, sleep=sleeps.append))
        self.assertEqual(sleeps, [2.0, 2.0] * 2)  # between the three attempts, not after the last

    def test_probe_retries_and_logs_the_reason(self):
        answers = [subprocess.TimeoutExpired("q", 15), OSError(2, "No such file or directory"),
                   subprocess.CompletedProcess([], 0, b"", b"")]
        logged, sleeps = [], []
        with mock.patch.object(vm.subprocess, "run", side_effect=answers) as run, \
                mock.patch.object(vm, "log", logged.append):
            self.assertTrue(vm.probe_sandbox(self.cfg, sleep=sleeps.append))
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleeps, [2.0, 2.0])
        self.assertEqual(len(logged), 2)
        self.assertIn("timed out", logged[0])
        self.assertIn("No such file or directory", logged[1])

    def test_probe_logs_exit_status_and_stderr(self):
        failed = subprocess.CompletedProcess([], 1, b"", b"qemu: seccomp is not available\n")
        logged = []
        with mock.patch.object(vm.subprocess, "run", return_value=failed), mock.patch.object(vm, "log", logged.append):
            self.assertFalse(vm.probe_sandbox(self.cfg, sleep=lambda s: None))
        self.assertEqual(len(logged), 3)
        self.assertIn("exit status 1", logged[0])
        self.assertIn("seccomp is not available", logged[0])

    def test_probe_succeeds_first_time_without_logging_or_sleeping(self):
        ok = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch.object(vm.subprocess, "run", return_value=ok), mock.patch.object(vm, "log") as log:
            self.assertTrue(vm.probe_sandbox(self.cfg, sleep=lambda s: self.fail("slept")))
        log.assert_not_called()


class SandboxFlagTests(unittest.TestCase):
    def cfg(self, mode):
        return types.SimpleNamespace(qemu="qemu-system-x86_64", qemu_sandbox=mode)

    def test_batch_auto_falls_back_with_a_warning(self):
        logged = []
        with mock.patch.object(runner, "probe_sandbox", return_value=False), mock.patch.object(runner, "log", logged.append):
            self.assertFalse(runner._sandbox_flag(self.cfg("auto")))
        self.assertTrue(any("seccomp sandbox unavailable" in m for m in logged))

    def test_batch_auto_enabled_when_the_probe_works(self):
        with mock.patch.object(runner, "probe_sandbox", return_value=True):
            self.assertTrue(runner._sandbox_flag(self.cfg("auto")))

    def test_session_auto_fails_closed(self):
        with mock.patch.object(runner, "probe_sandbox", return_value=False):
            with self.assertRaisesRegex(SandboxError, "seccomp sandbox unavailable"):
                runner.session_sandbox_flag(self.cfg("auto"))

    def test_session_auto_enabled_when_the_probe_works(self):
        with mock.patch.object(runner, "probe_sandbox", return_value=True):
            self.assertTrue(runner.session_sandbox_flag(self.cfg("auto")))

    def test_session_explicit_modes_do_not_probe(self):
        with mock.patch.object(runner, "probe_sandbox", side_effect=AssertionError("probed")):
            self.assertTrue(runner.session_sandbox_flag(self.cfg("on")))
            self.assertFalse(runner.session_sandbox_flag(self.cfg("off")))


if __name__ == "__main__":
    unittest.main()
