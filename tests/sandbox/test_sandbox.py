# SPDX-License-Identifier: GPL-3.0-or-later
import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server"))
from agino_store_sandbox import config, guest, jobs, profile, qga, qmp, report, vm  # noqa: E402
from agino_store_sandbox.util import InfraError, SandboxError  # noqa: E402

HAVE_OPENSSL = shutil.which("openssl") is not None


class FakeServer:
    """Unix socket server answering from a handler(request_dict) -> list of reply lines (dict or bytes)."""

    def __init__(self, path, handler, greeting=None):
        self.path, self.handler, self.greeting = path, handler, greeting
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(1)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        conn, _ = self.srv.accept()
        with conn:
            if self.greeting:
                conn.sendall(json.dumps(self.greeting).encode() + b"\n")
            buf = b""
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    req = json.loads(line.lstrip(b"\xff"))
                    for reply in self.handler(req):
                        conn.sendall((reply if isinstance(reply, bytes) else json.dumps(reply).encode()) + b"\n")

    def close(self):
        self.srv.close()


class SockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sock = os.path.join(self.tmp.name, "s")


class QmpTests(SockTest):
    def test_events_are_skipped_and_errors_raise(self):
        def handler(req):
            if req["execute"] == "qmp_capabilities":
                return [{"return": {}}]
            if req["execute"] == "screendump":
                self.assertEqual(req["arguments"], {"filename": "/x/shot.png", "format": "png"})
                return [{"event": "STOP"}, {"return": {}}]
            return [{"error": {"class": "GenericError", "desc": "nope"}}]
        srv = FakeServer(self.sock, handler, {"QMP": {"version": {}}})
        c = qmp.Qmp(self.sock)
        c.screendump("/x/shot.png")
        with self.assertRaises(InfraError):
            c.execute("bogus")
        c.close()
        srv.close()

    def test_missing_greeting(self):
        srv = FakeServer(self.sock, lambda r: [], {"hello": 1})
        with self.assertRaises(InfraError):
            qmp.Qmp(self.sock)
        srv.close()


class QgaTests(SockTest):
    def test_sync_exec_and_output(self):
        state = {"polls": 0}

        def handler(req):
            cmd, args = req["execute"], req.get("arguments", {})
            if cmd == "guest-sync":
                return [b"\xff" + json.dumps({"return": 111}).encode(), {"return": args["id"]}]
            if cmd == "guest-exec":
                self.assertEqual(args["path"], "/usr/bin/timeout")
                self.assertEqual(args["arg"][:4], ["-k", "10", "5", "/bin/sh"])
                self.assertEqual(base64.b64decode(args["input-data"]), b"in")
                return [{"return": {"pid": 7}}]
            state["polls"] += 1
            if state["polls"] < 2:
                return [{"return": {"exited": False}}]
            return [{"return": {"exited": True, "exitcode": 0,
                                "out-data": base64.b64encode(b"hello").decode(), "err-data": "!!notb64"}}]
        srv = FakeServer(self.sock, handler)
        c = qga.Qga(self.sock)
        c.wait_ready(10)
        res = c.sh("echo hi", ["a b"], timeout=5, input_data=b"in")
        self.assertTrue(res.ok)
        self.assertEqual((res.out, res.err), (b"hello", b""))
        c.close()
        srv.close()

    def test_timeout_exit_code_is_flagged(self):
        def handler(req):
            if req["execute"] == "guest-exec":
                return [{"return": {"pid": 1}}]
            return [{"return": {"exited": True, "exitcode": 124}}]
        srv = FakeServer(self.sock, handler)
        res = qga.Qga(self.sock).exec(["/bin/true"], 1)
        self.assertTrue(res.timed_out and not res.ok)
        srv.close()

    def test_guest_error_raises(self):
        srv = FakeServer(self.sock, lambda r: [{"error": {"desc": "denied"}}])
        with self.assertRaises(InfraError):
            qga.Qga(self.sock).start(["/bin/true"])
        srv.close()


