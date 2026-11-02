# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server"))
from agino_store_sandbox import config, jobs, shots_guest, shots_img, shots_scene, vm  # noqa: E402


def ppm(w, h, pixel):
    return b"P6\n%d %d\n255\n" % (w, h) + b"".join(bytes(pixel(x, y)) for y in range(h) for x in range(w))


def decode_png(data):
    """Tiny RGBA 8-bit decoder for filter-0 PNGs: (width, height, rows)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, size = 8, b"", None
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        assert struct.unpack(">I", data[pos + 8 + n:pos + 12 + n])[0] == zlib.crc32(kind + body)
        if kind == b"IHDR":
            size = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    w, h, depth, ctype = size[:4]
    assert (depth, ctype) == (8, 6)
    raw = zlib.decompress(idat)
    stride = w * 4 + 1
    assert all(raw[i * stride] == 0 for i in range(h))
    return w, h, [raw[i * stride + 1:(i + 1) * stride] for i in range(h)]


class ImageTests(unittest.TestCase):
    def test_parse_ppm(self):
        w, h, rgb = shots_img.parse_ppm(b"P6\n# c\n2 1\n255\n\x01\x02\x03\x04\x05\x06")
        self.assertEqual((w, h, rgb), (2, 1, b"\x01\x02\x03\x04\x05\x06"))
        for bad in (b"P5\n1 1\n255\n\0", b"P6\n1 1\n255\n\0\0", b"P6\n99999 1\n255\n", b"junk", b"P6\n1 1\n65535\n" + b"\0" * 6):
            with self.assertRaises(shots_img.ImageError):
                shots_img.parse_ppm(bad)

    def test_crop_alpha_and_png(self):
        w, h, rgb = shots_img.parse_ppm(ppm(60, 40, lambda x, y: (x * 4, y * 6, 200)))
        rect = (10, 5, 40, 30)
        rows = shots_img.crop_rgba(w, rgb, rect, 8)
        png = shots_img.encode_png(40, 30, rows)
        dw, dh, out = decode_png(png)
        self.assertEqual((dw, dh), (40, 30))
        for x, y in ((0, 0), (39, 0), (0, 29), (39, 29)):
            self.assertEqual(out[y][x * 4 + 3], 0)
        self.assertEqual(out[15][20 * 4 + 3], 255)
        self.assertEqual(tuple(out[15][20 * 4:20 * 4 + 3]), (30 * 4, 20 * 6, 200))
        self.assertEqual(out[0][20 * 4 + 3], 255)  # straight edge between the corners stays opaque

    def test_radius_zero_and_oversized(self):
        w, h, rgb = shots_img.parse_ppm(ppm(10, 10, lambda x, y: (1, 2, 3)))
        rows = shots_img.crop_rgba(w, rgb, (0, 0, 10, 10), 0)
        self.assertTrue(all(r[3::4] == b"\xff" * 10 for r in rows))
        rows = shots_img.crop_rgba(w, rgb, (0, 0, 10, 10), 64)
        self.assertEqual(rows[0][3], 0)

    def test_stddev(self):
        w, h, flat = shots_img.parse_ppm(ppm(80, 80, lambda x, y: (9, 9, 9)))
        self.assertLess(shots_img.luminance_stddev(w, flat, (0, 0, 80, 80)), 1)
        w, h, noisy = shots_img.parse_ppm(ppm(80, 80, lambda x, y: (255, 255, 255) if (x // 4 + y // 4) % 2 else (0, 0, 0)))
        self.assertGreater(shots_img.luminance_stddev(w, noisy, (0, 0, 80, 80)), 50)

    def test_clamp_rect(self):
        self.assertEqual(shots_img.clamp_rect(-10, -10, 100, 100, 1920, 1080), (0, 0, 90, 90))
        self.assertEqual(shots_img.clamp_rect(1900, 1000, 500, 500, 1920, 1080), (1900, 1000, 20, 80))
        self.assertIsNone(shots_img.clamp_rect(3000, 0, 10, 10, 1920, 1080))
        self.assertIsNone(shots_img.clamp_rect("x", 0, 10, 10, 1920, 1080))
        self.assertIsNone(shots_img.clamp_rect(0, 0, 0, 10, 1920, 1080))


class ExecTests(unittest.TestCase):
    def build(self, line, args=()):
        return shots_scene.build_command(line, list(args))

    def test_field_codes(self):
        self.assertEqual(self.build("code %F", ["~/Demo/p", "b"]), ["code", "/home/tester/Demo/p", "b"])
        self.assertEqual(self.build("app %f", ["a", "b"]), ["app", "a"])
        self.assertEqual(self.build("app %U %i %c %k", ["u"]), ["app", "u"])
        self.assertEqual(self.build("app --open=%u 100%%", ["x"]), ["app", "--open=x", "100%"])
        self.assertEqual(self.build("app %F"), ["app"])
        self.assertEqual(self.build("app", ["x"]), ["app", "x"])

    def test_quoting(self):
        self.assertEqual(self.build('"/opt/My App/run" --flag "a \\"q\\" b" %f', ["f"]),
                         ["/opt/My App/run", "--flag", 'a "q" b', "f"])
        self.assertEqual(self.build('env A=1 "app" ""'), ["env", "A=1", "app", ""])
        self.assertIsNone(self.build('app "unterminated'))
        self.assertIsNone(self.build("--evil"))
        self.assertIsNone(self.build("   "))

    def test_read_exec(self):
        text = "[Desktop Action x]\nExec=wrong\n[Desktop Entry]\nName=A\nExec=right %U\n[Other]\nExec=no\n"
        self.assertEqual(shots_scene.read_exec(text), "right %U")
        self.assertIsNone(shots_scene.read_exec("[Desktop Entry]\nName=A\n"))


GOOD = {"schema": 1, "window": {"width": 1500, "height": 900}, "scenes": [
    {"id": "editor", "caption": {"zh_CN": "编辑", "en_US": "Edit"}, "demo": ["code-project"],
     "args": ["~/Demo/code-project"],
     "steps": [{"wait": 3}, {"key": "ctrl+shift+p"}, {"type": "hello"}, {"click": [200, 150]},
               {"click": [1, 2, "right"]}, {"wait_window": "Preferences"}]}]}


def deep(doc):
    return json.loads(json.dumps(doc))


class ValidationTests(unittest.TestCase):
    def test_good(self):
        out = shots_scene.validate(GOOD)
        sc = out["scenes"][0]
        self.assertEqual(sc["window"], (1500, 900))
        self.assertEqual(sc["steps"][0], ("wait", 3.0))
        self.assertEqual(sc["steps"][3], ("click", (200, 150, "left")))
        self.assertEqual(sc["steps"][4], ("click", (1, 2, "right")))
        self.assertEqual(sc["steps"][2], ("type", {"zh_CN": "hello", "en_US": "hello"}))

    def test_localized_steps_and_lang_args(self):
        doc = deep(GOOD)
        doc["scenes"][0]["steps"] = [{"wait_window": {"zh_CN": "首选项", "en_US": "Preferences"}},
                                     {"type": {"zh_CN": "你好", "en_US": "hello"}}]
        steps = shots_scene.validate(doc)["scenes"][0]["steps"]
        self.assertEqual(steps[0], ("wait_window", {"zh_CN": "首选项", "en_US": "Preferences"}))
        self.assertEqual(steps[1][1]["zh_CN"], "你好")
        self.assertEqual(shots_scene.scene_args(["~/Demo/website/{lang}/index.html", "-x"], "zh_CN"),
                         ["~/Demo/website/zh_CN/index.html", "-x"])

    def mutate(self, fn):
        doc = deep(GOOD)
        fn(doc)
        with self.assertRaises(shots_scene.ShotsError):
            shots_scene.validate(doc)

    def test_bad(self):
        scene = lambda d: d["scenes"][0]  # noqa: E731
        self.mutate(lambda d: d.update(schema=2))
        self.mutate(lambda d: d.update(scenes=[]))
        self.mutate(lambda d: d.update(scenes=[scene(d)] * 2))
        self.mutate(lambda d: d.update(window={"width": 100, "height": 900}))
        self.mutate(lambda d: scene(d).update(id="Bad Id"))
        self.mutate(lambda d: scene(d)["caption"].pop("en_US"))
        self.mutate(lambda d: scene(d)["caption"].update(en_US="x" * 81))
        self.mutate(lambda d: scene(d).update(demo=["../x"]))
        self.mutate(lambda d: scene(d).update(args=["a"] * 9))
        self.mutate(lambda d: scene(d).update(args=["a\nb"]))
        self.mutate(lambda d: scene(d).update(steps=[{"wait": 0}] * 31))
        self.mutate(lambda d: scene(d).update(steps=[{"wait": 21}]))
        self.mutate(lambda d: scene(d).update(steps=[{"wait": True}]))
        self.mutate(lambda d: scene(d).update(steps=[{"key": "ctrl shift"}]))
        self.mutate(lambda d: scene(d).update(steps=[{"type": "a\nb"}]))
        self.mutate(lambda d: scene(d).update(steps=[{"click": [1]}]))
        self.mutate(lambda d: scene(d).update(steps=[{"click": [1, 2, "back"]}]))
        self.mutate(lambda d: scene(d).update(steps=[{"click": [-1, 2]}]))
        self.mutate(lambda d: scene(d).update(steps=[{"wait": 1, "key": "a"}]))
        self.mutate(lambda d: scene(d).update(steps=[{"sleep": 1}]))
        self.mutate(lambda d: scene(d).update(capture="screen"))
        self.mutate(lambda d: scene(d).update(steps=[{"wait_window": {"zh_CN": "x"}}]))
        self.mutate(lambda d: scene(d).update(steps=[{"type": {"zh_CN": "a", "en_US": "b", "de_DE": "c"}}]))
        self.mutate(lambda d: scene(d).update(steps=[{"type": {"zh_CN": "a", "en_US": "a\nb"}}]))


class GeometryTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(shots_scene.parse_geometry("WINDOW=1\nX=10\nY=-5\nWIDTH=800\nHEIGHT=600\nSCREEN=0\n"),
                         (10, -5, 800, 600))
        self.assertIsNone(shots_scene.parse_geometry("X=1\nY=2\n"))
        self.assertIsNone(shots_scene.parse_geometry("X=999999999\nY=1\nWIDTH=1\nHEIGHT=1\n"))
        self.assertEqual(shots_scene.parse_extents("_NET_FRAME_EXTENTS(CARDINAL) = 1, 2, 40, 3"), (1, 2, 40, 3))
        self.assertEqual(shots_scene.parse_extents("_NET_FRAME_EXTENTS(CARDINAL) = 1, 2, 4000, 3"), (0, 0, 0, 0))
        self.assertEqual(shots_scene.parse_extents("not found"), (0, 0, 0, 0))

    def test_frame_clamped(self):
        screen = (1920, 1080)
        self.assertEqual(shots_scene.frame_rect((160, 40, 1600, 1000), (1, 1, 32, 1), screen), (159, 8, 1602, 1033))
        self.assertEqual(shots_scene.frame_rect((-50, -50, 5000, 5000), (0, 0, 0, 0), screen), (0, 0, 1920, 1080))
        self.assertIsNone(shots_scene.frame_rect(None, (0, 0, 0, 0), screen))
        self.assertIsNone(shots_scene.frame_rect((5000, 5000, 10, 10), (0, 0, 0, 0), screen))

    def test_hex_and_pids(self):
        self.assertEqual(shots_guest.hex_id("58720259"), "0x3800003")
        self.assertEqual(shots_guest.hex_id("0x3800003"), "0x3800003")
        self.assertIsNone(shots_guest.hex_id("junk"))
        self.assertIsNone(shots_guest.hex_id("0"))
        self.assertEqual(shots_guest.parse_pids("12\n300\nx; rm\n1234567890123\n"), {12, 300})


class JsonTests(unittest.TestCase):
    def test_image_problems(self):
        self.assertEqual(shots_scene.image_problems(800, 600, 30.0, []), [])
        got = shots_scene.image_problems(300, 200, 1.0, ["Save?"])
        self.assertEqual(len(got), 3)
        self.assertIn("extra dialog window: Save?", got)

    def test_build(self):
        job = jobs.Job("a_0123456789ab", "a", "1.0", "f" * 64, 1, "a", None, [], [], "/q/a.deb")
        sc = shots_scene.validate(GOOD)["scenes"][0]
        doc = shots_scene.build_shots_json(job, [{"scene": sc, "n": 1, "problems": ["zh_CN: window not found"],
                                                  "files": {"en_US": {"name": "1-editor.png", "sha256": "0" * 64,
                                                                      "width": 5, "height": 6}}}])
        self.assertEqual(doc["schema"], 1)
        self.assertEqual(doc["scenes"][0]["id"], "editor")
        self.assertEqual(doc["scenes"][0]["caption"]["en_US"], "Edit")
        self.assertEqual(list(doc["scenes"][0]["files"]), ["en_US"])
        json.dumps(doc)


class JobShotsTests(unittest.TestCase):
    def write_job(self, tmp, extra):
        import hashlib
        deb = b"deb"
        stem = "app_0123456789ab"
        with open(os.path.join(tmp, stem + ".deb"), "wb") as f:
            f.write(deb)
        doc = {"schema": 1, "app": "app", "version": "1.0", "sha256": hashlib.sha256(deb).hexdigest(),
               "size": len(deb), "package": "app", "launchable": None, "deb": stem + ".deb", **extra}
        with open(os.path.join(tmp, stem + ".json"), "w") as f:
            json.dump(doc, f)
        return stem + ".json"

    def test_no_shots(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = jobs.read_job(tmp, self.write_job(tmp, {}))
            self.assertIsNone(job.shots)
            self.assertIsNone(job.shots_error)

    def test_shots_and_demo(self):
        with tempfile.TemporaryDirectory() as tmp:
            name = self.write_job(tmp, {"shots": GOOD, "demo": "app_0123456789ab.demo.tar"})
            with open(os.path.join(tmp, "app_0123456789ab.demo.tar"), "wb") as f:
                f.write(b"tar")
            job = jobs.read_job(tmp, name)
            self.assertEqual(job.demo_size, 3)
            self.assertEqual(job.demo_path, os.path.join(tmp, "app_0123456789ab.demo.tar"))
            self.assertEqual(len(job.shots["scenes"]), 1)

    def test_bad_shots_only_disable_screenshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = jobs.read_job(tmp, self.write_job(tmp, {"shots": GOOD}))  # demo named but no demo field
            self.assertIsNone(job.shots)
            self.assertIn("demo", job.shots_error)
            job = jobs.read_job(tmp, self.write_job(tmp, {"shots": {"schema": 9}}))
            self.assertIsNone(job.shots)


class ConfigVmTests(unittest.TestCase):
    def test_defaults_and_legacy(self):
        cfg = config.from_dict({})
        self.assertTrue(cfg.shots)
        self.assertEqual((cfg.screen, cfg.corner_radius, cfg.shots_out),
                         ((1920, 1080), 8, "/srv/store/sandbox/shots-out"))
        self.assertFalse(config.from_dict({"shots": "off"}).shots)
        legacy = config.from_dict({"shots": "/x/png"})
        self.assertTrue(legacy.shots)
        self.assertEqual(legacy.shot_dir, "/x/png")
        for bad in ({"shots": "maybe"}, {"screen": "10x10"}, {"corner_radius": "99"}):
            with self.assertRaises(Exception):
                config.from_dict(bad)

    def test_argv_screen_and_demo(self):
        cfg = config.from_dict({"screen": "1280x800"})
        files = vm.RunFiles("/run/0")
        joined = " ".join(vm.build_argv(cfg, 0, files, "/q/x.deb", False, "/q/x.demo.tar"))
        self.assertIn("virtio-vga,xres=1280,yres=800", joined)
        self.assertIn("serial=sbxdemo", joined)
        self.assertIn("readonly=on,id=demo", joined)
        self.assertNotIn("sbxdemo", " ".join(vm.build_argv(cfg, 0, files, "/q/x.deb", False)))


if __name__ == "__main__":
    unittest.main()


class FakeWindows:
    """Guest stand-in for Phase._main_window: a list of window sets returned by successive polls."""

    def __init__(self, polls, types, areas):
        self.polls, self.types, self.areas = list(polls), types, areas

    def windows(self):
        return set(self.polls.pop(0) if len(self.polls) > 1 else self.polls[0])

    def is_main_candidate(self, wid):
        return self.types.get(wid, "NORMAL") not in ("SPLASH", "DIALOG")

    def geometry(self, wid):
        return (0, 0, self.areas[wid], 1)


class MainWindowTests(unittest.TestCase):
    def find(self, polls, types=None, areas=None):
        from unittest import mock
        from agino_store_sandbox import shots
        phase = shots.Phase.__new__(shots.Phase)
        phase.g = FakeWindows(polls, types or {}, areas or {"0x1": 900, "0x2": 500, "0x3": 2000})
        with mock.patch.object(shots.time, "sleep"):
            return phase._main_window(set())

    def test_splash_is_skipped_and_window_must_be_stable(self):
        polls = [{"0x3"}, {"0x3", "0x2"}, {"0x2"}, {"0x2", "0x1"}, {"0x2", "0x1"}, {"0x2", "0x1"}]
        self.assertEqual(self.find(polls, types={"0x3": "SPLASH"}), "0x1")

    def test_dialog_alone_is_no_main_window(self):
        from unittest import mock
        from agino_store_sandbox import shots
        with mock.patch.object(shots, "WINDOW_WAIT", 0):
            self.assertIsNone(self.find([{"0x2"}], types={"0x2": "DIALOG"}))


class SwitchLocaleTests(unittest.TestCase):
    def guest(self, mkdir_ok):
        from unittest import mock
        g = mock.Mock()
        g.qga.sh.return_value = mock.Mock(ok=mkdir_ok)
        g.qga.exec.return_value = mock.Mock(ok=True)
        return g

    def test_work_dir_is_created_before_the_script(self):
        g = self.guest(True)
        self.assertTrue(shots_guest.ShotsGuest(g).switch_locale("zh_CN"))
        self.assertEqual(g.qga.sh.call_args.args[0], shots_guest.MAKE_WORK_DIR)
        self.assertEqual(g.qga.write_file.call_args.args[0], shots_guest.SWITCH_SCRIPT_PATH)

    def test_work_dir_failure_is_infra_error(self):
        from agino_store_sandbox.util import InfraError
        g = self.guest(False)
        with self.assertRaises(InfraError):
            shots_guest.ShotsGuest(g).switch_locale("en_US")
        self.assertFalse(g.qga.write_file.called)
