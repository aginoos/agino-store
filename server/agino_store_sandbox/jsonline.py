# SPDX-License-Identifier: GPL-3.0-or-later
"""JSON-lines client over a unix socket; the framing shared by QMP and the guest agent."""

import json
import socket
import time

from .util import InfraError

MAX_LINE = 8 << 20


class JsonLineClient:
    def __init__(self, path, connect_timeout=5.0):
        self._buf = b""
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(connect_timeout)
        try:
            self._sock.connect(path)
        except OSError as e:
            self._sock.close()
            raise InfraError("cannot connect to %s: %s" % (path, e.strerror)) from None

    def close(self):
        self._sock.close()

    def send(self, obj, prefix=b""):
        self._sock.settimeout(10)
        try:
            self._sock.sendall(prefix + json.dumps(obj).encode() + b"\n")
        except OSError as e:
            raise InfraError("socket write failed: %s" % e.strerror) from None

    def recv(self, timeout):
        """Next JSON object; None when nothing complete arrives within timeout seconds."""
        deadline = time.monotonic() + timeout
        while True:
            line, sep, rest = self._buf.partition(b"\n")
            if sep:
                self._buf = rest
                line = line.strip().lstrip(b"\xff")
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError, RecursionError):
                    continue
                if isinstance(obj, dict):
                    return obj
                continue
            if len(self._buf) > MAX_LINE:
                raise InfraError("peer sent a line larger than %d bytes" % MAX_LINE)
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self._sock.settimeout(left)
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                return None
            except OSError as e:
                raise InfraError("socket read failed: %s" % e.strerror) from None
            if not chunk:
                raise InfraError("peer closed the socket")
            self._buf += chunk
