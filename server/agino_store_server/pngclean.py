# SPDX-License-Identifier: GPL-3.0-or-later
"""Validate a PNG and strip it to the chunks that matter (docs/server.md section 12).

Only IHDR, PLTE, IDAT, IEND and tRNS survive; text, EXIF, colour-profile and every other
ancillary chunk is dropped. Chunk CRCs are checked, the zlib stream is decompressed with a
hard bound (the size the header promises) and must match that size exactly.
"""

import struct
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"
KEEP = (b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS")
MAX_CHUNK = 1 << 30
# color type -> allowed bit depths, samples per pixel
COLOR_TYPES = {0: ((1, 2, 4, 8, 16), 1), 2: ((8, 16), 3), 3: ((1, 2, 4, 8), 1), 4: ((8, 16), 2), 6: ((8, 16), 4)}
ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


class PngError(ValueError):
    pass


def _chunks(data):
    pos = len(SIGNATURE)
    while pos < len(data):
        if pos + 12 > len(data):
            raise PngError("truncated chunk header")
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        if length > MAX_CHUNK or pos + 12 + length > len(data):
            raise PngError("chunk %r is truncated" % kind.decode("latin-1"))
        body = data[pos + 8:pos + 8 + length]
        crc = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])[0]
        if kind in KEEP and zlib.crc32(kind + body) != crc:
            raise PngError("bad CRC in chunk %s" % kind.decode("latin-1"))
        yield kind, body
        pos += 12 + length


def _header(body):
    if len(body) != 13:
        raise PngError("bad IHDR")
    width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", body)
    if color not in COLOR_TYPES or depth not in COLOR_TYPES[color][0]:
        raise PngError("unsupported colour type or bit depth")
    if compression or filtering or interlace not in (0, 1):
        raise PngError("unsupported PNG method")
    if not width or not height:
        raise PngError("empty image")
    return width, height, depth * COLOR_TYPES[color][1], interlace


def _raw_size(width, height, bits, interlace):
    """Exact size of the decompressed IDAT stream."""
    def pass_size(w, h):
        return h * (1 + (w * bits + 7) // 8) if w and h else 0
    if not interlace:
        return pass_size(width, height)
    total = 0
    for x0, y0, dx, dy in ADAM7:
        total += pass_size((width - x0 + dx - 1) // dx, (height - y0 + dy - 1) // dy)
    return total


def _check_stream(idat, expected):
    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(idat, expected + 1)
    except zlib.error as e:
        raise PngError("damaged image data") from e
    if len(raw) != expected or not inflater.eof or inflater.unused_data:
        raise PngError("image data does not match the header")


def clean(data, max_dim):
    """(stripped PNG bytes, width, height); PngError for anything that is not a plain, sound PNG.

    `max_dim` caps width and height (the header is read before anything is decompressed).
    """
    if data[:8] != SIGNATURE:
        raise PngError("not a PNG file")
    kept, header, idat, seen_end = [], None, [], False
    for kind, body in _chunks(data):
        if seen_end:
            raise PngError("data after IEND")
        if header is None:
            if kind != b"IHDR":
                raise PngError("IHDR is not the first chunk")
            header = _header(body)
            if max(header[0], header[1]) > max_dim:
                raise PngError("image larger than %d px" % max_dim)
        elif kind == b"IHDR":
            raise PngError("second IHDR")
        seen_end = kind == b"IEND"
        if kind in KEEP:
            kept.append((kind, body))
            if kind == b"IDAT":
                idat.append(body)
    if header is None or not seen_end or not idat:
        raise PngError("incomplete PNG")
    width, height, bits, interlace = header
    _check_stream(b"".join(idat), _raw_size(width, height, bits, interlace))
    out = [SIGNATURE]
    for kind, body in kept:
        out.append(struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body)))
    return b"".join(out), width, height
