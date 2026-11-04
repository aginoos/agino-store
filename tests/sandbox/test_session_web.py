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

import session_helpers  # noqa: E402,F401

from agino_store_sandbox import session_web as W  # noqa: E402
from agino_store_sandbox.session import OpError  # noqa: E402
from test_rfb_filter import HANDSHAKE, key, pointer, update_request  # noqa: E402

TOKEN = bytes(range(32))


class FakeSession:
    def __init__(self):
        self.vnc = None
        self.inputs = 0
        self.calls = []
        self.png = b"\x89PNG" + bytes(range(256)) * 1200
        self.fail_with = None

    def vnc_path(self):
        return self.vnc

    def touch_input(self, n=1):
        self.inputs += n

    def status(self):
        return {"ok": True, "state": "running", "id": "0123456789abcdef", "slot": "vm0", "app": "demo",
                "lang": "zh_CN", "desktop_lang": "zh_CN", "remaining_s": 1700, "idle_s": 3, "captures": 0,
                "extended": False, "error": ""}

    def _maybe_fail(self):
        if self.fail_with:
            raise OpError(self.fail_with)

    def op_capture(self, kind):
        self._maybe_fail()
        self.calls.append(("capture", kind))
        return {"n": 1, "file": "01-%s-zh_CN.png" % kind, "kind": kind, "lang": "zh_CN", "width": 1280,
                "height": 720, "size": 10, "sha256": "0" * 64, "title": "t", "app": "demo", "desktop_lang": "zh_CN",
                "problems": []}

    def op_captures(self):
        return [{"n": 1, "file": "01-screen-zh_CN.png", "kind": "screen", "lang": "zh_CN", "width": 1, "height": 1,
                 "size": 1, "sha256": "0" * 64, "title": "", "problems": []}]

    def op_get(self, n):
        if n != 4:
            raise OpError("no_such_capture")
        return self.png

    def op_lang(self, lang, desktop=False):
        self._maybe_fail()
        self.calls.append(("lang", lang, desktop))

    def op_select_app(self, app, lang):
        self._maybe_fail()
        self.calls.append(("select_app", app, lang))

    def op_finish_app(self):
        self._maybe_fail()
        self.calls.append(("finish_app",))

    def op_restart(self):
        self.calls.append(("restart",))

    def op_rollback(self):
        self.calls.append(("rollback",))

    def op_extend(self):
        self.calls.append(("extend",))

    def op_end(self):
        self.calls.append(("end",))


class FakeQemu:
    """A minimal RFB-ish server: greets, then echoes what it receives prefixed with 'S:'."""

    def __init__(self, path):
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(path)
        self.srv.listen(4)
        self.received = []
        self.connections = 0
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self.one, args=(c,), daemon=True).start()

    def one(self, c):
        try:
            c.sendall(b"RFB 003.008\n")
            while True:
                data = c.recv(65536)
                if not data:
                    return
                self.received.append(data)
                c.sendall(b"S:" + data)
        except OSError:
            pass
        finally:
            c.close()

    def close(self):
        self.srv.close()


def recv_line(sock, limit=65536):
    buf = b""
    while not buf.endswith(b"\n"):
        c = sock.recv(1)
        if not c:
            break
        buf += c
        assert len(buf) < limit
    return buf


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            break
        buf += c
    return buf


