# SPDX-License-Identifier: GPL-3.0-or-later
import struct
import unittest

from agino_store_server import pngclean

from .shots_helpers import chunk, chunk_kinds, make_png


class TestPngClean(unittest.TestCase):
    def test_strips_ancillary_chunks(self):
        data = make_png(extra=[(b"tEXt", b"Comment\x00secret"), (b"tRNS", b"\x00\x01"), (b"eXIf", b"x" * 40),
                               (b"PLTE", b"\x00\x00\x00")])
        cleaned, width, height = pngclean.clean(data, 4096)
        self.assertEqual((width, height), (900, 320))
        self.assertEqual(chunk_kinds(cleaned), ["IHDR", "tRNS", "PLTE", "IDAT", "IEND"])
        self.assertEqual(pngclean.clean(cleaned, 4096)[0], cleaned)

    def test_rejects_damage(self):
        good = make_png()
        cases = {
            "signature": b"GIF89a" + good[6:],
            "empty": b"",
            "truncated": good[:-20],
            "trailing": good + b"junk",
            "crc": good[:60] + bytes([good[60] ^ 1]) + good[61:],
            "no idat": good[:8] + good[8:33] + good[-12:],
        }
        for name, data in cases.items():
            with self.subTest(name), self.assertRaises(pngclean.PngError):
                pngclean.clean(data, 4096)

    def test_data_must_match_header(self):
        other = make_png(width=901, height=320)
        idat_a = make_png()[33:-12]    # IDAT chunk of the 900 wide image
        forged = other[:33] + idat_a + other[-12:]
        with self.assertRaises(pngclean.PngError):
            pngclean.clean(forged, 4096)

    def test_decompression_bomb_is_bounded(self):
        import zlib
        head = struct.pack(">IIBBBBB", 900, 320, 8, 2, 0, 0, 0)
        bomb = zlib.compress(b"\x00" * (200 << 20))
        data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head) + chunk(b"IDAT", bomb) + chunk(b"IEND", b""))
        with self.assertRaises(pngclean.PngError):
            pngclean.clean(data, 4096)

    def test_dimension_cap_and_first_chunk(self):
        with self.assertRaises(pngclean.PngError):
            pngclean.clean(make_png(width=5000, height=10), 4096)
        good = make_png()
        swapped = good[:8] + good[33:-12] + good[8:33] + good[-12:]
        with self.assertRaises(pngclean.PngError):
            pngclean.clean(swapped, 4096)

    def test_bad_header_values(self):
        for head in (struct.pack(">IIBBBBB", 900, 320, 7, 2, 0, 0, 0), struct.pack(">IIBBBBB", 900, 320, 8, 5, 0, 0, 0),
                     struct.pack(">IIBBBBB", 0, 320, 8, 2, 0, 0, 0), struct.pack(">IIBBBBB", 900, 320, 8, 2, 1, 0, 0),
                     struct.pack(">IIBBBBB", 900, 320, 8, 2, 0, 0, 2)):
            data = make_png()
            forged = data[:8] + chunk(b"IHDR", head) + data[33:]
            with self.subTest(head.hex()), self.assertRaises(pngclean.PngError):
                pngclean.clean(forged, 4096)


if __name__ == "__main__":
    unittest.main()
