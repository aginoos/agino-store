# SPDX-License-Identifier: GPL-3.0-or-later
"""Fakes shared by the session tests: QMP, guest agent, guest helpers, VM."""

import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server"))

from agino_store_sandbox import config  # noqa: E402
from agino_store_sandbox.util import InfraError  # noqa: E402


def make_ppm(width, height, pixel=b"\x10\x20\x30", header=None):
    head = header if header is not None else b"P6\n%d %d\n255\n" % (width, height)
    return head + pixel * (width * height)


def decode_rgb_png(data):
    """(width, height, rgb bytes) of a filter-0 8-bit RGB PNG; asserts the file has only IHDR/IDAT/IEND."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, head, kinds = 8, b"", None, []
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        assert struct.unpack(">I", data[pos + 8 + n:pos + 12 + n])[0] == zlib.crc32(kind + body)
        kinds.append(kind)
        if kind == b"IHDR":
            head = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    assert kinds == [b"IHDR", b"IDAT", b"IEND"], kinds
    w, h, depth, ctype = head[:4]
    assert (depth, ctype) == (8, 2)
    raw = zlib.decompress(idat)
    stride = w * 3 + 1
    assert all(raw[i * stride] == 0 for i in range(h))
    return w, h, b"".join(raw[i * stride + 1:(i + 1) * stride] for i in range(h))


def session_cfg(tmp, sandbox=None, **session):
    """A Config whose paths are all inside tmp."""
    values = {"queue": tmp + "/queue", "state": tmp + "/state", "run": tmp + "/run", "shot_dir": tmp + "/shots",
              "shots_out": tmp + "/shots-out", "reports": tmp + "/reports", "base": tmp + "/base.qcow2",
              "base_vars": tmp + "/base.vars", "ovmf_code": tmp + "/code.fd", "key": tmp + "/key.pem",
              "qemu_sandbox": "off"}
    values.update(sandbox or {})
    sess = {"runtime_dir": tmp + "/rt", "captures": tmp + "/captures", "web_group": "nonexistent-group-xyz"}
    sess.update({k: str(v) for k, v in session.items()})
    return config.from_dict(values, sess)


class FakeQmp:
    def __init__(self, data=None, fail=False):
        self.data, self.fail, self.calls = data, fail, []

    def screendump(self, filename, fmt="png"):
        self.calls.append((filename, fmt))
        if self.fail:
            raise InfraError("QMP screendump failed: no display")
        with open(filename, "wb") as f:
            f.write(self.data)

    def quit(self):
        self.calls.append("quit")

    def close(self):
        pass


class FakeShotsGuest:
    """The parts of ShotsGuest the window locator calls."""

    def __init__(self, windows=None, active=None, xdotool=True):
        # windows: {wid: dict(type, geo=(x,y,w,h), ext=(l,r,t,b), title)}
        self.table = windows or {}
        self.active, self.xdotool, self.calls = active, xdotool, []
        self.fail = False

    def has_xdotool(self):
        return self.xdotool

    def windows(self):
        if self.fail:
            raise InfraError("xprop failed")
        return set(self.table)

    def active_window(self):
        return self.active

    def window_type(self, wid):
        return self.table[wid].get("type", "")

    def geometry(self, wid):
        return self.table[wid]["geo"]

    def extents(self, wid):
        return self.table[wid].get("ext", (0, 0, 0, 0))

    def title(self, wid):
        return self.table[wid].get("title", "")
