# SPDX-License-Identifier: GPL-3.0-or-later
"""QEMU monitor protocol client (greeting, capabilities, commands; events are skipped)."""

import time

from .jsonline import JsonLineClient
from .util import InfraError


class Qmp:
    def __init__(self, path):
        self._c = JsonLineClient(path)
        greeting = self._c.recv(10)
        if not greeting or "QMP" not in greeting:
            raise InfraError("no QMP greeting")
        self.execute("qmp_capabilities")

    def close(self):
        self._c.close()

    def execute(self, command, arguments=None, timeout=30):
        msg = {"execute": command}
        if arguments:
            msg["arguments"] = arguments
        self._c.send(msg)
        deadline = time.monotonic() + timeout
        while True:
            obj = self._c.recv(max(0.0, deadline - time.monotonic()))
            if obj is None:
                raise InfraError("QMP %s timed out" % command)
            if "return" in obj:
                return obj["return"]
            if "error" in obj:
                err = obj["error"] if isinstance(obj["error"], dict) else {}
                raise InfraError("QMP %s failed: %s" % (command, str(err.get("desc", "error"))[:200]))

    def screendump(self, filename):
        self.execute("screendump", {"filename": filename, "format": "png"})

    def quit(self):
        try:
            self.execute("quit", timeout=5)
        except InfraError:
            pass
