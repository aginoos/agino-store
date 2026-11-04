# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from session_helpers import FakeQmp, FakeShotsGuest, decode_rgb_png, make_ppm  # noqa: E402

from agino_store_sandbox import session_capture as C  # noqa: E402
from agino_store_sandbox.util import InfraError  # noqa: E402

def slurp(path):
    with open(path, "rb") as f:
        return f.read()


NORMAL = "_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NORMAL"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ppm = os.path.join(self.tmp.name, "shot.ppm")
        self.dir = os.path.join(self.tmp.name, "caps", "0123456789abcdef")
        self.store = C.CaptureStore(self.dir, "0123456789abcdef", 3)
        self.store.create_directory()

    def capturer(self, data, locator=None, **kw):
        return C.Capturer(self.store, FakeQmp(data, **kw), self.ppm, locator)


class PipelineTests(Base):
    def test_screen_capture(self):
        item = self.capturer(make_ppm(1920, 1080)).capture("screen", "zh_CN", "app", "zh_CN")
        self.assertEqual({k: item[k] for k in ("n", "file", "kind", "lang", "width", "height", "title", "problems")},
                         {"n": 1, "file": "01-screen-zh_CN.png", "kind": "screen", "lang": "zh_CN", "width": 1920,
                          "height": 1080, "title": "", "problems": []})
        self.assertNotIn("time", item)
        path = os.path.join(self.dir, item["file"])
        data = slurp(path)
        self.assertEqual(item["size"], len(data))
        w, h, rgb = decode_rgb_png(data)
        self.assertEqual((w, h, rgb[:3]), (1920, 1080, b"\x10\x20\x30"))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)
        self.assertFalse(os.path.exists(self.ppm))
        index = json.loads(slurp(os.path.join(self.dir, "captures.json")))
        self.assertEqual(index["items"][0]["sha256"], item["sha256"])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, "captures.json")).st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode) & 0o777, 0o750)   # setgid comes from the parent

    def test_items_carry_the_app_and_the_desktop_language(self):
        cap = self.capturer(make_ppm(1920, 1080))
        item = cap.capture("screen", "en_US", "audacity", "en_US")
        self.assertEqual((item["app"], item["lang"], item["desktop_lang"]), ("audacity", "en_US", "en_US"))
        other = cap.capture("screen", "zh_CN", "gimp", "zh_CN")
        index = json.loads(slurp(os.path.join(self.dir, "captures.json")))
        self.assertEqual([(i["n"], i["app"], i["desktop_lang"]) for i in index["items"]],
                         [(1, "audacity", "en_US"), (2, "gimp", "zh_CN")])
        self.assertNotIn("app", index)  # the app is per capture, a session captures many
        self.assertEqual(other["file"], "02-screen-zh_CN.png")

    def test_bad_app_or_desktop_language(self):
        for app, desktop in (("", "zh_CN"), ("a", "fr_FR")):
            with self.assertRaises(C.CaptureError) as ctx:
                self.capturer(make_ppm(640, 480)).capture("screen", "zh_CN", app, desktop)
            self.assertEqual(ctx.exception.code, "bad_request")

    def test_bad_screendumps(self):
        cases = {
            "bad header": b"P5\n1920 1080\n255\n" + b"\0" * 10,
            "garbage": b"hello",
            "too narrow": make_ppm(639, 480),
            "too short": make_ppm(640, 479),
            "too wide": b"P6\n4097 640\n255\n" + b"\0" * 100,
            "huge number": b"P6\n999999 999999\n255\n",
            "maxval": b"P6\n640 480\n65535\n" + b"\0" * (640 * 480 * 6),
            "truncated": make_ppm(640, 480)[:-1],
            "trailing": make_ppm(640, 480) + b"x",
        }
        for name, data in cases.items():
            with self.assertRaises(C.CaptureError, msg=name) as ctx:
                self.capturer(data).capture("screen", "en_US", "app", "zh_CN")
            self.assertEqual(ctx.exception.code, "bad_image", name)
            self.assertFalse(os.path.exists(self.ppm), name)
        self.assertEqual(self.store.count(), 0)

    def test_oversized_dump_is_removed_unread(self):
        class Big(FakeQmp):
            def screendump(self, filename, fmt="png"):
                with open(filename, "wb") as f:
                    f.truncate(C.MAX_PPM_FILE + 1)
        capturer = C.Capturer(self.store, Big(), self.ppm, None)
        with self.assertRaises(C.CaptureError) as ctx:
            capturer.capture("screen", "zh_CN", "app", "zh_CN")
        self.assertEqual(ctx.exception.code, "bad_image")
        self.assertFalse(os.path.exists(self.ppm))

    def test_symlinked_dump_is_refused(self):
        secret = os.path.join(self.tmp.name, "secret")
        with open(secret, "wb") as f:
            f.write(make_ppm(640, 480))

        class Link(FakeQmp):
            def screendump(self, filename, fmt="png"):
                os.symlink(secret, filename)
        with self.assertRaises(C.CaptureError):
            C.Capturer(self.store, Link(), self.ppm, None).capture("screen", "zh_CN", "app", "zh_CN")
        self.assertTrue(os.path.exists(secret))

    def test_qmp_failure(self):
        with self.assertRaises(C.CaptureError) as ctx:
            self.capturer(b"", fail=True).capture("screen", "zh_CN", "app", "zh_CN")
        self.assertEqual(ctx.exception.code, "capture_failed")

    def test_too_big_is_rejected_and_nothing_is_left(self):
        noise = make_ppm(1024, 1100, pixel=b"")
        noise = noise + os.urandom(1024 * 1100 * 3)
        with self.assertRaises(C.CaptureError) as ctx:
            self.capturer(noise).capture("screen", "zh_CN", "app", "zh_CN")
        self.assertEqual(ctx.exception.code, "too_big")
        self.assertEqual(sorted(os.listdir(self.dir)), ["captures.json"])
        self.assertEqual(self.store.count(), 0)
        self.assertFalse(os.path.exists(self.ppm))

    def test_limit_and_numbering(self):
        cap = self.capturer(make_ppm(640, 480))
        names = [cap.capture("screen", "zh_CN", "app", "zh_CN")["file"] for _ in range(3)]
        self.assertEqual(names, ["01-screen-zh_CN.png", "02-screen-zh_CN.png", "03-screen-zh_CN.png"])
        with self.assertRaises(C.CaptureError) as ctx:
            cap.capture("screen", "en_US", "app", "zh_CN")
        self.assertEqual(ctx.exception.code, "too_many")
        self.assertEqual(self.store.count(), 3)

    def test_bad_arguments(self):
        for kind, lang in (("full", "zh_CN"), ("screen", "fr_FR"), ("", "zh_CN")):
            with self.assertRaises(C.CaptureError) as ctx:
                self.capturer(make_ppm(640, 480)).capture(kind, lang, "app", "zh_CN")
            self.assertEqual(ctx.exception.code, "bad_request")

    def test_problems_for_small_images(self):
        self.assertEqual(C.problems_for(899, 300), ["too_narrow"])
        self.assertEqual(C.problems_for(900, 299), ["too_short"])
        self.assertEqual(C.problems_for(100, 100), ["too_narrow", "too_short"])
        self.assertEqual(C.problems_for(900, 300), [])

    def test_store_read_and_snapshot(self):
        cap = self.capturer(make_ppm(640, 480))
        item = cap.capture("screen", "zh_CN", "app", "zh_CN")
        self.assertEqual(self.store.read(1), slurp(os.path.join(self.dir, item["file"])))
        self.assertIsNone(self.store.read(2))
        self.assertEqual(self.store.snapshot(), [item])


