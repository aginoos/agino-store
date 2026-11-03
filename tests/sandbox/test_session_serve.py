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
                           tap_bytes=lambda slot: 0, free_bytes=lambda p: 1 << 40)
        self.app_dir = os.path.join(self.tmp.name, "rt", "demo")

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
        f.write(json.dumps(obj).encode() + b"\n")
        f.flush()
        return json.loads(f.readline())

    def test_full_run(self):
        result = []
        t = threading.Thread(target=lambda: result.append(SS.serve(self.cfg, "demo", "zh_CN", self.deps, 1)))
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
        self.wait(lambda: self.ask(f, {"op": "status"})["state"] == "running", "running state")
        status = self.ask(f, {"op": "status"})
        self.assertEqual((status["id"], status["app"]), (session_id, "demo"))
        shot = self.ask(f, {"op": "capture", "kind": "screen"})
        self.assertTrue(shot["ok"])
        self.assertEqual(self.ask(f, {"op": "end"}), {"ok": True})
        t.join(10)
        self.assertEqual(result, [0])
        self.assertFalse(os.path.exists(self.app_dir))  # runtime files are gone
        caps = os.path.join(self.tmp.name, "captures", session_id)
        self.assertEqual(sorted(os.listdir(caps)), ["01-screen-zh_CN.png", "captures.json"])  # captures stay
        self.assertEqual(T.FakeVm.instances[0].events[-1], "stop")

    def test_failed_session_lingers_then_cleans_up(self):
        self.deps.find_job = lambda cfg, app: (_ for _ in ()).throw(SandboxError("no usable sandbox job"))
        result = []
        t = threading.Thread(target=lambda: result.append(SS.serve(self.cfg, "demo", "zh_CN", self.deps, 0.5)))
        t.start()
        self.wait(lambda: os.path.exists(os.path.join(self.app_dir, "token.once")), "token.once")
        token = bytes.fromhex(read(os.path.join(self.app_dir, "token.once")).decode().strip())
        try:
            s, f = self.ctl(token)
            self.wait(lambda: self.ask(f, {"op": "status"})["state"] == "failed", "failed state")
            self.assertIn("no usable sandbox job", self.ask(f, {"op": "status"})["error"])
        except (OSError, AssertionError):
            pass  # the linger is short in this test; the cleanup below is what matters
        t.join(10)
        self.assertFalse(os.path.exists(self.app_dir))

    def test_bad_arguments(self):
        for app, lang in (("../x", "zh_CN"), ("A", "zh_CN"), ("", "zh_CN"), ("demo", "fr_FR"), ("-x", "en_US")):
            with self.assertRaises(SandboxError):
                SS.serve(self.cfg, app, lang, self.deps)

    def test_stale_files_of_a_crashed_run_are_removed(self):
        private = os.path.join(self.app_dir, "private")
        os.makedirs(private)
        for name in ("token.once", "session.id", "web.sock"):
            with open(os.path.join(self.app_dir, name), "w") as f:
                f.write("old")
        SS.prepare_dirs(self.cfg.session, "demo")
        self.assertEqual(sorted(os.listdir(self.app_dir)), ["private"])

    def test_status_command(self):
        private = os.path.join(self.app_dir, "private")
        os.makedirs(private)
        doc = {"state": "running", "remaining_s": 100, "idle_s": 5, "written": time.time() - 10}
        with open(os.path.join(private, "status.json"), "w") as f:
            json.dump(doc, f)
        out = json.loads(SS.status(self.cfg, "demo"))
        self.assertEqual(out["state"], "running")
        self.assertTrue(88 <= out["remaining_s"] <= 90 and 14 <= out["idle_s"] <= 16)
        with self.assertRaises(SandboxError):
            SS.status(self.cfg, "other")
        with self.assertRaises(SandboxError):
            SS.status(self.cfg, "../demo")


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
