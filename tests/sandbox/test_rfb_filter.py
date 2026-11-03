# SPDX-License-Identifier: GPL-3.0-or-later
import os
import random
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "server"))

from agino_store_sandbox import rfb_filter as R  # noqa: E402

HANDSHAKE = R.VERSION + b"\x01" + b"\x01"
GOOD_PF = struct.pack(">BBBBHHHBBB", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)


def pixel_format(body=GOOD_PF, pad=b"\0\0\0"):
    return b"\x00\0\0\0" + body + pad


def set_encodings(codes):
    return struct.pack(">BxH%di" % len(codes), 2, len(codes), *codes)


def update_request(inc=1, x=0, y=0, w=1920, h=1080):
    return struct.pack(">BBHHHH", 3, inc, x, y, w, h)


def key(down=1, sym=0x61):
    return struct.pack(">BB2xI", 4, down, sym)


def pointer(mask=1, x=10, y=20):
    return struct.pack(">BBHH", 5, mask, x, y)


def fresh():
    f = R.ClientFilter()
    assert f.feed(HANDSHAKE) == HANDSHAKE
    return f


def validate_stream(data):
    """Independent whitelist parser for forwarded bytes: raises AssertionError on anything not allowed."""
    assert data.startswith(HANDSHAKE), data[:20]
    pos = len(HANDSHAKE)
    while pos < len(data):
        kind = data[pos]
        if kind == 0:
            msg = data[pos:pos + 20]
            assert len(msg) == 20 and msg[1:4] == b"\0\0\0" and msg[17:20] == b"\0\0\0"
            bpp, depth, big, tc = msg[4], msg[5], msg[6], msg[7]
            assert bpp in (8, 16, 32) and depth <= bpp and big <= 1 and tc <= 1
            pos += 20
        elif kind == 2:
            count = struct.unpack(">H", data[pos + 2:pos + 4])[0]
            assert count <= 20 and data[pos + 1] == 0
            codes = struct.unpack(">%di" % count, data[pos + 4:pos + 4 + 4 * count])
            assert all(c in (0, 1, 7, 16, -239, -223, -308, -224) or -32 <= c <= -23 or -256 <= c <= -247
                       for c in codes), codes
            pos += 4 + 4 * count
        elif kind == 3:
            assert len(data[pos:pos + 10]) == 10 and data[pos + 1] <= 1
            pos += 10
        elif kind == 4:
            assert len(data[pos:pos + 8]) == 8 and data[pos + 1] <= 1 and data[pos + 2:pos + 4] == b"\0\0"
            pos += 8
        elif kind == 5:
            assert len(data[pos:pos + 6]) == 6
            pos += 6
        else:
            raise AssertionError("type %d forwarded" % kind)
    assert pos == len(data)


class HandshakeTests(unittest.TestCase):
    def test_good_handshake_is_forwarded_verbatim(self):
        self.assertEqual(R.ClientFilter().feed(HANDSHAKE), HANDSHAKE)

    def test_bad_versions(self):
        for version in (b"RFB 003.003\n", b"RFB 003.007\n", b"RFB 003.889\n", b"GET / HTTP/1.1", b"RFB 003.008\r",
                        b"rfb 003.008\n"):
            with self.assertRaises(R.RfbReject, msg=version):
                R.ClientFilter().feed(version.ljust(12, b"x"))

    def test_security_must_be_none(self):
        for choice in (0, 2, 16, 255):
            f = R.ClientFilter()
            f.feed(R.VERSION)
            with self.assertRaises(R.RfbReject):
                f.feed(bytes([choice]))

    def test_client_init_values(self):
        for value in (0, 1):
            f = R.ClientFilter()
            self.assertEqual(f.feed(R.VERSION + b"\x01" + bytes([value])), R.VERSION + b"\x01" + bytes([value]))
        f = R.ClientFilter()
        with self.assertRaises(R.RfbReject):
            f.feed(R.VERSION + b"\x01\x02")

    def test_rejection_is_sticky(self):
        f = R.ClientFilter()
        with self.assertRaises(R.RfbReject):
            f.feed(b"x" * 12)
        with self.assertRaises(R.RfbReject):
            f.feed(HANDSHAKE)


