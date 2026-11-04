# SPDX-License-Identifier: GPL-3.0-or-later
import os
import stat
import subprocess
import sys
import tempfile
import unittest

CTL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "deploy", "session", "agino-session-ctl")
TOKEN = "ab" * 32
SID = "0123456789abcdef"

FAKE_SYSTEMCTL = r"""#!/bin/sh
# fake systemctl: logs its arguments, lists units from $FAKE/units, simulates the daemon on start
echo "$*" >> "$FAKE/calls"
case "$1" in
list-units) [ -f "$FAKE/list-fails" ] && exit 1; cat "$FAKE/units" 2>/dev/null; exit 0;;
start) [ -f "$FAKE/start-fails" ] && exit 1
       slot=${2#agino-store-session@}; slot=${slot%.service}
       if [ -f "$FAKE/daemon" ]; then
         mkdir -p "$FAKE/rt/$slot"
         [ -f "$FAKE/session-id" ] && cp "$FAKE/session-id" "$FAKE/rt/$slot/session.id"
         [ -f "$FAKE/token" ] && cp "$FAKE/token" "$FAKE/rt/$slot/token.once"
         [ -f "$FAKE/readonly-dir" ] && chmod 500 "$FAKE/rt/$slot"
       fi
       exit 0;;
stop) [ -f "$FAKE/stop-fails" ] && exit 1; exit 0;;
esac
exit 2
"""


class CtlCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = self.tmp.name
        os.makedirs(os.path.join(self.fake, "bin"))
        os.makedirs(os.path.join(self.fake, "rt"))
        path = os.path.join(self.fake, "bin", "systemctl")
        with open(path, "w") as f:
            f.write(FAKE_SYSTEMCTL)
        os.chmod(path, 0o755)
        self.daemon()

    def put(self, name, text):
        with open(os.path.join(self.fake, name), "w") as f:
            f.write(text)

    def daemon(self, session_id=SID + "\n", token=TOKEN + "\n"):
        self.put("daemon", "")
        for name, value in (("session-id", session_id), ("token", token)):
            if value is None:
                if os.path.exists(os.path.join(self.fake, name)):
                    os.unlink(os.path.join(self.fake, name))
            else:
                self.put(name, value)

    def run_ctl(self, *args, test=True, wait="0.5", limit=None):
        env = {"PATH": os.path.join(self.fake, "bin") + ":/usr/bin:/bin", "FAKE": self.fake}
        pre = []
        if test:
            env["AGINO_SESSION_CTL_TEST"] = "1"
            pre = ["--runtime-dir", os.path.join(self.fake, "rt"), "--wait", wait]
            if limit:
                pre += ["--max-sessions", str(limit)]
        done = subprocess.run([sys.executable, "-I", CTL, *pre, *args], capture_output=True, env=env, timeout=30,
                              text=True)
        return done.returncode, done.stdout, done.stderr

    def calls(self):
        try:
            with open(os.path.join(self.fake, "calls")) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []


class ArgumentTests(CtlCase):
    def test_bad_arguments_never_reach_systemctl(self):
        bad = [[], ["stop"], ["start", "vm0"], ["start", "demo"], ["start", ""], ["start", "-x"],
               ["start", "vm0", "vm1"], ["restart"], ["restart", "vm0"], ["START"], ["START", "vm0"],
               ["stop", "vm0", "vm1"], ["stop", "demo"], ["stop", "audacity"], ["stop", "VM0"], ["stop", "vm"],
               ["stop", "vm10"], ["stop", "vm-1"], ["stop", "vma"], ["stop", "vm0\n"], ["stop", "vm0 "],
               ["stop", " vm0"], ["stop", "xvm0"], ["stop", "vm0x"], ["stop", "../vm0"], ["stop", "vm0/.."],
               ["stop", "vm0.service"], ["stop", "vm0@x"], ["stop", "vm\u0663"], ["stop", "vm0;id"],
               ["stop", "$(id)"], ["stop", "vm0%i"], ["stop", ""], ["stop", "-"]]
        for args in bad:
            code, out, err = self.run_ctl(*args)
            self.assertEqual((code, out), (2, ""), args)
        self.assertEqual(self.calls(), [])

    def test_options_are_refused_outside_test_mode(self):
        for args in (["--runtime-dir", "/tmp", "start"], ["--max-sessions", "9", "start"],
                     ["--wait", "1", "start"], ["--help"], ["-h"]):
            code, out, err = self.run_ctl(*args, test=False)
            self.assertEqual((code, out), (2, ""), args)
        self.assertEqual(self.calls(), [])

    def test_unknown_or_malformed_options_in_test_mode(self):
        for args in (["--evil", "x", "start"], ["--runtime-dir"], ["--max-sessions", "x", "start"],
                     ["--max-sessions", "0", "start"], ["--max-sessions", "11", "start"],
                     ["--wait", "99", "start"]):
            env = {"PATH": "/usr/bin:/bin", "AGINO_SESSION_CTL_TEST": "1"}
            done = subprocess.run([sys.executable, "-I", CTL, *args], capture_output=True, env=env, text=True)
            self.assertEqual(done.returncode, 2, args)

    def test_test_variable_must_be_exactly_one(self):
        env = {"PATH": "/usr/bin:/bin", "AGINO_SESSION_CTL_TEST": "yes"}
        done = subprocess.run([sys.executable, "-I", CTL, "--runtime-dir", "/tmp", "start"], capture_output=True,
                              env=env, text=True)
        self.assertEqual(done.returncode, 2)


