# SPDX-License-Identifier: GPL-3.0-or-later
"""QEMU guest agent client: guest-sync handshake and guest-exec with timeouts and capped output."""

import base64
import binascii
import dataclasses
import itertools
import time

from .jsonline import JsonLineClient
from .util import InfraError

MAX_OUTPUT = 1 << 20
TIMEOUT_PATH = "/usr/bin/timeout"


@dataclasses.dataclass
class ExecResult:
    exitcode: int
    out: bytes
    err: bytes
    timed_out: bool = False

    @property
    def ok(self):
        return self.exitcode == 0 and not self.timed_out

    @property
    def text(self):
        return self.out.decode("utf-8", "replace")


def _decode(value):
    if not isinstance(value, str):
        return b""
    try:
        return base64.b64decode(value, validate=True)[:MAX_OUTPUT]
    except (binascii.Error, ValueError):
        return b""


class Qga:
    def __init__(self, path):
        self._c = JsonLineClient(path)
        self._ids = itertools.count(int(time.time()) & 0xFFFFFF)

    def close(self):
        self._c.close()

    def _call(self, command, arguments=None, timeout=30):
        msg = {"execute": command}
        if arguments is not None:
            msg["arguments"] = arguments
        self._c.send(msg)
        obj = self._c.recv(timeout)
        if obj is None:
            raise InfraError("guest agent %s timed out" % command)
        if "error" in obj:
            err = obj["error"] if isinstance(obj["error"], dict) else {}
            raise InfraError("guest agent %s failed: %s" % (command, str(err.get("desc", "error"))[:200]))
        return obj.get("return")

    def wait_ready(self, timeout):
        """guest-sync until the agent answers with our id; stale replies are discarded."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sync_id = next(self._ids)
            self._c.send({"execute": "guest-sync", "arguments": {"id": sync_id}}, prefix=b"\xff")
            until = min(deadline, time.monotonic() + 3)
            while time.monotonic() < until:
                obj = self._c.recv(max(0.0, until - time.monotonic()))
                if obj is not None and obj.get("return") == sync_id:
                    return
            time.sleep(1)
        raise InfraError("guest agent did not answer within %d s" % timeout)

    def start(self, argv, input_data=None, timeout=60):
        """guest-exec without waiting; argv[0] must be an absolute path. Returns the guest pid."""
        args = {"path": argv[0], "arg": list(argv[1:]), "capture-output": True}
        if input_data is not None:
            args["input-data"] = base64.b64encode(input_data).decode()
        ret = self._call("guest-exec", args, timeout)
        pid = ret.get("pid") if isinstance(ret, dict) else None
        if not isinstance(pid, int) or isinstance(pid, bool):
            raise InfraError("guest-exec returned no pid")
        return pid

    def status(self, pid):
        """ExecResult once the process has exited, else None."""
        ret = self._call("guest-exec-status", {"pid": pid}, 30)
        if not isinstance(ret, dict) or not ret.get("exited"):
            return None
        code = ret.get("exitcode", -1)
        if not isinstance(code, int) or isinstance(code, bool):
            code = -1
        return ExecResult(code, _decode(ret.get("out-data")), _decode(ret.get("err-data")))

    def exec(self, argv, timeout, input_data=None):
        """Run argv in the guest under `timeout`(1); a hung step is reported as timed_out, not raised."""
        wrapped = [TIMEOUT_PATH, "-k", "10", str(int(timeout))] + list(argv)
        pid = self.start(wrapped, input_data)
        deadline = time.monotonic() + timeout + 60
        while time.monotonic() < deadline:
            res = self.status(pid)
            if res is not None:
                res.timed_out = res.exitcode in (124, 137)
                return res
            time.sleep(0.5)
        return ExecResult(-1, b"", b"", timed_out=True)

    def sh(self, script, args=(), timeout=60, input_data=None):
        """Run a fixed shell script; variable parts travel as positional arguments, never in the text."""
        return self.exec(["/bin/sh", "-c", script, "sh", *args], timeout, input_data)