class MessageTests(unittest.TestCase):
    def test_valid_messages_pass(self):
        table = [
            (pixel_format(), pixel_format()),
            (update_request(), update_request()),
            (update_request(0, 1, 2, 3, 4), update_request(0, 1, 2, 3, 4)),
            (key(), key()),
            (key(0, 0xFFFFFFFF), key(0, 0xFFFFFFFF)),
            (pointer(), pointer()),
            (pointer(255, 65535, 65535), pointer(255, 65535, 65535)),
            (set_encodings([]), set_encodings([])),
            (set_encodings([7, 16, 0, 1, -239, -223, -308, -224]), set_encodings([7, 16, 0, 1, -239, -223, -308, -224])),
        ]
        for sent, expected in table:
            self.assertEqual(fresh().feed(sent), expected, sent)

    def test_padding_is_zeroed(self):
        f = fresh()
        dirty = b"\x00\x01\x02\x03" + GOOD_PF + b"\x07\x08\x09"
        self.assertEqual(f.feed(dirty), pixel_format())
        self.assertEqual(f.feed(b"\x04\x01\xaa\xbb\x00\x00\x00\x61"), key())

    def test_pixel_format_cases(self):
        def pf(**over):
            v = dict(bpp=32, depth=24, big=0, tc=1, rmax=255, gmax=255, bmax=255, rs=16, gs=8, bs=0)
            v.update(over)
            return pixel_format(struct.pack(">BBBBHHHBBB", v["bpp"], v["depth"], v["big"], v["tc"], v["rmax"],
                                            v["gmax"], v["bmax"], v["rs"], v["gs"], v["bs"]))
        for good in (pf(), pf(bpp=16, depth=16, rmax=31, gmax=63, bmax=31, rs=11, gs=5, bs=0),
                     pf(bpp=8, depth=8, rmax=7, gmax=7, bmax=3, rs=0, gs=3, bs=6), pf(bpp=8, depth=8, tc=0),
                     pf(big=1)):
            fresh().feed(good)
        for bad in (pf(bpp=24), pf(bpp=0), pf(bpp=64), pf(depth=33), pf(depth=0), pf(bpp=16, depth=24), pf(big=2),
                    pf(tc=2), pf(rmax=0), pf(rmax=254), pf(gmax=65535, gs=31), pf(rs=31), pf(bpp=32, tc=0)):
            with self.assertRaises(R.RfbReject, msg=bad):
                fresh().feed(bad)

    def test_encodings_are_stripped_and_count_rewritten(self):
        codes = [7, -258, 16, -259, 0, 5, 6, 1, -32, -23, -22, -33, -256, -247, -246, -239, -240, -223, -308, -224,
                 2147483647][:20]
        out = fresh().feed(set_encodings(codes))
        kept = [7, 16, 0, 1, -32, -23, -256, -247, -239, -223, -308, -224]
        self.assertEqual(out, set_encodings(kept))
        self.assertEqual(fresh().feed(set_encodings([-258, -259, 255])), set_encodings([]))

    def test_novnc_default_encoding_list_is_accepted(self):
        # what @novnc/novnc 1.5.0 sends: 22 entries, most of them stripped
        codes = [7, 6, 5, 16, 0, 1, -260, -314, -308, -307, -306, -305, -304, -303, -302, -301, -258, -257, -224,
                 -223, -240, -239]
        self.assertEqual(len(codes), 22)
        self.assertEqual(fresh().feed(set_encodings(codes)), set_encodings([7, 16, 0, 1, -308, -224, -223, -239]))

    def test_encoding_count_limit(self):
        self.assertEqual(fresh().feed(set_encodings([0] * 64)), set_encodings([0] * 64))
        with self.assertRaises(R.RfbReject):
            fresh().feed(struct.pack(">BxH", 2, 65))
        with self.assertRaises(R.RfbReject):
            fresh().feed(struct.pack(">BxH", 2, 65535))

    def test_forbidden_types(self):
        for kind in (1, 6, 7, 8, 9, 100, 128, 249, 250, 251, 254, 255):
            with self.assertRaises(R.RfbReject, msg=kind):
                fresh().feed(bytes([kind]) + b"\0" * 40)
        with self.assertRaises(R.RfbReject):
            fresh().feed(b"\x06\0\0\0\0\0\0\x05hello")  # ClientCutText

    def test_bad_flags(self):
        for bad in (b"\x04\x02\0\0\0\0\0a", update_request(2)):
            with self.assertRaises(R.RfbReject):
                fresh().feed(bad)

    def test_truncated_messages_stay_buffered(self):
        for msg in (pixel_format(), update_request(), key(), pointer(), set_encodings([7, 0])):
            for cut in range(1, len(msg)):
                f = fresh()
                self.assertEqual(f.feed(msg[:cut]), b"")
                self.assertEqual(f.feed(msg[cut:]), msg)

    def test_multiple_messages_in_one_read(self):
        stream = update_request() + key() + key(0) + pointer() + set_encodings([7, -258]) + pointer(0, 5, 5)
        out = fresh().feed(stream)
        self.assertEqual(out, update_request() + key() + key(0) + pointer() + set_encodings([7]) + pointer(0, 5, 5))

    def test_byte_at_a_time(self):
        stream = HANDSHAKE + pixel_format() + set_encodings([7, -258, 0]) + update_request() + key() + pointer()
        f = R.ClientFilter()
        out = b"".join(f.feed(bytes([b])) for b in stream)
        self.assertEqual(out, HANDSHAKE + pixel_format() + set_encodings([7, 0]) + update_request() + key() + pointer())

    def test_input_events_counted_and_reported(self):
        seen = []
        f = R.ClientFilter(on_input=seen.append)
        f.feed(HANDSHAKE + update_request() + set_encodings([0]))
        self.assertEqual((f.input_events, seen), (0, []))
        f.feed(key() + pointer() + pointer() + update_request())
        self.assertEqual((f.key_events, f.pointer_events, seen), (1, 2, [3]))

    def test_buffer_does_not_grow(self):
        f = fresh()
        for _ in range(1000):
            f.feed(update_request()[:5])
            f.feed(update_request()[5:])
        self.assertEqual(len(f._buf), 0)