def make_job(queue, app="demo", deb=b"debdata", **over):
    sha = hashlib.sha256(deb).hexdigest()
    stem = "%s_%s" % (app, sha[:12])
    doc = {"schema": 1, "app": app, "version": "1.0-1", "sha256": sha, "size": len(deb), "package": app,
           "filename": "x.deb", "deb": stem + ".deb", "launchable": None, "install_profile": None,
           "queued": "2026-11-02T00:00:00Z"}
    doc.update(over)
    with open(os.path.join(queue, stem + ".deb"), "wb") as f:
        f.write(deb)
    with open(os.path.join(queue, stem + ".json"), "w") as f:
        json.dump(doc, f)
    return stem


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = os.path.join(self.tmp.name, "q")
        self.state = os.path.join(self.tmp.name, "s")
        os.mkdir(self.queue)

    def test_read_valid_job(self):
        stem = make_job(self.queue, launchable="demo", install_profile={
            "debconf": ["demo demo/x boolean false"],
            "default_files": [{"path": "/etc/default/demo", "content": "a=1\n"}]})
        job = jobs.read_job(self.queue, stem + ".json")
        self.assertEqual((job.app, job.launchable, job.debconf), ("demo", "demo.desktop", ["demo demo/x boolean false"]))
        self.assertEqual(job.default_files, [("/etc/default/demo", b"a=1\n")])

    def test_rejects_bad_jobs(self):
        stem = make_job(self.queue)
        with open(os.path.join(self.queue, stem + ".deb"), "ab") as f:
            f.write(b"x")
        with self.assertRaises(jobs.JobError):
            jobs.read_job(self.queue, stem + ".json")
        with self.assertRaises(jobs.JobError):
            jobs.read_job(self.queue, "../etc_passwd.json")
        bad = make_job(self.queue, app="other", package="Bad Pkg")
        with self.assertRaises(jobs.JobError):
            jobs.read_job(self.queue, bad + ".json")

    def test_hash_mismatch(self):
        stem = make_job(self.queue)
        with open(os.path.join(self.queue, stem + ".deb"), "wb") as f:
            f.write(b"DEBDATA")  # same size, other content
        with self.assertRaises(jobs.JobError):
            jobs.read_job(self.queue, stem + ".json")

    def test_selection_and_state(self):
        a, b, c = (make_job(self.queue, app=n, deb=n.encode()) for n in ("aa", "bb", "cc"))
        open(os.path.join(self.queue, "notes.txt"), "w").close()
        self.assertEqual(jobs.pending(self.queue, self.state), sorted(s + ".json" for s in (a, b, c)))
        jobs.write_state(self.state, a, {"status": "done", "attempts": 1})
        jobs.write_state(self.state, b, {"status": "error", "attempts": 2})
        jobs.write_state(self.state, c, {"status": "error", "attempts": 3})
        self.assertEqual(jobs.pending(self.queue, self.state), [b + ".json"])
        self.assertEqual(jobs.pending(self.queue, self.state, only=a), [a + ".json"])
        self.assertEqual(jobs.pending(self.queue, self.state, only="zz_000000000000"), [])
        self.assertEqual(jobs.previous_attempts(self.state, b), 2)
        self.assertEqual(jobs.previous_attempts(self.state, a), 0)


class ProfileTests(unittest.TestCase):
    def test_valid(self):
        lines, files = profile.validate({"debconf": ["code code/add-repo boolean false", "o q string some value"],
                                         "default_files": [{"path": "/etc/default/a.b+c", "content": "x"}]})
        self.assertEqual((len(lines), len(files)), (2, 1))
        self.assertEqual(profile.validate(None), ([], []))

    def test_invalid(self):
        bad = [{"debconf": ["o q password x"]}, {"debconf": ["o q string a\nb"]}, {"debconf": ["o q string"]},
               {"debconf": [5]}, {"default_files": [{"path": "/etc/passwd", "content": ""}]},
               {"default_files": [{"path": "/etc/default/../x", "content": ""}]},
               {"default_files": [{"path": "/etc/default/A", "content": ""}]},
               {"default_files": [{"path": "/etc/default/a", "content": "x" * 4097}]},
               {"default_files": [{"path": "/etc/default/a", "content": "a\0b"}]}, {"other": 1}, "str"]
        for profile_doc in bad:
            with self.assertRaises(profile.ProfileError, msg=repr(profile_doc)):
                profile.validate(profile_doc)