def unit(slot):
    return "agino-store-session@%s.service" % slot


def active(*slots):
    return "".join("%s loaded active running x\n" % unit(slot) for slot in slots)


class StartTests(CtlCase):
    def test_success_prints_three_lines_and_consumes_the_token(self):
        code, out, err = self.run_ctl("start")
        self.assertEqual((code, out, err), (0, "SLOT vm0\nSESSION %s\nTOKEN %s\n" % (SID, TOKEN), ""))
        self.assertFalse(os.path.exists(os.path.join(self.fake, "rt", "vm0", "token.once")))
        calls = self.calls()
        self.assertEqual(calls[0], "list-units --state=active --plain --no-legend agino-store-session@*.service")
        self.assertEqual(calls[1:], ["start agino-store-session@vm0.service"])

    def test_first_free_slot_is_chosen(self):
        self.put("units", active("vm0"))
        code, out, _ = self.run_ctl("start", limit=3)
        self.assertEqual((code, out.splitlines()[0]), (0, "SLOT vm1"))
        self.put("units", active("vm1"))
        code, out, _ = self.run_ctl("start", limit=3)
        self.assertEqual((code, out.splitlines()[0]), (0, "SLOT vm0"))
        self.put("units", active("vm0", "vm1"))
        code, out, _ = self.run_ctl("start", limit=3)
        self.assertEqual((code, out.splitlines()[0]), (0, "SLOT vm2"))
        self.put("units", active("vm0", "vm2"))
        code, out, _ = self.run_ctl("start", limit=3)
        self.assertEqual((code, out.splitlines()[0]), (0, "SLOT vm1"))

    def test_all_slots_busy_exits_with_three_and_one_line(self):
        self.put("units", active("vm0", "vm1"))
        code, out, err = self.run_ctl("start", limit=2)
        self.assertEqual((code, out), (3, ""))
        self.assertEqual(len(err.splitlines()), 1)
        self.assertIn("busy", err)
        self.assertEqual(len(self.calls()), 1)  # only the listing, nothing started
        self.assertNotIn("start agino-store-session@vm0.service", self.calls())

    def test_default_limit_is_two_slots(self):
        self.put("units", active("vm0", "vm1"))
        code, _, _ = self.run_ctl("start")
        self.assertEqual(code, 3)
        self.put("units", active("vm0"))
        code, out, _ = self.run_ctl("start")
        self.assertEqual((code, out.splitlines()[0]), (0, "SLOT vm1"))

    def test_slots_outside_the_limit_are_not_used(self):
        # vm0 is free, but a unit beyond the limit does not count and a slot beyond the limit is never picked
        self.put("units", active("vm1", "vm5"))
        code, out, _ = self.run_ctl("start", limit=2)
        self.assertEqual((code, out.splitlines()[0]), (0, "SLOT vm0"))
        self.put("units", active("vm0", "vm1"))
        self.assertEqual(self.run_ctl("start", limit=2)[0], 3)

    def test_other_units_do_not_occupy_slots(self):
        self.put("units", "other.service loaded active running y\n"
                          "agino-store-session@@evil.service loaded active running z\n"
                          "agino-store-session@demo.service loaded active running z\n"
                          "agino-store-session@vm10.service loaded active running z\n"
                          + active("vm1"))
        code, out, _ = self.run_ctl("start", limit=2)
        self.assertEqual((code, out.splitlines()[0]), (0, "SLOT vm0"))

    def test_same_slot_is_not_allocated_twice(self):
        # the second start sees the first one's unit as active (as systemd would report it)
        code, out, _ = self.run_ctl("start", limit=2)
        self.assertEqual((code, out.splitlines()[0]), (0, "SLOT vm0"))
        self.put("units", active("vm0"))
        code, out, _ = self.run_ctl("start", limit=2)
        self.assertEqual((code, out.splitlines()[0]), (0, "SLOT vm1"))
        self.put("units", active("vm0", "vm1"))
        code, out, _ = self.run_ctl("start", limit=2)
        self.assertEqual((code, out), (3, ""))
        started = [c for c in self.calls() if c.startswith("start ")]
        self.assertEqual(started, ["start " + unit("vm0"), "start " + unit("vm1")])

    def test_timeout_stops_the_unit(self):
        self.daemon(token=None)
        code, out, err = self.run_ctl("start")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(self.calls()[-1], "stop agino-store-session@vm0.service")

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_unlink_failure_stops_the_unit_and_is_a_clean_refusal(self):
        self.put("readonly-dir", "")
        self.addCleanup(lambda: os.path.isdir(os.path.join(self.fake, "rt", "vm0"))
                        and os.chmod(os.path.join(self.fake, "rt", "vm0"), 0o700))
        code, out, err = self.run_ctl("start")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("agino-session-ctl: cannot collect the session token", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.calls()[-1], "stop agino-store-session@vm0.service")

    def test_stale_files_of_an_earlier_run_are_not_returned(self):
        os.makedirs(os.path.join(self.fake, "rt", "vm0"))
        for name, text in (("token.once", "f" * 64 + "\n"), ("session.id", "f" * 16 + "\n")):
            with open(os.path.join(self.fake, "rt", "vm0", name), "w") as f:
                f.write(text)
        self.daemon(token=None, session_id=None)
        code, out, _ = self.run_ctl("start")
        self.assertEqual((code, out), (1, ""))
        self.assertFalse(os.path.exists(os.path.join(self.fake, "rt", "vm0", "token.once")))
        self.assertFalse(os.path.exists(os.path.join(self.fake, "rt", "vm0", "session.id")))

    def test_odd_token_files_are_refused(self):
        for token in ("zz" * 32 + "\n", "ab" * 31 + "\n", "ab" * 33 + "\n", "AB" * 32 + "\n", TOKEN + "\nextra\n",
                      "../../etc/shadow\n", ""):
            self.daemon(token=token)
            code, out, _ = self.run_ctl("start")
            self.assertEqual((code, out), (1, ""), token)

    def test_odd_session_ids_are_refused(self):
        for sid in ("short\n", "0123456789ABCDEF\n", SID + "0\n", "../etc/passwd!!!!\n"):
            self.daemon(session_id=sid)
            code, out, _ = self.run_ctl("start")
            self.assertEqual((code, out), (1, ""), sid)

    def test_token_symlink_is_refused(self):
        secret = os.path.join(self.fake, "secret")
        with open(secret, "w") as f:
            f.write(TOKEN + "\n")
        os.makedirs(os.path.join(self.fake, "rt", "vm0"))
        self.daemon(token=None)

        # the fake daemon creates a symlink instead of a file
        self.put("token", TOKEN + "\n")
        path = os.path.join(self.fake, "bin", "systemctl")
        with open(path) as f:
            script = f.read()
        script = script.replace('cp "$FAKE/token" "$FAKE/rt/$slot/token.once"', 'ln -s "$FAKE/secret" "$FAKE/rt/$slot/token.once"')
        with open(path, "w") as f:
            f.write(script)
        code, out, err = self.run_ctl("start")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(self.calls()[-1], "stop agino-store-session@vm0.service")

    def test_oversized_token_file_is_refused(self):
        self.daemon(token="a" * 5000)
        code, out, _ = self.run_ctl("start")
        self.assertEqual((code, out), (1, ""))

    def test_systemctl_failures(self):
        self.put("start-fails", "")
        code, out, _ = self.run_ctl("start")
        self.assertEqual((code, out), (1, ""))
        os.unlink(os.path.join(self.fake, "start-fails"))
        os.unlink(os.path.join(self.fake, "calls"))
        self.put("list-fails", "")
        code, out, _ = self.run_ctl("start")
        self.assertEqual((code, out), (1, ""))
        self.assertNotIn("start agino-store-session@vm0.service", self.calls())

    def test_lock_file_is_private(self):
        self.run_ctl("start")
        mode = stat.S_IMODE(os.stat(os.path.join(self.fake, "rt", ".lock")).st_mode)
        self.assertEqual(mode, 0o600)


class StopTests(CtlCase):
    def test_stop(self):
        code, out, _ = self.run_ctl("stop", "vm1")
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(self.calls(), ["stop agino-store-session@vm1.service"])

    def test_stop_failure(self):
        self.put("stop-fails", "")
        self.assertEqual(self.run_ctl("stop", "vm0")[0], 1)


if __name__ == "__main__":
    unittest.main()