class WebCase(unittest.TestCase):
    def setUp(self):
        quiet = mock.patch.object(W, "log", lambda msg: None)
        quiet.start()
        self.addCleanup(quiet.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "web.sock")
        self.session = FakeSession()
        self.web = W.WebServer(self.session, self.path, TOKEN, "no-such-group-xyz")
        self.web.bind()
        threading.Thread(target=self.web.serve, daemon=True).start()
        self.addCleanup(self.web.close)
        self.qemu = None
        self.socks = []
        self.addCleanup(self.close_socks)

    def close_socks(self):
        for s in self.socks:
            s.close()

    def start_qemu(self):
        path = os.path.join(self.tmp.name, "vnc.sock")
        self.qemu = FakeQemu(path)
        self.addCleanup(self.qemu.close)
        self.session.vnc = path

    def connect(self, timeout=3):
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(timeout)
        s.connect(self.path)
        self.socks.append(s)
        return s

    def login(self, mode=b"CTL", token=TOKEN, sock=None):
        s = sock or self.connect()
        line = recv_line(s)
        self.assertTrue(line.startswith(b"CHALLENGE ") and len(line) == 10 + 32 + 1, line)
        challenge = bytes.fromhex(line[10:-1].decode())
        s.sendall(b"AUTH " + mode + b" " + hmac.new(token, challenge + mode, hashlib.sha256).hexdigest().encode() + b"\n")
        return s

    def ctl(self, request=None, raw=None):
        s = self.login()
        self.assertEqual(recv_line(s), b"OK\n")
        return s

    @staticmethod
    def ask(s, obj):
        s.sendall((json.dumps(obj) if not isinstance(obj, bytes) else obj) + b"\n" if isinstance(obj, bytes)
                  else json.dumps(obj).encode() + b"\n")
        return json.loads(recv_line(s))

    def assertClosed(self, s):
        try:
            self.assertEqual(s.recv(100), b"")
        except ConnectionResetError:
            pass


class AuthTests(WebCase):
    def test_socket_permissions(self):
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o660)

    def test_success(self):
        s = self.login()
        self.assertEqual(recv_line(s), b"OK\n")

    def test_wrong_token(self):
        s = self.login(token=bytes(32))
        self.assertClosed(s)

    def test_mac_is_bound_to_the_mode(self):
        s = self.connect()
        line = recv_line(s)
        challenge = bytes.fromhex(line[10:-1].decode())
        mac = hmac.new(TOKEN, challenge + b"CTL", hashlib.sha256).hexdigest().encode()
        s.sendall(b"AUTH VNC " + mac + b"\n")
        self.assertClosed(s)

    def test_mac_is_bound_to_the_challenge(self):
        a = self.connect()
        first = bytes.fromhex(recv_line(a)[10:-1].decode())
        mac = hmac.new(TOKEN, first + b"CTL", hashlib.sha256).hexdigest().encode()
        b = self.connect()
        second = bytes.fromhex(recv_line(b)[10:-1].decode())
        self.assertNotEqual(first, second)
        b.sendall(b"AUTH CTL " + mac + b"\n")  # replayed from the other connection
        self.assertClosed(b)

    def test_malformed_first_lines(self):
        mac = "0" * 64
        for line in (b"AUTH CTL\n", b"AUTH CTL " + mac.encode() + b" extra\n", b"AUTH  CTL " + mac.encode() + b"\n",
                     b"auth CTL " + mac.encode() + b"\n", b"AUTH CTL " + mac.upper().encode() + b"\n",
                     b"AUTH CTL " + b"0" * 63 + b"\n", b"AUTH CTL " + b"0" * 65 + b"\n", b"AUTH XXX " + mac.encode() + b"\n",
                     b"CTL " + mac.encode() + b"\n", b"\n", b"AUTH CTL " + mac.encode() + b"\r\n", b"\xff\xfe\n"):
            s = self.connect()
            recv_line(s)
            s.sendall(line)
            self.assertClosed(s)

    def test_first_line_size_cap(self):
        s = self.connect()
        recv_line(s)
        s.sendall(b"A" * 129)
        self.assertClosed(s)
        s = self.connect()
        recv_line(s)
        s.sendall(b"A" * 5000)
        self.assertClosed(s)

    def test_first_line_must_arrive_in_time(self):
        old = W.AUTH_DEADLINE
        W.AUTH_DEADLINE = 0.3
        self.addCleanup(setattr, W, "AUTH_DEADLINE", old)
        s = self.connect()
        recv_line(s)
        s.sendall(b"AUTH CT")
        time.sleep(0.6)
        self.assertClosed(s)

    def test_mac_check_uses_constant_time_compare(self):
        self.assertTrue(hmac.compare_digest(W.expected_mac(TOKEN, b"c" * 16, b"CTL"), W.expected_mac(TOKEN, b"c" * 16, b"CTL")))
        self.assertEqual(len(W.expected_mac(TOKEN, b"c" * 16, b"VNC")), 64)

    def test_connection_limit(self):
        socks = [self.connect() for _ in range(W.MAX_CONNECTIONS)]
        for s in socks:
            recv_line(s)
        extra = self.connect()
        self.assertClosed(extra)