class FuzzTests(unittest.TestCase):
    def test_random_bytes_after_handshake(self):
        rng = random.Random(20261103)
        for _ in range(4000):
            data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 120)))
            f = R.ClientFilter()
            try:
                out = f.feed(HANDSHAKE)
                for part in self.split(rng, data):
                    out += f.feed(part)
            except R.RfbReject:
                continue
            validate_stream(out)

    def test_mutated_valid_streams(self):
        rng = random.Random(7)
        base = (pixel_format() + set_encodings([7, 16, 0, -258, -239]) + update_request() + key() + pointer()
                + key(0))
        for _ in range(4000):
            data = bytearray(base)
            for _ in range(rng.randrange(1, 6)):
                data[rng.randrange(len(data))] = rng.randrange(256)
            if rng.random() < 0.3:
                del data[rng.randrange(len(data)):]
            f = R.ClientFilter()
            try:
                out = f.feed(HANDSHAKE)
                for part in self.split(rng, bytes(data)):
                    out += f.feed(part)
            except R.RfbReject:
                continue
            validate_stream(out)

    @staticmethod
    def split(rng, data):
        pos = 0
        while pos < len(data):
            n = rng.randrange(1, 40)
            yield data[pos:pos + n]
            pos += n

    def test_random_bytes_from_the_start(self):
        rng = random.Random(99)
        for _ in range(2000):
            f = R.ClientFilter()
            try:
                f.feed(bytes(rng.randrange(256) for _ in range(rng.randrange(0, 40))))
            except R.RfbReject:
                pass


if __name__ == "__main__":
    unittest.main()
