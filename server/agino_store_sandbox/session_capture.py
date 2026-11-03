# SPDX-License-Identifier: GPL-3.0-or-later
"""Screen and window captures of an interactive session (docs/sandbox.md section 9).

Nothing from the guest is trusted: the picture comes from QMP screendump (a PPM that is stat'ed before it
is read, then decoded with a hard size limit and re-encoded as a plain RGB PNG on the host); the guest only
tells the window geometry and title, as integers and a short cleaned string.
"""

import hashlib
import json
import os
import re
import stat
import struct
import threading
import time
import zlib

from . import shots_img, shots_scene
from .util import InfraError, clean, now

MIN_SIDE = (640, 480)
MAX_SIDE = 4096
MAX_PPM_FILE = shots_img.MAX_PPM
MAX_PNG = 3 << 20
TITLE_LIMIT = 120
MIN_SHOT_WIDTH = 900
MIN_SHOT_HEIGHT = 300
KINDS = ("window", "screen")
PPM_HEADER = re.compile(rb"\AP6[ \n]+([0-9]{1,5})[ \n]+([0-9]{1,5})[ \n]+255\n")
NAME_RE = re.compile(r"\A([0-9]{2})-(window|screen)-(zh_CN|en_US)\.png\Z")
FILE_MODE = 0o640
WINDOW_TYPES_OK = ("NORMAL",)
WINDOW_BUDGET = 40.0


class CaptureError(Exception):
    """A capture that cannot be made; `code` is the short error string sent to the client."""

    def __init__(self, code, detail="", qga_broken=False):
        super().__init__(code + (": " + detail if detail else ""))
        self.code, self.detail, self.qga_broken = code, detail, qga_broken


def ppm_header(head):
    """(width, height, header length) of a P6 header inside `head`, range-checked; CaptureError otherwise."""
    m = PPM_HEADER.match(head)
    if not m:
        raise CaptureError("bad_image", "bad PPM header")
    width, height = int(m.group(1)), int(m.group(2))
    if not (MIN_SIDE[0] <= width <= MAX_SIDE and MIN_SIDE[1] <= height <= MAX_SIDE):
        raise CaptureError("bad_image", "display size %dx%d is out of range" % (width, height))
    return width, height, m.end()


def read_screendump(path):
    """(width, height, rgb) of the PPM at path, which is removed in every case."""
    try:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as e:
            raise CaptureError("capture_failed", "no screendump: %s" % e.strerror) from None
        with os.fdopen(fd, "rb") as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PPM_FILE:
                raise CaptureError("bad_image", "screendump is not a plain file of acceptable size")
            width, height, hlen = ppm_header(f.read(64))
            if info.st_size != hlen + width * height * 3:
                raise CaptureError("bad_image", "screendump size does not match its header")
            f.seek(0)
            data = f.read(info.st_size)
        try:
            return shots_img.parse_ppm(data, MAX_SIDE)
        except shots_img.ImageError as e:
            raise CaptureError("bad_image", str(e)) from None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _chunk(kind, payload):
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))


def encode_png_rgb(width, height, rgb, level=6):
    """Plain 8-bit RGB PNG (no alpha, no ancillary chunks) of an RGB frame."""
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, level)) + _chunk(b"IEND", b""))


def crop_rgb(width, rgb, rect):
    x, y, w, h = rect
    return b"".join(rgb[((y + j) * width + x) * 3:((y + j) * width + x + w) * 3] for j in range(h))


def problems_for(width, height):
    out = []
    if width < MIN_SHOT_WIDTH:
        out.append("too_narrow")
    if height < MIN_SHOT_HEIGHT:
        out.append("too_short")
    return out


class GuestLocator:
    """Finds the window to capture through the guest agent; every step is bounded by one overall budget."""

    def __init__(self, shots_guest, budget=WINDOW_BUDGET, clock=time.monotonic):
        self.sg, self.budget, self.clock = shots_guest, budget, clock

    def locate(self, screen):
        """((x, y, w, h) frame rectangle inside screen, title) of the active or the largest normal window."""
        end = self.clock() + self.budget

        def step():
            if self.clock() > end:
                raise InfraError("guest agent budget exceeded")

        sg = self.sg
        if not sg.has_xdotool():
            raise CaptureError("no_xdotool")
        windows = sg.windows()
        step()
        wid = sg.active_window()
        if wid not in windows or not self._acceptable(wid, True):
            step()
            normal = [w for w in sorted(windows) if self._acceptable(w, False)]
            step()
            wid = max(normal, key=lambda w: self._area(w), default=None)
        if wid is None:
            raise CaptureError("no_window")
        step()
        rect = shots_scene.frame_rect(sg.geometry(wid), sg.extents(wid), screen)
        if rect is None:
            raise CaptureError("no_window", "window geometry unusable")
        step()
        return rect, clean(sg.title(wid), TITLE_LIMIT)

    def _acceptable(self, wid, allow_dialog):
        types = re.findall(r"_NET_WM_WINDOW_TYPE_([A-Z_]+)", self.sg.window_type(wid))
        ok = WINDOW_TYPES_OK + (("DIALOG",) if allow_dialog else ())
        return not types or any(t in ok for t in types)

    def _area(self, wid):
        geo = self.sg.geometry(wid)
        return geo[2] * geo[3] if geo else 0


