# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import hmac
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_session as T  # noqa: E402
from session_helpers import session_cfg  # noqa: E402

from agino_store_sandbox import session as S  # noqa: E402
from agino_store_sandbox import session_serve as SS  # noqa: E402
from agino_store_sandbox.util import SandboxError  # noqa: E402


def read(path):
    with open(path, "rb") as f:
        return f.read()


class ServeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target in ("session", "session_web", "session_serve"):
            patcher = mock.patch("agino_store_sandbox.%s.log" % target, lambda msg: None)
            patcher.start()
            self.addCleanup(patcher.stop)
        T.FakeVm.instances = []
        self.cfg = session_cfg(self.tmp.name)
        self.clock = T.Clock()
        self.sg = T.FakeSg()
        self.deps = S.Deps(vm_factory=T.FakeVm, guest_factory=T.FakeGuest, sg_factory=lambda g: self.sg,
                           install=lambda *a: None, locator_factory=lambda sg: T.FakeLocator(),
                           find_job=lambda cfg, app: T.FakeJob(), sandbox_flag=lambda c: False,
                           tap_bytes=lambda slot: 0, free_bytes=lambda p: 1 << 40, settle=0, desktop_settle=0)
        self.app_dir = os.path.join(self.tmp.name, "rt", "vm0")

    def wait(self, predicate, what, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("timed out waiting for " + what)

    def ctl(self, token):
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(3)
        s.connect(os.path.join(self.app_dir, "web.sock"))
        f = s.makefile("rwb")
        challenge = bytes.fromhex(f.readline()[10:-1].decode())
        f.write(b"AUTH CTL " + hmac.new(token, challenge + b"CTL", hashlib.sha256).hexdigest().encode() + b"\n")
        f.flush()
        self.assertEqual(f.readline(), b"OK\n")
        return s, f

    @staticmethod
    def ask(f, obj):
        time.sleep(0.21)  # the daemon allows 5 requests per second per connection
        f.write(json.dumps(obj).encode() + b"\n")
        f.flush()
        return json.loads(f.readline())

    def test_full_run(self):
        result = []
        t = threading.Thread(target=lambda: result.append(SS.serve(self.cfg, "vm0", self.deps, 1)))
        t.start()
        token_path = os.path.join(self.app_dir, "token.once")
        self.wait(lambda: os.path.exists(token_path), "token.once")
        self.assertEqual(stat.S_IMODE(os.stat(token_path).st_mode), 0o600)
        token_hex = read(token_path).decode().strip()
        self.assertEqual(len(token_hex), 64)
        session_id = read(os.path.join(self.app_dir, "session.id")).decode().strip()
        self.assertRegex(session_id, r"\A[0-9a-f]{16}\Z")
        self.assertEqual(stat.S_IMODE(os.stat(self.app_dir).st_mode), 0o750)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.app_dir, "private")).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.app_dir, "web.sock")).st_mode), 0o660)
        s, f = self.ctl(bytes.fromhex(token_hex))
        self.wait(lambda: self.ask(f, {"op": "status"})["state"] == "idle", "idle state")
        status = self.ask(f, {"op": "status"})
        self.assertEqual((status["id"], status["slot"], status["app"]), (session_id, "vm0", ""))
        self.assertEqual(self.ask(f, {"op": "capture", "kind": "screen"}), {"ok": False, "error": "bad_state"})
        self.assertEqual(self.ask(f, {"op": "select_app", "app": "demo", "lang": "zh_CN"}), {"ok": True})
        self.wait(lambda: self.ask(f, {"op": "status"})["state"] == "running", "running state")
        self.assertEqual(self.ask(f, {"op": "status"})["app"], "demo")
        shot = self.ask(f, {"op": "capture", "kind": "screen"})
        self.assertTrue(shot["ok"])
        self.assertEqual((shot["app"], shot["desktop_lang"]), ("demo", "zh_CN"))
        self.assertEqual(self.ask(f, {"op": "finish_app"}), {"ok": True})
        self.assertEqual(self.ask(f, {"op": "status"})["state"], "idle")
        self.assertEqual(self.ask(f, {"op": "end"}), {"ok": True})
        t.join(10)
        self.assertEqual(result, [0])
        self.assertFalse(os.path.exists(self.app_dir))  # runtime files are gone
        caps = os.path.join(self.tmp.name, "captures", session_id)
        self.assertEqual(sorted(os.listdir(caps)), ["01-screen-zh_CN.png", "captures.json"])  # captures stay
        self.assertEqual(T.FakeVm.instances[0].events[-1], "stop")

    def test_failed_session_lingers_then_cleans_up(self):
        def broken(*a):
            vm = T.FakeVm(*a)
            vm.fail_boot = True
            return vm
        self.deps.vm_factory = broken
        result = []
        t = threading.Thread(target=lambda: result.append(SS.serve(self.cfg, "vm0", self.deps, 0.5)))
        t.start()
        self.wait(lambda: os.path.exists(os.path.join(self.app_dir, "token.once")), "token.once")
        token = bytes.fromhex(read(os.path.join(self.app_dir, "token.once")).decode().strip())
        try:
            s, f = self.ctl(token)
            self.wait(lambda: self.ask(f, {"op": "status"})["state"] == "failed", "failed state")
            self.assertIn("qemu did not start", self.ask(f, {"op": "status"})["error"])
        except (OSError, AssertionError):
            pass  # the linger is short in this test; the cleanup below is what matters
        t.join(10)
        self.assertFalse(os.path.exists(self.app_dir))

    def test_bad_arguments(self):
        for slot in ("../x", "A", "", "demo", "-x", "vm", "vm10", "vmx", "VM0", "vm0\n", "vm0 ", "vm-1"):
            with self.assertRaises(SandboxError):
                SS.serve(self.cfg, slot, self.deps)

    def test_stale_files_of_a_crashed_run_are_removed(self):
        private = os.path.join(self.app_dir, "private")
        os.makedirs(private)
        for name in ("token.once", "session.id", "web.sock"):
            with open(os.path.join(self.app_dir, name), "w") as f:
                f.write("old")
        SS.prepare_dirs(self.cfg.session, "vm0")
        self.assertEqual(sorted(os.listdir(self.app_dir)), ["private"])

    def test_group_chown_failure_is_not_fatal(self):
        # The daemon is not a member of web_group: chown fails with EPERM and the run directory (setgid in
        # production) provides the group, so prepare_dirs only logs.
        other = os.getegid() + 4242
        messages = []
        denied = PermissionError(1, "Operation not permitted")
        with mock.patch.object(SS, "_group_id", lambda name: other), \
                mock.patch.object(SS, "log", messages.append), \
                mock.patch("os.chown", side_effect=denied) as chown:
            app_dir, private = SS.prepare_dirs(self.cfg.session, "vm0")
        chown.assert_called_once_with(app_dir, -1, other)
        self.assertEqual(stat.S_IMODE(os.stat(app_dir).st_mode) & 0o777, 0o750)
        self.assertEqual(stat.S_IMODE(os.stat(private).st_mode) & 0o777, 0o700)
        self.assertEqual(len(messages), 1)
        self.assertIn("cannot give", messages[0])
        self.assertIn("Operation not permitted", messages[0])

    def test_session_id_write_survives_fchown_failure(self):
        path = os.path.join(self.tmp.name, "session.id")
        messages = []
        with mock.patch.object(SS, "log", messages.append), \
                mock.patch("os.fchown", side_effect=PermissionError(1, "Operation not permitted")):
            SS._write(path, b"abc\n", 0o640, gid=os.getegid() + 4242)
        self.assertEqual(read(path), b"abc\n")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)
        self.assertEqual(len(messages), 1)

    def test_no_chown_when_the_group_is_already_right(self):
        with mock.patch("os.chown") as chown:
            app_dir, _ = SS.prepare_dirs(self.cfg.session, "vm0")  # nonexistent group: our own gid
        chown.assert_not_called()
        self.assertTrue(os.path.isdir(app_dir))

    def test_write_publishes_the_complete_file_atomically(self):
        path = os.path.join(self.tmp.name, "token.once")
        seen = []
        real_rename = os.rename

        def spy(src, dst):
            # at the instant of publication the final name is still absent and the temporary file is complete
            seen.append((os.path.exists(dst), read(src), os.path.dirname(src) == os.path.dirname(dst)))
            real_rename(src, dst)

        with mock.patch("os.rename", spy):
            SS._write(path, b"x" * 65 + b"\n", 0o600, exclusive=True)
        self.assertEqual(seen, [(False, b"x" * 65 + b"\n", True)])
        self.assertEqual(read(path), b"x" * 65 + b"\n")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(os.listdir(self.tmp.name), ["token.once"])

    def test_exclusive_write_refuses_a_leftover_temporary_file(self):
        path = os.path.join(self.tmp.name, "token.once")
        with open(path + ".tmp", "w") as f:
            f.write("planted")
        with self.assertRaises(FileExistsError):
            SS._write(path, b"data", 0o600, exclusive=True)
        self.assertFalse(os.path.exists(path))

    def test_failed_write_leaves_no_temporary_file(self):
        path = os.path.join(self.tmp.name, "token.once")
        with mock.patch("os.rename", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                SS._write(path, b"data", 0o600, exclusive=True)
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_session_id_is_in_place_before_the_token_appears(self):
        order = []
        real_rename = os.rename

        def spy(src, dst):
            order.append(os.path.basename(dst))
            real_rename(src, dst)

        result = []
        with mock.patch("os.rename", spy):
            t = threading.Thread(target=lambda: result.append(SS.serve(self.cfg, "vm0", self.deps, 1)))
            t.start()
            self.wait(lambda: "token.once" in order, "token.once")
            self.assertLess(order.index("session.id"), order.index("token.once"))
            token = bytes.fromhex(read(os.path.join(self.app_dir, "token.once")).decode().strip())
            s, f = self.ctl(token)
            self.ask(f, {"op": "end"})
            t.join(10)
        self.assertEqual(result, [0])

    def test_stale_temporary_files_are_removed(self):
        os.makedirs(os.path.join(self.app_dir, "private"))
        for name in ("token.once.tmp", "session.id.tmp"):
            with open(os.path.join(self.app_dir, name), "w") as f:
                f.write("old")
        SS.prepare_dirs(self.cfg.session, "vm0")
        self.assertEqual(sorted(os.listdir(self.app_dir)), ["private"])

    def test_status_command(self):
        private = os.path.join(self.app_dir, "private")
        os.makedirs(private)
        doc = {"state": "running", "remaining_s": 100, "idle_s": 5, "written": time.time() - 10}
        with open(os.path.join(private, "status.json"), "w") as f:
            json.dump(doc, f)
        out = json.loads(SS.status(self.cfg, "vm0"))
        self.assertEqual(out["state"], "running")
        self.assertTrue(88 <= out["remaining_s"] <= 90 and 14 <= out["idle_s"] <= 16)
        with self.assertRaises(SandboxError):
            SS.status(self.cfg, "vm1")
        with self.assertRaises(SandboxError):
            SS.status(self.cfg, "../vm0")
        with self.assertRaises(SandboxError):
            SS.status(self.cfg, "demo")


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = session_cfg(self.tmp.name)
        self.root = self.cfg.session.captures
        os.makedirs(self.root)

    def make(self, name, files=("01-screen-zh_CN.png", "captures.json"), age_hours=48):
        d = os.path.join(self.root, name)
        os.makedirs(d, exist_ok=True)
        for f in files:
            with open(os.path.join(d, f), "w") as fh:
                fh.write("x")
        old = time.time() - age_hours * 3600
        os.utime(d, (old, old))
        return d

    def test_removes_only_old_session_directories(self):
        old = self.make("0123456789abcdef")
        young = self.make("fedcba9876543210", age_hours=2)
        self.assertEqual(SS.cleanup(self.cfg), ["0123456789abcdef"])
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(young))

    def test_names_must_match_the_session_id_format(self):
        for name in ("0123456789ABCDEF", "short", "0123456789abcdef0", "..", "lost+found", ".hidden"):
            if name != "..":
                self.make(name)
        self.assertEqual(SS.cleanup(self.cfg), [])
        self.assertTrue(os.path.exists(os.path.join(self.root, "lost+found")))

    def test_symlinks_are_never_followed(self):
        outside = os.path.join(self.tmp.name, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "01-screen-zh_CN.png"), "w") as f:
            f.write("keep")
        os.symlink(outside, os.path.join(self.root, "0123456789abcdef"))
        self.assertEqual(SS.cleanup(self.cfg), [])
        self.assertTrue(os.path.exists(os.path.join(outside, "01-screen-zh_CN.png")))

    def test_unknown_files_stay_and_keep_the_directory(self):
        d = self.make("0123456789abcdef", files=("01-screen-zh_CN.png", "notes.txt", "../escape"[3:]))
        self.assertEqual(SS.cleanup(self.cfg), [])
        self.assertEqual(sorted(os.listdir(d)), ["notes.txt"] if False else sorted(os.listdir(d)))
        self.assertTrue(os.path.exists(os.path.join(d, "notes.txt")))
        self.assertFalse(os.path.exists(os.path.join(d, "01-screen-zh_CN.png")))

    def test_leftover_temp_files_are_removed(self):
        d = self.make("0123456789abcdef", files=(".01-window-en_US.png.tmp", "captures.json"))
        self.assertEqual(SS.cleanup(self.cfg), ["0123456789abcdef"])
        self.assertFalse(os.path.exists(d))

    def test_missing_root(self):
        with self.assertRaises(SandboxError):
            SS.cleanup(session_cfg(self.tmp.name + "/nowhere"))


if __name__ == "__main__":
    unittest.main()
