# SPDX-License-Identifier: GPL-3.0-or-later
"""web.sock of an interactive session: challenge/HMAC login, the VNC relay and the control channel.

Protocol (docs/sandbox.md section 9): the server sends `CHALLENGE <32 hex>`, the client answers
`AUTH <VNC|CTL> <hmac hex>` (HMAC-SHA256 over challenge + mode with the 32-byte token as key), the server
replies `OK`. VNC then relays RFB through the client-message whitelist; CTL is line-based JSON.
Nothing here talks to the guest agent while a relay thread is involved: control operations run on the
connection's own thread through Session.
"""

import collections
import grp
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import stat
import struct
import threading
import time

from .rfb_filter import ClientFilter, RfbReject
from .session import APP_RE, OpError
from .shots_scene import LANGS
from .util import log

AUTH_LINE_MAX = 128
AUTH_DEADLINE = 5.0
CTL_LINE_MAX = 4096
CTL_IDLE = 900.0
RATE = 5
SEND_TIMEOUT = 30
MAX_CONNECTIONS = 16
CHUNK = 65536
HEX64 = re.compile(rb"\A[0-9a-f]{64}\Z")
KINDS = ("window", "screen")


class Closed(Exception):
    pass


class LineTooLong(Exception):
    pass


class Deadline(Exception):
    pass


class LineReader:
    """Reads newline-terminated lines of bounded length; keeps what follows the newline."""

    def __init__(self, sock):
        self.sock, self.buf = sock, bytearray()

    def readline(self, limit, deadline=None):
        """The next line without its newline; `limit` is the longest accepted line (newline not counted)."""
        while True:
            i = self.buf.find(b"\n")
            if 0 <= i <= limit:
                line = bytes(self.buf[:i])
                del self.buf[:i + 1]
                return line
            if len(self.buf) > limit:
                raise LineTooLong()
            self.sock.settimeout(None if deadline is None else max(0.001, deadline - time.monotonic()))
            try:
                chunk = self.sock.recv(min(CHUNK, limit + 1 - len(self.buf)))
            except socket.timeout:
                raise Deadline() from None
            except OSError:
                raise Closed() from None
            if not chunk:
                raise Closed()
            self.buf += chunk


class RateLimiter:
    def __init__(self, count, window=1.0, clock=time.monotonic):
        self.count, self.window, self.clock = count, window, clock
        self.times = collections.deque()

    def allow(self):
        now = self.clock()
        while self.times and now - self.times[0] >= self.window:
            self.times.popleft()
        if len(self.times) >= self.count:
            return False
        self.times.append(now)
        return True


def expected_mac(token, challenge, mode):
    return hmac.new(token, challenge + mode, hashlib.sha256).hexdigest().encode("ascii")


def _send_timeout(sock):
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, struct.pack("ll", SEND_TIMEOUT, 0))


def _field(kind):
    return {"str": lambda v: isinstance(v, str),
            "kind": lambda v: v in KINDS,
            "lang": lambda v: v in LANGS,
            "app": lambda v: isinstance(v, str) and APP_RE.match(v) is not None,
            "bool": lambda v: isinstance(v, bool),
            "n": lambda v: isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 99}[kind]