class GuestParseTests(unittest.TestCase):
    def test_listing_keeps_only_watched_paths(self):
        raw = b"/etc/apt/keyrings/a.gpg\0/etc/apt/keyrings/../../passwd\0/etc/shadow\0/etc/cron.d/x\ny\0/etc/cron.d/ok\0"
        self.assertEqual(guest.parse_listing(raw, guest.WATCH_DIRS), {"/etc/apt/keyrings/a.gpg", "/etc/cron.d/ok"})

    def test_env_and_windows_and_cursor(self):
        env = guest.parse_env("DISPLAY=:0\nDBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus\nEVIL=1\nXAUTHORITY=a\x01b\n")
        self.assertEqual(env, {"DISPLAY": ":0", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"})
        self.assertIsNone(guest.parse_env("DISPLAY=:0\n"))
        self.assertEqual(guest.parse_windows("_NET_CLIENT_LIST(WINDOW): window id # 0x1a00003, 0x2c00005"),
                         {"0x1a00003", "0x2c00005"})
        self.assertEqual(guest.parse_cursor("-- cursor: s=abc;i=1;b=2\n"), "s=abc;i=1;b=2")
        self.assertIsNone(guest.parse_cursor("-- cursor: $(rm)\n"))


class FakeQga:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def sh(self, script, args=(), timeout=60, input_data=None):
        self.calls.append(("sh", list(args), input_data))
        return self.handler(script, list(args), input_data)

    def exec(self, argv, timeout, input_data=None):
        self.calls.append(("exec", list(argv), input_data))
        return self.handler(None, list(argv), input_data)


class GuestStepTests(unittest.TestCase):
    def test_apply_profile_feeds_stdin_and_never_interpolates(self):
        fake = FakeQga(lambda s, a, i: qga.ExecResult(0, b"", b""))
        job = jobs.Job("s", "demo", "1", "0" * 64, 1, "demo", None, ["o q boolean false"],
                       [("/etc/default/demo", b"a=1\n")], "/x")
        self.assertIsNone(guest.Guest(fake).apply_profile(job))
        self.assertEqual(fake.calls[0], ("exec", ["/usr/bin/debconf-set-selections"], b"o q boolean false\n"))
        self.assertEqual(fake.calls[1][1:], (["/etc/default/demo"], b"a=1\n"))

    def test_remove_unowned_only_passes_paths_in_apt_dirs(self):
        fake = FakeQga(lambda s, a, i: qga.ExecResult(0, a[0].encode() + b"\0", b""))
        removed = guest.Guest(fake).remove_unowned({"/etc/apt/sources.list.d/x.list", "/etc/cron.d/y"})
        self.assertEqual(removed, ["/etc/apt/sources.list.d/x.list"])
        self.assertEqual(fake.calls[0][1], ["/etc/apt/sources.list.d/x.list"])


@unittest.skipUnless(HAVE_OPENSSL, "openssl missing")
class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.key = os.path.join(self.tmp.name, "k.pem")
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", self.key], check=True, capture_output=True)
        os.chmod(self.key, 0o600)
        self.job = jobs.Job("demo_aaaaaaaaaaaa", "demo", "1.0-1", "a" * 64, 1, "demo", None, [], [], "/x")
        self.good = {"installed": True, "third_party_sources": [], "desktop_file": "demo.desktop",
                     "window_seen": True, "new_errors": 3, "removed_clean": True}

    def verify(self, data, sig_b64):
        der = subprocess.run(["openssl", "pkey", "-in", self.key, "-pubout", "-outform", "DER"],
                             check=True, capture_output=True).stdout
        d = self.tmp.name
        for name, content in (("pub.der", der), ("data", data), ("sig", base64.b64decode(sig_b64))):
            with open(os.path.join(d, name), "wb") as f:
                f.write(content)
        return subprocess.run(["openssl", "pkeyutl", "-verify", "-rawin", "-pubin", "-keyform", "DER", "-inkey",
                               os.path.join(d, "pub.der"), "-in", os.path.join(d, "data"), "-sigfile",
                               os.path.join(d, "sig")], capture_output=True).returncode == 0

    def test_pass_and_fail_rules(self):
        doc = report.build(self.job, self.good, {}, [], "t0", "t1", host="h")
        self.assertEqual(doc["status"], "pass")
        self.assertEqual(doc["host"], "h")
        for name, value in (("installed", False), ("third_party_sources", ["/etc/apt/x.list"]),
                            ("desktop_file", None), ("window_seen", False), ("removed_clean", False)):
            doc = report.build(self.job, dict(self.good, **{name: value}), {}, [], "t0", "t1")
            self.assertEqual(doc["status"], "fail", name)
            self.assertIn(name, doc["summary"])

    def test_failure_report(self):
        doc = report.build_failure(self.job, "VM did not boot", "t0", "t1")
        self.assertEqual(doc["status"], "fail")
        self.assertTrue(doc["summary"].startswith("sandbox could not run: VM did not boot"))

    def test_write_is_signed_and_verifiable(self):
        out = os.path.join(self.tmp.name, "reports")
        doc = report.build(self.job, self.good, {"x": "y"}, [], "t0", "t1")
        name = report.write(out, self.key, "demo_aaaaaaaaaaaa", doc)
        self.assertEqual(name, "demo_aaaaaaaaaaaa.json")
        with open(os.path.join(out, name), "rb") as f:
            data = f.read()
        with open(os.path.join(out, name + ".sig")) as f:
            sig = f.read()
        self.assertTrue(self.verify(data, sig))
        self.assertFalse(self.verify(data + b" ", sig))
        self.assertEqual(json.loads(data)["status"], "pass")
        self.assertEqual(sorted(os.listdir(out)), [name, name + ".sig"])

    def test_key_permissions_checked(self):
        os.chmod(self.key, 0o640)
        with self.assertRaises(SandboxError):
            report.sign(self.key, b"x")


class RunFilesTests(unittest.TestCase):
    def test_cleanup_only_removes_created_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = vm.RunFiles(os.path.join(tmp, "0"))
            files.prepare()
            for name in ("overlay.qcow2", "qmp.sock"):
                open(files.path(name), "w").close()
            other = os.path.join(files.directory, "keep.me")
            open(other, "w").close()
            open(os.path.join(files.directory, "vars.fd"), "w").close()  # never registered as created
            files.cleanup()
            self.assertEqual(sorted(os.listdir(files.directory)), ["keep.me", "vars.fd"])
            with self.assertRaises(ValueError):
                files.path("../escape")

    def test_prepare_removes_stale_fixed_names_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = vm.RunFiles(tmp)
            for name in ("overlay.qcow2", "other.txt"):
                open(os.path.join(tmp, name), "w").close()
            files.prepare()
            self.assertEqual(os.listdir(tmp), ["other.txt"])


class QemuArgvTests(unittest.TestCase):
    def test_argv(self):
        cfg = config.from_dict({})
        files = vm.RunFiles("/run/0")
        argv = vm.build_argv(cfg, 2, files, "/q/x.deb", True)
        joined = " ".join(argv)
        for want in ("ifname=agsbx-t2,script=no,downscript=no", "mac=52:54:00:5b:00:02", "serial=sbxdeb",
                     "readonly=on,id=deb", "org.qemu.guest_agent.0", "-nodefaults", "-sandbox on"):
            self.assertIn(want, joined)
        self.assertNotIn("user", " ".join(a for a in argv if a.startswith("-netdev")))
        self.assertNotIn("serial=sbxdeb", " ".join(vm.build_argv(cfg, 0, files, None, False)))


class ConfigTests(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(config.from_dict({}).slots, 4)
        for bad in ({"slots": "9"}, {"qemu_sandbox": "maybe"}, {"cpus": "x"}, {"nonsense": "1"}):
            with self.assertRaises(SandboxError):
                config.from_dict(bad)


if __name__ == "__main__":
    unittest.main()
