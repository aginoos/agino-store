# SPDX-License-Identifier: GPL-3.0-or-later
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import session_helpers  # noqa: E402,F401

from agino_store_sandbox import shots_guest  # noqa: E402
from agino_store_sandbox.qga import ExecResult  # noqa: E402
from agino_store_sandbox.util import InfraError  # noqa: E402


class RecordingQga:
    def __init__(self, fail_at=None, timed_out=False):
        self.calls, self.fail_at, self.timed_out = [], fail_at, timed_out

    def _result(self):
        failing = self.fail_at is not None and len(self.calls) == self.fail_at + 1
        return ExecResult(1 if failing else 0, b"", b"", timed_out=failing and self.timed_out)

    def sh(self, script, args=(), timeout=60, input_data=None):
        self.calls.append(("sh", script, list(args), timeout))
        return self._result()

    def exec(self, argv, timeout, input_data=None):
        self.calls.append(("exec", list(argv), timeout))
        return self._result()


class FakeGuest:
    def __init__(self, qga):
        self.qga = qga
        self.env = {}


class SwitchTests(unittest.TestCase):
    def guest(self, **kw):
        qga = RecordingQga(**kw)
        return shots_guest.ShotsGuest(FakeGuest(qga)), qga

    def test_the_five_steps_in_order(self):
        sg, qga = self.guest()
        sg.switch_desktop_locale("en_US.UTF-8")
        c = qga.calls
        self.assertEqual(len(c), 5)
        self.assertEqual((c[0][0], c[0][2]), ("sh", ["en_US.UTF-8"]))  # the locale is an argument, not script text
        self.assertNotIn("en_US", c[0][1])
        self.assertEqual(c[1][1], ["/usr/bin/busctl", "--system", "call", "org.deepin.dde.Accounts1",
                                   "/org/deepin/dde/Accounts1/User1000", "org.deepin.dde.Accounts1.User",
                                   "SetLocale", "s", "en_US.UTF-8"])
        names = ["LANG", "LC_ADDRESS", "LC_IDENTIFICATION", "LC_MEASUREMENT", "LC_MONETARY", "LC_NAME",
                 "LC_NUMERIC", "LC_PAPER", "LC_TELEPHONE", "LC_TIME"]
        env = ["%s=en_US.UTF-8" % n for n in names]
        self.assertEqual(c[2][1], ["/usr/bin/systemctl", "set-environment"] + env)
        self.assertEqual(c[3][1], ["/usr/sbin/runuser", "-u", "tester", "--", "/usr/bin/env",
                                   "XDG_RUNTIME_DIR=/run/user/1000", "/usr/bin/systemctl", "--user",
                                   "set-environment"] + env)
        self.assertEqual(c[4][1], ["/usr/bin/systemctl", "restart", "lightdm"])
        self.assertTrue(all(call[-1] <= 90 for call in c))  # every step has a timeout

    def test_only_the_two_locales_are_allowed(self):
        for bad in ("fr_FR.UTF-8", "zh_CN", "en_US.UTF-8; reboot", "", "zh_CN.UTF-8\n", None, "$(id)"):
            sg, qga = self.guest()
            with self.assertRaises(InfraError):
                sg.switch_desktop_locale(bad)
            self.assertEqual(qga.calls, [], bad)
        sg, qga = self.guest()
        sg.switch_desktop_locale("zh_CN.UTF-8")
        self.assertEqual(len(qga.calls), 5)

    def test_a_failing_step_stops_the_sequence_and_is_named(self):
        for index, name in enumerate(("locale files", "accounts", "system manager", "user manager",
                                      "display manager")):
            sg, qga = self.guest(fail_at=index)
            with self.assertRaises(InfraError) as ctx:
                sg.switch_desktop_locale("en_US.UTF-8")
            self.assertIn(name, str(ctx.exception))
            self.assertEqual(len(qga.calls), index + 1)  # nothing runs after the failing step

    def test_timeouts_are_reported(self):
        sg, _ = self.guest(fail_at=4, timed_out=True)
        with self.assertRaises(InfraError) as ctx:
            sg.switch_desktop_locale("zh_CN.UTF-8")
        self.assertIn("timed out", str(ctx.exception))


class LocaleScriptTests(unittest.TestCase):
    """The shell script of step 1 really edits both files (run with the host's sh on temporary copies)."""

    def run_script(self, files, locale):
        with tempfile.TemporaryDirectory() as tmp:
            script = shots_guest.SET_LOCALE_FILES
            paths = {}
            for name, text in files.items():
                path = os.path.join(tmp, name)
                paths[name] = path
                with open(path, "w") as f:
                    f.write(text)
                script = script.replace("/etc/default/locale" if name == "default" else "/etc/locale.conf", path)
            done = subprocess.run(["/bin/sh", "-c", script, "sh", locale], capture_output=True)
            return done.returncode, {n: open(p).read() for n, p in paths.items()}

    def test_lang_and_all_lc_variables_are_replaced(self):
        code, out = self.run_script({"default": "LANG=zh_CN.UTF-8\nLC_TIME=zh_CN.UTF-8\nLC_PAPER=zh_CN.UTF-8\n",
                                     "conf": "LANG=zh_CN.UTF-8\n"}, "en_US.UTF-8")
        self.assertEqual(code, 0)
        self.assertEqual(out["default"], "LANG=en_US.UTF-8\nLC_TIME=en_US.UTF-8\nLC_PAPER=en_US.UTF-8\n")
        self.assertEqual(out["conf"], "LANG=en_US.UTF-8\n")

    def test_lang_is_added_when_missing_and_other_lines_stay(self):
        code, out = self.run_script({"default": "# comment\nFOO=bar\n", "conf": "LC_NAME=zh_CN.UTF-8\n"},
                                    "zh_CN.UTF-8")
        self.assertEqual(code, 0)
        self.assertEqual(out["default"], "# comment\nFOO=bar\nLANG=zh_CN.UTF-8\n")
        self.assertEqual(out["conf"], "LC_NAME=zh_CN.UTF-8\nLANG=zh_CN.UTF-8\n")


if __name__ == "__main__":
    unittest.main()
