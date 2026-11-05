# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure-Python image handling for screenshots: PPM in, cropped RGBA PNG with rounded corners out."""

import math
import struct
import zlib

MAX_SIDE = 8192
MAX_PPM = 128 << 20


class ImageError(Exception):
    pass


def parse_ppm(data, max_side=MAX_SIDE):
    """(width, height, rgb bytes) from a binary P6 PPM; ImageError for anything else."""
    pos = 0
    tokens = []
    while len(tokens) < 4:
        while pos < len(data) and data[pos:pos + 1].isspace():
            pos += 1
        if pos < len(data) and data[pos:pos + 1] == b"#":
            while pos < len(data) and data[pos] != 0x0A:
                pos += 1
            continue
        start = pos
        while pos < len(data) and not data[pos:pos + 1].isspace() and len(tokens) < 4:
            pos += 1
        if start == pos or pos - start > 8:
            raise ImageError("bad PPM header")
        tokens.append(data[start:pos])
    pos += 1  # exactly one whitespace byte after maxval
    try:
        width, height, maxval = int(tokens[1]), int(tokens[2]), int(tokens[3])
    except ValueError:
        raise ImageError("bad PPM header") from None
    if tokens[0] != b"P6" or maxval != 255 or not (0 < width <= max_side and 0 < height <= max_side):
        raise ImageError("unsupported PPM")
    pixels = data[pos:pos + width * height * 3]
    if len(pixels) != width * height * 3:
        raise ImageError("truncated PPM")
    return width, height, pixels


def clamp_rect(x, y, w, h, screen_w, screen_h):
    """Integer rectangle forced inside the screen; None if nothing is left. Inputs are untrusted."""
    try:
        x, y, w, h = int(x), int(y), int(w), int(h)
    except (TypeError, ValueError, OverflowError):
        return None
    x0, y0 = max(0, min(x, screen_w)), max(0, min(y, screen_h))
    x1, y1 = max(0, min(x + w, screen_w)), max(0, min(y + h, screen_h))
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1 - x0, y1 - y0


def crop_rgba(width, rgb, rect, radius):
    """Rows (bytes, RGBA) of the rect cut out of an RGB frame, corners outside radius made transparent."""
    x, y, w, h = rect
    radius = max(0, min(int(radius), w // 2, h // 2))
    rows = []
    for j in range(h):
        start = ((y + j) * width + x) * 3
        src = rgb[start:start + w * 3]
        row = bytearray(w * 4)
        row[0::4], row[1::4], row[2::4] = src[0::3], src[1::3], src[2::3]
        row[3::4] = b"\xff" * w
        rows.append(row)
    for j in range(radius):
        for i in range(radius):
            # distance of the pixel centre from the corner circle's centre (radius, radius)
            d = math.hypot(radius - (i + 0.5), radius - (j + 0.5))
            alpha = int(max(0.0, min(1.0, radius - d + 0.5)) * 255)
            if alpha == 255:
                continue
            for jj, ii in ((j, i), (j, w - 1 - i), (h - 1 - j, i), (h - 1 - j, w - 1 - i)):
                rows[jj][ii * 4 + 3] = alpha
    return [bytes(r) for r in rows]


def dominant_fraction(width, rgb, rect):
    """Share (0..1) of the subsampled pixels of the rect that have the most common colour (5 bits per channel,
    so smooth gradients and compression noise still count as one colour): an empty window is mostly one colour."""
    x, y, w, h = rect
    seen = {}
    count = 0
    for j in range(y, y + h, 4):
        base = (j * width + x) * 3
        row = rgb[base:base + w * 3]
        for i in range(0, w * 3 - 2, 12):
            key = (row[i] >> 3, row[i + 1] >> 3, row[i + 2] >> 3)
            seen[key] = seen.get(key, 0) + 1
            count += 1
    return max(seen.values()) / count if count else 0.0


def luminance_stddev(width, rgb, rect):
    """Standard deviation of luminance over a subsample of the rect (cheap uniformity test)."""
    x, y, w, h = rect
    total = total2 = count = 0
    for j in range(y, y + h, 4):
        base = (j * width + x) * 3
        row = rgb[base:base + w * 3]
        for i in range(0, w * 3 - 2, 12):
            lum = (row[i] * 299 + row[i + 1] * 587 + row[i + 2] * 114) // 1000
            total += lum
            total2 += lum * lum
            count += 1
    if not count:
        return 0.0
    mean = total / count
    return math.sqrt(max(0.0, total2 / count - mean * mean))


def _chunk(kind, payload):
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))


def encode_png(width, height, rows):
    """8-bit RGBA PNG bytes from rows of RGBA bytes."""
    raw = b"".join(b"\x00" + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, 6)) + _chunk(b"IEND", b""))