def public(item):
    """The metadata clients see (no time stamp)."""
    return {k: v for k, v in item.items() if k != "time"}


class CaptureStore:
    """The captures of one session on disk: NN-<kind>-<lang>.png files and captures.json."""

    def __init__(self, directory, session_id, app, max_captures):
        self.directory, self.session_id, self.app, self.max = directory, session_id, app, max_captures
        self.items = []
        self._lock = threading.Lock()

    def create_directory(self):
        os.makedirs(os.path.dirname(self.directory), mode=0o750, exist_ok=True)
        os.mkdir(self.directory, 0o750)
        os.chmod(self.directory, 0o750)  # the process umask must not narrow it, nor the parent's setgid widen it
        self._write_index()

    def count(self):
        with self._lock:
            return len(self.items)

    def snapshot(self):
        with self._lock:
            return [public(i) for i in self.items]

    def add(self, kind, lang, png, width, height, title):
        """Store a finished PNG as the next capture; returns its metadata (CaptureError when full or too big)."""
        if len(png) > MAX_PNG:
            raise CaptureError("too_big", "%d bytes" % len(png))
        with self._lock:
            if len(self.items) >= self.max:
                raise CaptureError("too_many")
            n = max((i["n"] for i in self.items), default=0) + 1
            name = "%02d-%s-%s.png" % (n, kind, lang)
            self._write(name, png)
            item = {"n": n, "file": name, "kind": kind, "lang": lang, "width": width, "height": height,
                    "size": len(png), "sha256": hashlib.sha256(png).hexdigest(), "title": title,
                    "problems": problems_for(width, height), "time": now()}
            self.items = self.items + [item]
            try:
                self._write_index()
            except OSError:
                self.items = self.items[:-1]
                os.unlink(os.path.join(self.directory, name))
                raise
            return public(item)

    def read(self, n):
        """Bytes of capture n (None when there is no such capture)."""
        with self._lock:
            item = next((i for i in self.items if i["n"] == n), None)
        if item is None:
            return None
        with open(os.path.join(self.directory, item["file"]), "rb") as f:
            return f.read(MAX_PNG + 1)

    def _write(self, name, data):
        tmp = os.path.join(self.directory, "." + name + ".tmp")
        with open(tmp, "wb") as f:
            f.write(data)
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, os.path.join(self.directory, name))

    def _write_index(self):
        doc = {"schema": 1, "session": self.session_id, "app": self.app, "updated": now(),
               "items": list(self.items)}
        self._write("captures.json", (json.dumps(doc, indent=1, sort_keys=True) + "\n").encode("utf-8"))


class Capturer:
    def __init__(self, store, qmp, ppm_path, locator):
        self.store, self.qmp, self.ppm_path, self.locator = store, qmp, ppm_path, locator

    def capture(self, kind, lang):
        """Take a capture of kind 'window' or 'screen'; returns the stored item. CaptureError otherwise."""
        if kind not in KINDS or lang not in shots_scene.LANGS:
            raise CaptureError("bad_request")
        if self.store.count() >= self.store.max:
            raise CaptureError("too_many")
        try:
            os.unlink(self.ppm_path)
        except FileNotFoundError:
            pass
        try:
            self.qmp.screendump(self.ppm_path, "ppm")
        except InfraError as e:
            raise CaptureError("capture_failed", str(e)) from None
        width, height, rgb = read_screendump(self.ppm_path)
        title = ""
        if kind == "window":
            try:
                rect, title = self.locator.locate((width, height))
            except InfraError as e:
                raise CaptureError("guest_failed", str(e), qga_broken=True) from None
            rect = shots_img.clamp_rect(*rect, width, height)
            if rect is None:
                raise CaptureError("no_window", "window outside the screen")
            rgb, (width, height) = crop_rgb(width, rgb, rect), rect[2:]
        png = encode_png_rgb(width, height, rgb)
        return self.store.add(kind, lang, png, width, height, title)
