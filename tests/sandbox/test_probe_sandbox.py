# SPDX-License-Identifier: GPL-3.0-or-later
import subprocess
import types
import unittest
from unittest import mock

from agino_store_sandbox import vm


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
        with mock.patch.object(vm.subprocess, "run", side_effect=subprocess.TimeoutExpired("q", 15)):
            self.assertFalse(vm.probe_sandbox(self.cfg))
        with mock.patch.object(vm.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"")):
            self.assertFalse(vm.probe_sandbox(self.cfg))


if __name__ == "__main__":
    unittest.main()