class CtlTests(WebCase):
    def test_status(self):
        s = self.ctl()
        r = self.ask(s, {"op": "status"})
        self.assertEqual((r["ok"], r["state"], r["remaining_s"]), (True, "running", 1700))
        self.assertEqual((r["slot"], r["desktop_lang"]), ("vm0", "zh_CN"))

    def no_rate_limit(self):
        old = W.RATE
        W.RATE = 10000
        self.addCleanup(setattr, W, "RATE", old)

    def test_operations(self):
        self.no_rate_limit()
        s = self.ctl()
        r = self.ask(s, {"op": "capture", "kind": "window"})
        self.assertEqual((r["ok"], r["n"], r["file"], r["problems"]), (True, 1, "01-window-zh_CN.png", []))
        self.assertEqual((r["app"], r["desktop_lang"]), ("demo", "zh_CN"))
        r = self.ask(s, {"op": "captures"})
        self.assertEqual((r["ok"], len(r["items"]), r["items"][0]["file"]), (True, 1, "01-screen-zh_CN.png"))
        for obj, call in (({"op": "lang", "lang": "en_US"}, ("lang", "en_US", False)),
                          ({"op": "lang", "lang": "en_US", "desktop": True}, ("lang", "en_US", True)),
                          ({"op": "lang", "lang": "zh_CN", "desktop": False}, ("lang", "zh_CN", False)),
                          ({"op": "select_app", "app": "audacity", "lang": "en_US"}, ("select_app", "audacity", "en_US")),
                          ({"op": "finish_app"}, ("finish_app",)), ({"op": "restart"}, ("restart",)),
                          ({"op": "rollback"}, ("rollback",)), ({"op": "extend"}, ("extend",)),
                          ({"op": "end"}, ("end",))):
            self.assertEqual(self.ask(s, obj), {"ok": True})
            self.assertEqual(self.session.calls[-1], call)

    def test_errors_from_the_session(self):
        s = self.ctl()
        self.session.fail_with = "busy"
        self.assertEqual(self.ask(s, {"op": "capture", "kind": "screen"}), {"ok": False, "error": "busy"})
        self.session.fail_with = "bad_state"
        self.assertEqual(self.ask(s, {"op": "finish_app"}), {"ok": False, "error": "bad_state"})
        self.session.fail_with = "no_job"
        self.assertEqual(self.ask(s, {"op": "select_app", "app": "demo", "lang": "zh_CN"}),
                         {"ok": False, "error": "no_job"})
        self.session.fail_with = None
        self.assertEqual(self.ask(s, {"op": "get", "n": 9}), {"ok": False, "error": "no_such_capture"})

    def test_bad_requests(self):
        self.no_rate_limit()
        bad = [{"op": "nope"}, {"op": "status", "extra": 1}, {"op": "capture"}, {"op": "capture", "kind": "full"},
               {"op": "capture", "kind": 1}, {"op": "capture", "kind": "screen", "x": 1}, {"op": "lang", "lang": "fr"},
               {"op": "lang"}, {"op": "get"}, {"op": "get", "n": 0}, {"op": "get", "n": 100}, {"op": "get", "n": True},
               {"op": "get", "n": "4"}, {"op": "get", "n": 1.5}, {"op": 1}, {}, {"Op": "status"}, [], "status", 5, None,
               {"op": "end", "force": True}, {"op": "lang", "lang": "en_US", "desktop": "yes"},
               {"op": "lang", "lang": "en_US", "desktop": 1}, {"op": "lang", "lang": "en_US", "desktop": None},
               {"op": "lang", "lang": "en_US", "other": True}, {"op": "lang", "desktop": True},
               {"op": "select_app"}, {"op": "select_app", "app": "demo"}, {"op": "select_app", "lang": "zh_CN"},
               {"op": "select_app", "app": "../demo", "lang": "zh_CN"}, {"op": "select_app", "app": "Demo", "lang": "zh_CN"},
               {"op": "select_app", "app": "", "lang": "zh_CN"}, {"op": "select_app", "app": 1, "lang": "zh_CN"},
               {"op": "select_app", "app": "a" * 65, "lang": "zh_CN"}, {"op": "select_app", "app": "demo\n", "lang": "zh_CN"},
               {"op": "select_app", "app": "demo", "lang": "fr"}, {"op": "select_app", "app": "demo", "lang": "zh_CN", "x": 1},
               {"op": "finish_app", "app": "demo"}, {"op": "rollback", "lang": "zh_CN"}]
        s = self.ctl()
        for obj in bad:
            self.assertEqual(self.ask(s, obj), {"ok": False, "error": "bad_request"}, obj)
        for raw in (b"{not json", b"\xff\xfe", b"", b"{" * 5000 + b"}" * 5000):
            self.assertEqual(self.ask(s, raw), {"ok": False, "error": "bad_request"}, raw[:20])
        self.assertEqual(self.session.calls, [])

    def test_line_cap(self):
        self.no_rate_limit()
        s = self.ctl()
        pad = "a" * (W.CTL_LINE_MAX - len('{"op":"status","x":""}'))
        line = ('{"op":"status","x":"%s"}' % pad).encode()
        self.assertEqual(len(line), W.CTL_LINE_MAX)
        self.assertEqual(self.ask(s, line), {"ok": False, "error": "bad_request"})  # processed, merely invalid
        s.sendall(b"x" * (W.CTL_LINE_MAX + 1))
        self.assertEqual(json.loads(recv_line(s)), {"ok": False, "error": "bad_request"})
        self.assertClosed(s)

    def test_rate_limit(self):
        s = self.ctl()
        s.sendall(b'{"op":"status"}\n' * 8)
        answers = [json.loads(recv_line(s)) for _ in range(8)]
        self.assertEqual([a["ok"] for a in answers], [True] * W.RATE + [False] * (8 - W.RATE))
        self.assertEqual(answers[-1]["error"], "rate_limited")
        time.sleep(1.1)
        self.assertTrue(self.ask(s, {"op": "status"})["ok"])

    def test_get_streams_exactly_size_bytes(self):
        self.no_rate_limit()
        s = self.ctl()
        s.sendall(b'{"op":"get","n":4}\n')
        head = json.loads(recv_line(s))
        self.assertEqual(head, {"ok": True, "size": len(self.session.png)})
        self.assertEqual(recv_exact(s, head["size"]), self.session.png)
        self.assertTrue(self.ask(s, {"op": "status"})["ok"])  # the connection is reusable

    def test_pipelined_requests_after_auth(self):
        s = self.login()
        s.sendall(b'{"op":"status"}\n')
        self.assertEqual(recv_line(s), b"OK\n")
        self.assertTrue(json.loads(recv_line(s))["ok"])