class WebServer:
    def __init__(self, session, sock_path, token, group="agino-admin"):
        self.session, self.path, self.token, self.group = session, sock_path, token, group
        self.listener = None
        self._conns = set()
        self._lock = threading.Lock()
        self._vnc = None
        # op -> (required fields, optional fields, handler); fields map a name to a kind of _field()
        self.ops = {
            "status": ({}, {}, lambda r: session.status()),
            "capture": ({"kind": "kind"}, {}, lambda r: dict(session.op_capture(r["kind"]), ok=True)),
            "captures": ({}, {}, lambda r: {"ok": True, "items": [dict(i, ok=True) for i in session.op_captures()]}),
            "select_app": ({"app": "app", "lang": "lang"}, {},
                           lambda r: self._done(session.op_select_app(r["app"], r["lang"]))),
            "finish_app": ({}, {}, lambda r: self._done(session.op_finish_app())),
            "lang": ({"lang": "lang"}, {"desktop": "bool"},
                     lambda r: self._done(session.op_lang(r["lang"], r.get("desktop", False)))),
            "restart": ({}, {}, lambda r: self._done(session.op_restart())),
            "rollback": ({}, {}, lambda r: self._done(session.op_rollback())),
            "extend": ({}, {}, lambda r: self._done(session.op_extend())),
            "end": ({}, {}, lambda r: self._done(session.op_end())),
        }

    @staticmethod
    def _done(_):
        return {"ok": True}

    # ---- listener ----

    def bind(self):
        try:
            if stat.S_ISSOCK(os.lstat(self.path).st_mode):
                os.unlink(self.path)  # left behind by a crashed daemon with this fixed name
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)  # never visible with wider permissions, even for an instant
        try:
            srv.bind(self.path)
        except OSError:
            srv.close()
            raise
        finally:
            os.umask(old)
        try:
            gid = grp.getgrnam(self.group).gr_gid
        except KeyError:
            log("group %s does not exist; web.sock keeps the process group" % self.group)
            gid = os.getegid()
        try:
            os.chown(self.path, -1, gid)
        except OSError as e:
            log("cannot give web.sock to group %s: %s" % (self.group, e.strerror))
        os.chmod(self.path, 0o660)
        srv.listen(32)
        self.listener = srv

    def serve(self):
        """Accept loop (run in a thread)."""
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            with self._lock:
                busy = len(self._conns) >= MAX_CONNECTIONS
                if not busy:
                    self._conns.add(conn)
            if busy:
                conn.close()
                continue
            threading.Thread(target=self._guard, args=(conn,), daemon=True).start()

    def close(self):
        if self.listener is not None:
            with _quiet():
                self.listener.close()
        with _quiet():
            os.unlink(self.path)
        with self._lock:
            conns = list(self._conns)
        for c in conns:
            with _quiet():
                c.shutdown(socket.SHUT_RDWR)

    def drop_vnc(self):
        with self._lock:
            old, self._vnc = self._vnc, None
        if old is not None:
            old.close()

    # ---- one connection ----

    def _guard(self, conn):
        try:
            _send_timeout(conn)
            self._handle(conn)
        except (Closed, Deadline, LineTooLong, OSError):
            pass
        except Exception as e:  # noqa: BLE001 - one bad connection must not end the daemon
            log("web.sock: %s: %s" % (type(e).__name__, e))
        finally:
            with self._lock:
                self._conns.discard(conn)
            with _quiet():
                conn.close()

    def _handle(self, conn):
        challenge = secrets.token_bytes(16)
        conn.sendall(b"CHALLENGE " + challenge.hex().encode("ascii") + b"\n")
        reader = LineReader(conn)
        line = reader.readline(AUTH_LINE_MAX, time.monotonic() + AUTH_DEADLINE)
        parts = line.split(b" ")
        if len(parts) != 3 or parts[0] != b"AUTH" or parts[1] not in (b"VNC", b"CTL") or not HEX64.match(parts[2]) \
                or not hmac.compare_digest(parts[2], expected_mac(self.token, challenge, parts[1])):
            log("web.sock: authentication failed")
            return
        if parts[1] == b"CTL":
            conn.sendall(b"OK\n")
            self._ctl(conn, reader)
        else:
            self._vnc_session(conn, reader)

    # ---- VNC ----

    def _vnc_session(self, conn, reader):
        path = self.session.vnc_path()
        if path is None:
            conn.sendall(b"ERR not_running\n")
            return
        qemu = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            qemu.settimeout(5)
            qemu.connect(path)
            qemu.settimeout(None)
            _send_timeout(qemu)
        except OSError as e:
            qemu.close()
            log("web.sock: cannot reach the VNC socket: %s" % e.strerror)
            conn.sendall(b"ERR vnc_unavailable\n")
            return
        link = _Link(conn, qemu)
        with self._lock:
            old, self._vnc = self._vnc, link
        if old is not None:
            old.close()
        try:
            conn.settimeout(None)  # blocking relay; writes are bounded by SO_SNDTIMEO
            conn.sendall(b"OK\n")
            threading.Thread(target=link.pump_back, daemon=True).start()
            self._pump_forward(link, reader)
        finally:
            link.close()
            with self._lock:
                if self._vnc is link:
                    self._vnc = None
            log("web.sock: vnc closed (browser->qemu %d B forwarded, qemu->browser %d B)" % (link.sent, link.received))

    def _pump_forward(self, link, reader):
        flt = ClientFilter(on_input=self.session.touch_input)
        pending = bytes(reader.buf)
        try:
            while True:
                data = pending or link.client.recv(CHUNK)
                pending = b""
                if not data:
                    return
                out = flt.feed(data)
                if out:
                    link.qemu.sendall(out)
                    link.sent += len(out)
        except RfbReject as e:
            log("web.sock: VNC client message rejected: %s" % e)
        except OSError:
            pass

    # ---- control channel ----

    def _ctl(self, conn, reader):
        limiter = RateLimiter(RATE)
        while True:
            try:
                line = reader.readline(CTL_LINE_MAX, time.monotonic() + CTL_IDLE)
            except LineTooLong:
                self._reply(conn, {"ok": False, "error": "bad_request"})
                return
            if not limiter.allow():
                self._reply(conn, {"ok": False, "error": "rate_limited"})
                continue
            blob = None
            try:
                request = self._parse(line)
                if request["op"] == "get":
                    blob = self.session.op_get(request["n"])
                    response = {"ok": True, "size": len(blob)}
                else:
                    response = self.ops[request["op"]][2](request)
            except OpError as e:
                response, blob = {"ok": False, "error": e.code}, None
            except _BadRequest:
                response, blob = {"ok": False, "error": "bad_request"}, None
            except Exception as e:  # noqa: BLE001 - answer, keep the connection
                log("web.sock: %s while serving a control request: %s" % (type(e).__name__, e))
                response, blob = {"ok": False, "error": "internal"}, None
            self._reply(conn, response)
            if blob is not None:
                conn.sendall(blob)  # same SEND_TIMEOUT as the reply

    def _parse(self, line):
        try:
            request = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise _BadRequest() from None
        if not isinstance(request, dict) or not isinstance(request.get("op"), str):
            raise _BadRequest()
        op = request["op"]
        if op == "get":
            required, optional = {"n": "n"}, {}
        elif op in self.ops:
            required, optional = self.ops[op][0], self.ops[op][1]
        else:
            raise _BadRequest()
        given = set(request) - {"op"}
        if not set(required) <= given <= set(required) | set(optional):
            raise _BadRequest()
        fields = dict(required, **optional)
        if not all(_field(fields[name])(request[name]) for name in given):
            raise _BadRequest()
        return request

    @staticmethod
    def _reply(conn, response):
        conn.settimeout(SEND_TIMEOUT)
        conn.sendall(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")


class _BadRequest(Exception):
    pass


class _quiet:  # noqa: N801 - tiny context manager that ignores OSError
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, OSError)


class _Link:
    """The two sockets of one VNC relay; close() ends both directions."""

    def __init__(self, client, qemu):
        self.client, self.qemu = client, qemu
        self.sent = self.received = 0
        self._closed = threading.Event()

    def pump_back(self):
        """qemu -> browser, opaque (only counted)."""
        try:
            while True:
                data = self.qemu.recv(CHUNK)
                if not data:
                    break
                self.client.sendall(data)
                self.received += len(data)
        except OSError:
            pass
        finally:
            self.close()

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        for s in (self.client, self.qemu):
            with _quiet():
                s.shutdown(socket.SHUT_RDWR)
        with _quiet():
            self.qemu.close()