class WindowTests(Base):
    def locator(self, **kw):
        return C.GuestLocator(FakeShotsGuest(**kw))

    def test_window_capture_crops_and_cleans_title(self):
        ppm = bytearray(make_ppm(1920, 1080))
        sg = FakeShotsGuest({"0x1": {"type": NORMAL, "geo": (100, 100, 1000, 700), "ext": (2, 2, 30, 2),
                                     "title": "My\x1b[31m App\x00"}}, active="0x1")
        item = self.capturer(bytes(ppm), C.GuestLocator(sg)).capture("window", "en_US", "app", "zh_CN")
        self.assertEqual((item["width"], item["height"]), (1004, 732))
        self.assertEqual(item["file"], "01-window-en_US.png")
        self.assertNotIn("\x1b", item["title"])
        self.assertNotIn("\x00", item["title"])
        self.assertEqual(item["problems"], [])

    def test_small_window_is_kept_with_problems(self):
        sg = FakeShotsGuest({"0x1": {"geo": (0, 0, 800, 250)}}, active="0x1")
        item = self.capturer(make_ppm(1920, 1080), C.GuestLocator(sg)).capture("window", "zh_CN", "app", "zh_CN")
        self.assertEqual(item["problems"], ["too_narrow", "too_short"])

    def test_window_outside_screen_is_clamped(self):
        sg = FakeShotsGuest({"0x1": {"geo": (1800, 1000, 600, 600)}}, active="0x1")
        item = self.capturer(make_ppm(1920, 1080), C.GuestLocator(sg)).capture("window", "zh_CN", "app", "zh_CN")
        self.assertEqual((item["width"], item["height"]), (120, 80))

    def test_window_fully_outside_the_screen(self):
        sg = FakeShotsGuest({"0x1": {"geo": (5000, 5000, 600, 600)}}, active="0x1")
        with self.assertRaises(C.CaptureError) as ctx:
            self.capturer(make_ppm(1920, 1080), C.GuestLocator(sg)).capture("window", "zh_CN", "app", "zh_CN")
        self.assertEqual(ctx.exception.code, "no_window")

    def test_active_dialog_allowed_dock_not(self):
        win = {"0x1": {"type": NORMAL, "geo": (0, 0, 1000, 800), "title": "main"},
               "0x2": {"type": "_NET_WM_WINDOW_TYPE_DIALOG", "geo": (10, 10, 300, 200), "title": "dlg"},
               "0x3": {"type": "_NET_WM_WINDOW_TYPE_DOCK", "geo": (0, 1000, 1920, 80), "title": "dock"}}
        loc = self.locator(windows=win, active="0x2")
        self.assertEqual(loc.locate((1920, 1080))[0], (10, 10, 300, 200))
        loc = self.locator(windows=win, active="0x3")  # the dock is not acceptable: the largest normal window
        self.assertEqual(loc.locate((1920, 1080))[0], (0, 0, 1000, 800))
        loc = self.locator(windows=win, active=None)
        self.assertEqual(loc.locate((1920, 1080))[1], "main")

    def test_largest_normal_window_when_active_unknown(self):
        win = {"0x1": {"geo": (0, 0, 100, 100)}, "0x2": {"type": NORMAL, "geo": (0, 0, 500, 400)},
               "0x3": {"type": "_NET_WM_WINDOW_TYPE_DIALOG", "geo": (0, 0, 900, 900)}}
        rect, _ = self.locator(windows=win, active="0x9").locate((1920, 1080))
        self.assertEqual(rect, (0, 0, 500, 400))

    def test_no_window_and_no_xdotool(self):
        with self.assertRaises(C.CaptureError) as ctx:
            self.locator(windows={}).locate((1920, 1080))
        self.assertEqual(ctx.exception.code, "no_window")
        with self.assertRaises(C.CaptureError) as ctx:
            self.locator(xdotool=False).locate((1920, 1080))
        self.assertEqual(ctx.exception.code, "no_xdotool")

    def test_guest_failure_marks_the_agent_as_broken(self):
        sg = FakeShotsGuest({"0x1": {"geo": (0, 0, 500, 400)}}, active="0x1")
        sg.fail = True
        with self.assertRaises(C.CaptureError) as ctx:
            self.capturer(make_ppm(640, 480), C.GuestLocator(sg)).capture("window", "zh_CN", "app", "zh_CN")
        self.assertEqual(ctx.exception.code, "guest_failed")
        self.assertTrue(ctx.exception.qga_broken)
        self.assertEqual(self.store.count(), 0)

    def test_budget(self):
        ticks = iter(range(0, 1000, 20))
        sg = FakeShotsGuest({"0x1": {"geo": (0, 0, 500, 400)}}, active="0x1")
        loc = C.GuestLocator(sg, budget=30, clock=lambda: next(ticks))
        with self.assertRaises(InfraError):
            loc.locate((1920, 1080))

    def test_screen_kind_never_touches_the_guest(self):
        class Boom:
            def locate(self, screen):
                raise AssertionError("guest used")
        self.capturer(make_ppm(640, 480), Boom()).capture("screen", "zh_CN", "app", "zh_CN")


class EncodingTests(unittest.TestCase):
    def test_png_is_plain_rgb(self):
        png = C.encode_png_rgb(3, 2, bytes(range(18)))
        self.assertEqual(decode_rgb_png(png), (3, 2, bytes(range(18))))

    def test_crop(self):
        rgb = bytes(i % 256 for i in range(4 * 4 * 3))
        out = C.crop_rgb(4, rgb, (1, 1, 2, 2))
        self.assertEqual(out, rgb[15:21] + rgb[27:33])


if __name__ == "__main__":
    unittest.main()