class VncTests(WebCase):
    def vnc(self):
        s = self.login(b"VNC")
        self.assertEqual(recv_line(s), b"OK\n")
        return s

    def test_not_running(self):
        s = self.login(b"VNC")
        self.assertEqual(recv_line(s), b"ERR not_running\n")
        self.assertClosed(s)

    def test_relay_both_ways_and_filtering(self):
        self.start_qemu()
        s = self.vnc()
        self.assertEqual(recv_exact(s, 12), b"RFB 003.008\n")
        s.sendall(HANDSHAKE + update_request() + key() + pointer())
        want = HANDSHAKE + update_request() + key() + pointer()
        got = b""
        while len(got) < len(want) + 2:
            got += s.recv(100)
        self.assertEqual(got.replace(b"S:", b""), want)
        self.assertEqual(self.session.inputs, 2)

    def test_forbidden_message_closes_the_connection(self):
        self.start_qemu()
        s = self.vnc()
        recv_exact(s, 12)
        s.sendall(HANDSHAKE + b"\x06\0\0\0\0\0\0\x03abc")
        data = b""
        try:
            while True:
                chunk = s.recv(1000)
                if not chunk:
                    break
                data += chunk
        except ConnectionResetError:
            pass
        self.assertNotIn(b"abc", b"".join(self.qemu.received))
        self.assertNotIn(b"\x06", b"".join(self.qemu.received))

    def test_bad_version_is_never_forwarded(self):
        self.start_qemu()
        s = self.vnc()
        recv_exact(s, 12)
        s.sendall(b"RFB 003.003\n")
        self.assertClosed_or_eof(s)
        self.assertEqual(self.qemu.received, [])

    def assertClosed_or_eof(self, s):
        data = b""
        try:
            while True:
                c = s.recv(100)
                if not c:
                    break
                data += c
        except ConnectionResetError:
            pass
        return data

    def test_only_one_vnc_connection(self):
        self.start_qemu()
        first = self.vnc()
        recv_exact(first, 12)
        second = self.vnc()
        recv_exact(second, 12)
        self.assertEqual(self.assertClosed_or_eof(first), b"")
        second.sendall(HANDSHAKE)
        self.assertEqual(recv_exact(second, 2 + len(HANDSHAKE)), b"S:" + HANDSHAKE)

    def test_drop_vnc(self):
        self.start_qemu()
        s = self.vnc()
        recv_exact(s, 12)
        time.sleep(0.1)
        self.web.drop_vnc()
        self.assertEqual(self.assertClosed_or_eof(s), b"")

    def test_data_pipelined_behind_the_login_line(self):
        self.start_qemu()
        s = self.connect()
        line = recv_line(s)
        challenge = bytes.fromhex(line[10:-1].decode())
        mac = hmac.new(TOKEN, challenge + b"VNC", hashlib.sha256).hexdigest().encode()
        s.sendall(b"AUTH VNC " + mac + b"\n" + HANDSHAKE)
        self.assertEqual(recv_line(s), b"OK\n")
        self.assertEqual(recv_exact(s, 12), b"RFB 003.008\n")
        self.assertEqual(recv_exact(s, 2 + len(HANDSHAKE)), b"S:" + HANDSHAKE)

    def test_qemu_hangup_closes_the_browser_side(self):
        self.start_qemu()
        s = self.vnc()
        recv_exact(s, 12)
        self.qemu.close()
        self.session.vnc = os.path.join(self.tmp.name, "gone.sock")
        s2 = self.login(b"VNC")
        self.assertEqual(recv_line(s2), b"ERR vnc_unavailable\n")

    def test_close_removes_the_socket(self):
        self.web.close()
        self.assertFalse(os.path.exists(self.path))


class UnitTests(unittest.TestCase):
    def test_rate_limiter(self):
        now = [0.0]
        r = W.RateLimiter(2, 1.0, clock=lambda: now[0])
        self.assertEqual([r.allow(), r.allow(), r.allow()], [True, True, False])
        now[0] = 1.0
        self.assertTrue(r.allow())

    def test_stale_socket_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "web.sock")
            old = socket.socket(socket.AF_UNIX)
            old.bind(path)
            old.close()
            web = W.WebServer(FakeSession(), path, TOKEN, "no-such-group-xyz")
            web.bind()
            web.close()
            self.assertFalse(os.path.exists(path))

    def test_regular_file_is_not_deleted_by_bind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "web.sock")
            with open(path, "w") as f:
                f.write("precious")
            web = W.WebServer(FakeSession(), path, TOKEN, "no-such-group-xyz")
            with self.assertRaises(OSError):
                web.bind()
            self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
