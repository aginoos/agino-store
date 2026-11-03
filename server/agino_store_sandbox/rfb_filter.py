# SPDX-License-Identifier: GPL-3.0-or-later
"""Whitelist filter for the browser -> qemu direction of an RFB 3.8 connection (docs/sandbox.md, session).

qemu's built-in VNC server is C code that parses whatever the browser sends. Nothing reaches it
unless it is one of the few messages below, with an exact length; every message is rebuilt from the
parsed fields (padding zeroed), so no byte the filter did not look at is forwarded.

The filter works on a byte stream: feed() takes whatever recv() returned (any split, any number of
messages) and returns the bytes that are safe to forward. Incomplete messages stay buffered.
Anything not on the whitelist raises RfbReject; the caller closes the connection.
The qemu -> browser direction is not parsed (the browser handles it); it is only counted.
"""

import struct

VERSION = b"RFB 003.008\n"
SECURITY_NONE = 1
MAX_ENCODINGS = 20
# Raw, CopyRect, Tight, ZRLE, Cursor, DesktopSize, ExtendedDesktopSize, LastRect
ENCODINGS = frozenset((0, 1, 7, 16, -239, -223, -308, -224))
TIGHT_QUALITY = range(-32, -22)      # -32 .. -23
TIGHT_COMPRESS = range(-256, -246)   # -256 .. -247
SET_PIXEL_FORMAT, SET_ENCODINGS, UPDATE_REQUEST, KEY_EVENT, POINTER_EVENT = 0, 2, 3, 4, 5
FIXED_LENGTH = {SET_PIXEL_FORMAT: 20, UPDATE_REQUEST: 10, KEY_EVENT: 8, POINTER_EVENT: 6}

_HANDSHAKE_VERSION, _HANDSHAKE_SECURITY, _HANDSHAKE_INIT, _MESSAGES = range(4)


class RfbReject(Exception):
    """The peer sent something that is not allowed; the connection must be closed."""


def encoding_allowed(code):
    return code in ENCODINGS or code in TIGHT_QUALITY or code in TIGHT_COMPRESS


def _pixel_format(body):
    """Canonical 16-byte pixel format from the 16 bytes of a SetPixelFormat; RfbReject if implausible."""
    bpp, depth, big, true_colour, rmax, gmax, bmax, rshift, gshift, bshift = struct.unpack(">BBBBHHHBBB", body[:13])
    if bpp not in (8, 16, 32) or not 1 <= depth <= bpp or big > 1 or true_colour > 1:
        raise RfbReject("bad pixel format")
    if true_colour:
        for top, shift in ((rmax, rshift), (gmax, gshift), (bmax, bshift)):
            if top == 0 or top & (top + 1) or shift + top.bit_length() > bpp:
                raise RfbReject("bad pixel format colour field")
    elif bpp != 8:
        raise RfbReject("colour map needs 8 bits per pixel")
    return struct.pack(">BBBBHHHBBB", bpp, depth, big, true_colour, rmax, gmax, bmax, rshift, gshift, bshift) + b"\0\0\0"


class ClientFilter:
    def __init__(self, on_input=None):
        self._buf = bytearray()
        self._state = _HANDSHAKE_VERSION
        self._on_input = on_input
        self._dead = None
        self.key_events = 0
        self.pointer_events = 0
        self.bytes_in = 0
        self.bytes_out = 0

    @property
    def input_events(self):
        return self.key_events + self.pointer_events

    def feed(self, data):
        """Bytes to forward to qemu for `data`; RfbReject on the first violation (and ever after)."""
        if self._dead is not None:
            raise RfbReject(self._dead)
        self.bytes_in += len(data)
        self._buf += data
        out = bytearray()
        before = self.input_events
        try:
            while True:
                chunk = self._next()
                if chunk is None:
                    break
                out += chunk
        except RfbReject as e:
            self._dead = str(e)
            self._buf.clear()
            raise
        self.bytes_out += len(out)
        if self._on_input is not None and self.input_events != before:
            self._on_input(self.input_events - before)
        return bytes(out)

    def _take(self, n):
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    def _next(self):
        """Bytes of the next complete message (possibly b"" for a fully dropped one) or None if more is needed."""
        if self._state == _HANDSHAKE_VERSION:
            if len(self._buf) < len(VERSION):
                return None
            if bytes(self._buf[:len(VERSION)]) != VERSION:
                raise RfbReject("protocol version is not RFB 003.008")
            self._state = _HANDSHAKE_SECURITY
            return self._take(len(VERSION))
        if self._state == _HANDSHAKE_SECURITY:
            if not self._buf:
                return None
            if self._buf[0] != SECURITY_NONE:
                raise RfbReject("security type is not None")
            self._state = _HANDSHAKE_INIT
            return self._take(1)
        if self._state == _HANDSHAKE_INIT:
            if not self._buf:
                return None
            if self._buf[0] > 1:
                raise RfbReject("bad ClientInit")
            self._state = _MESSAGES
            return self._take(1)
        return self._message()

    def _message(self):
        if not self._buf:
            return None
        kind = self._buf[0]
        if kind == SET_ENCODINGS:
            return self._set_encodings()
        length = FIXED_LENGTH.get(kind)
        if length is None:
            raise RfbReject("message type %d is not allowed" % kind)
        if len(self._buf) < length:
            return None
        body = self._take(length)
        if kind == SET_PIXEL_FORMAT:
            return b"\x00\0\0\0" + _pixel_format(body[4:])
        if kind == UPDATE_REQUEST:
            incremental, x, y, w, h = struct.unpack(">BHHHH", body[1:])
            if incremental > 1:
                raise RfbReject("bad FramebufferUpdateRequest")
            return struct.pack(">BBHHHH", UPDATE_REQUEST, incremental, x, y, w, h)
        if kind == KEY_EVENT:
            down, key = struct.unpack(">B2xI", body[1:])
            if down > 1:
                raise RfbReject("bad KeyEvent")
            self.key_events += 1
            return struct.pack(">BB2xI", KEY_EVENT, down, key)
        buttons, x, y = struct.unpack(">BHH", body[1:])
        self.pointer_events += 1
        return struct.pack(">BBHH", POINTER_EVENT, buttons, x, y)

    def _set_encodings(self):
        if len(self._buf) < 4:
            return None
        count = struct.unpack(">H", bytes(self._buf[2:4]))[0]
        if count > MAX_ENCODINGS:
            raise RfbReject("SetEncodings lists %d encodings" % count)
        if len(self._buf) < 4 + 4 * count:
            return None
        body = self._take(4 + 4 * count)
        codes = struct.unpack(">%di" % count, body[4:])
        kept = [c for c in codes if encoding_allowed(c)]
        return struct.pack(">BxH%di" % len(kept), SET_ENCODINGS, len(kept), *kept)
