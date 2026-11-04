# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import json
import os
import unittest

from . import helpers as H
from .shots_helpers import ShotsCase, chunk_kinds, make_png, slurp

SID = "0123456789abcdef"


class SessionCase(ShotsCase):
    def capture(self, n=1, kind="window", lang="zh_CN", sid=SID, data=None, app="gimp", index=True, **png):
        """Write a capture file and (unless index=False) record it, with its app, in captures.json like the daemon."""
        name = "%02d-%s-%s.png" % (n, kind, lang)
        H.write(os.path.join(self.captures, sid, name), data if data is not None else make_png(seed=n * 3, **png))
        if index:
            self.index_items = getattr(self, "index_items", {})
            items = self.index_items.setdefault(sid, [])
            items.append({"n": n, "file": name, "kind": kind, "lang": lang, "app": app, "desktop_lang": "zh_CN"})
            H.write(os.path.join(self.captures, sid, "captures.json"),
                    json.dumps({"schema": 2, "session": sid, "items": items}), "w")
        return name

    def write_index(self, *names, app="gimp", sid=SID):
        """captures.json for files that were created by hand (symlinks, fifos...)."""
        items = [{"n": int(n[:2]), "file": n, "app": app} for n in names]
        H.write(os.path.join(self.captures, sid, "captures.json"), json.dumps({"items": items}), "w")

    def imp(self, pick, sid=SID, app="gimp", reviewer="alice"):
        return self.run_cli("shots-session-import", app, "--session", sid, "--reviewer", reviewer, "--pick", pick)


class TestImport(SessionCase):
    def test_imports_drafts_with_manual_source(self):
        self.capture(1, "window", "zh_CN")
        self.capture(2, "screen", "en_US")
        self.capture(3, "screen", "en_US")  # not picked
        code, out, err = self.imp("01,02")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("imported 2, skipped 0, problems 0", out)
        images = sorted(self.images(), key=lambda i: i["lang"])
        self.assertEqual([(i["lang"], i["file"], i["scene"]) for i in images],
                         [("en_US", "01-session-02.png", "session-02"), ("zh_CN", "01-session-01.png", "session-01")])
        for i in images:
            self.assertEqual((i["status"], i["source"], i["origin"], i["reviewer"]), ("draft", "manual", "session:" + SID, None))
            self.assertEqual(hashlib.sha256(slurp(os.path.join(self.root, "gimp", i["lang"], i["file"]))).hexdigest(), i["sha256"])

    def test_strips_extra_chunks(self):
        self.capture(1, extra=[(b"tEXt", b"a\x00b")])
        self.imp("01")
        self.assertEqual(chunk_kinds(slurp(os.path.join(self.root, "gimp", "zh_CN", "01-session-01.png"))),
                         ["IHDR", "IDAT", "IEND"])

    def test_forged_captures_json_is_ignored(self):
        self.capture(1, width=900, height=320)
        forged = {"items": [{"n": 1, "file": "01-window-zh_CN.png", "app": "gimp", "kind": "screen", "lang": "en_US",
                             "width": 5, "height": 5, "size": 1, "sha256": "f" * 64}]}
        H.write(os.path.join(self.captures, SID, "captures.json"), json.dumps(forged), "w")
        code, out, _ = self.imp("01")
        self.assertEqual(code, 0)
        image = self.images()[0]
        self.assertEqual((image["lang"], image["width"], image["height"]), ("zh_CN", 900, 320))
        self.assertNotEqual(image["sha256"], "f" * 64)

    def test_3_mib_limit_for_session_images_only(self):
        self.capture(1, width=900, height=700, noise=True)       # about 1.9 MiB: too big elsewhere, fine here
        code, out, _ = self.imp("01")
        self.assertEqual(code, 0, out)
        image = self.images()[0]
        self.assertGreater(image["size"], 3 << 19)
        self.assertEqual(self.review("01-session-01.png", "approved")[0], 0)  # approving re-checks with the 3 MiB limit
        self.capture(2, lang="en_US", width=1100, height=1000, noise=True)   # about 3.3 MiB
        code, out, _ = self.imp("02")
        self.assertEqual(code, 1)
        self.assertIn("at most %d" % (3 << 20), out)
        self.assertEqual(len(self.images()), 1)

    def test_ordinary_images_keep_1_5_mib(self):
        name = "stage-big-0001.png"
        code, _, err = self.ingest(name, make_png(width=900, height=700, noise=True), source="manual")
        self.assertNotEqual(code, 0)

    def test_raw_size_cap(self):
        self.capture(1, width=1400, height=1000, noise=True)     # more than 4 MiB before stripping
        code, out, _ = self.imp("01")
        self.assertEqual(code, 1)
        self.assertIn("larger than", out)
        self.assertEqual(os.path.exists(os.path.join(self.root, "gimp", "meta.json")), False)

    def test_small_image_is_a_problem_not_an_import(self):
        self.capture(1, width=800, height=250)
        code, out, _ = self.imp("01")
        self.assertEqual(code, 1)
        self.assertIn("problem", out)

    def test_other_png_formats_are_refused(self):
        data = make_png()
        rgba = data.replace(b"\x08\x02\x00\x00\x00", b"\x08\x06\x00\x00\x00", 1)  # header says RGBA: refused up front
        self.capture(1, data=rgba)
        code, out, _ = self.imp("01")
        self.assertEqual(code, 1)

    def test_not_a_png(self):
        self.capture(1, data=b"GIF89a" + b"\0" * 100)
        code, out, _ = self.imp("01")
        self.assertEqual(code, 1)

    def test_missing_capture(self):
        self.capture(1)
        code, out, _ = self.imp("01,02")
        self.assertEqual(code, 1)
        self.assertIn("capture 02: not found", out)
        self.assertIn("imported 1", out)

    def test_two_files_with_one_number(self):
        self.capture(1, "window", "zh_CN")
        self.capture(1, "screen", "zh_CN", data=make_png(seed=99))
        code, out, _ = self.imp("01")
        self.assertEqual(code, 1)
        self.assertIn("more than one file", out)

    def test_dedup_and_second_run(self):
        self.capture(1)
        self.imp("01")
        code, out, _ = self.imp("01")
        self.assertEqual(code, 0)
        self.assertIn("imported 0, skipped 1", out)
        self.assertEqual(len(self.images()), 1)

    def test_per_language_cap(self):
        for n in range(1, 9):
            self.capture(n, lang="zh_CN")
        code, out, _ = self.imp("01,02,03,04,05,06,07,08")
        self.assertEqual(code, 0)
        self.assertIn("imported 6, skipped 2", out)
        self.assertEqual(out.count("already has 6 images"), 2)
        self.assertEqual(len([i for i in self.images() if i["lang"] == "zh_CN"]), 6)

    def test_rejected_images_are_not_imported_again(self):
        self.capture(1)
        self.imp("01")
        self.review("01-session-01.png", "rejected")
        code, out, _ = self.imp("01")
        self.assertIn("skipped 1", out)


class TestAppFilter(SessionCase):
    def test_only_captures_of_the_app_are_imported(self):
        self.capture(1, app="gimp")
        self.capture(2, app="chrome", lang="en_US")
        self.capture(3, app="gimp", lang="en_US")
        code, out, _ = self.imp("01,02,03")
        self.assertEqual(code, 1)
        self.assertIn("imported 2, skipped 0, problems 1", out)
        self.assertIn("problem capture 02: not recorded for gimp", out)
        self.assertEqual(sorted(i["scene"] for i in self.images()), ["session-01", "session-03"])

    def test_the_other_app_can_import_its_own(self):
        self.capture(1, app="gimp")
        self.capture(2, app="chrome")
        code, out, _ = self.imp("02", app="chrome")
        self.assertEqual(code, 0, out)

    def test_app_names_must_match_exactly(self):
        self.capture(1, app="gimp-extra")
        self.capture(2, app="GIMP")
        code, out, _ = self.imp("01,02")
        self.assertEqual(code, 1)
        self.assertIn("imported 0, skipped 0, problems 2", out)

    def test_capture_missing_from_the_index(self):
        self.capture(1, index=False)
        self.capture(2)
        code, out, _ = self.imp("01,02")
        self.assertEqual(code, 1)
        self.assertIn("capture 01: not recorded for gimp", out)
        self.assertIn("imported 1", out)

    def test_index_must_name_the_same_file(self):
        self.capture(1, kind="window")
        doc = {"items": [{"n": 1, "file": "01-screen-zh_CN.png", "app": "gimp"}]}
        H.write(os.path.join(self.captures, SID, "captures.json"), json.dumps(doc), "w")
        code, out, _ = self.imp("01")
        self.assertEqual(code, 1)
        self.assertIn("not recorded for gimp", out)

    def test_missing_corrupt_or_odd_index_imports_nothing(self):
        self.capture(1, index=False)
        code, out, _ = self.imp("01")
        self.assertEqual(code, 1)
        self.assertIn("captures.json", out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "gimp", "meta.json")))
        for text in ("{not json", "[]", '{"items": 3}', '{"items": [1, "x", null, {"n": true, "file": 1, "app": 2}]}'):
            H.write(os.path.join(self.captures, SID, "captures.json"), text, "w")
            code, out, _ = self.imp("01")
            self.assertEqual(code, 1, text)
            self.assertFalse(os.path.exists(os.path.join(self.root, "gimp", "meta.json")))

    def test_index_symlink_and_size_cap(self):
        self.capture(1)
        path = os.path.join(self.captures, SID, "captures.json")
        os.unlink(path)
        secret = os.path.join(os.path.dirname(self.captures), "secret.json")
        H.write(secret, json.dumps({"items": [{"n": 1, "file": "01-window-zh_CN.png", "app": "gimp"}]}), "w")
        os.symlink(secret, path)
        code, out, _ = self.imp("01")
        self.assertEqual(code, 1)
        self.assertIn("symlink", out)
        os.unlink(path)
        H.write(path, " " * ((1 << 20) + 1), "w")
        code, out, _ = self.imp("01")
        self.assertEqual(code, 1)
        self.assertIn("larger than", out)


class TestHostileInput(SessionCase):
    def test_bad_arguments(self):
        self.capture(1)
        for pick in ("1", "001", "00", "100", "01,01", "a1", "01,", ",01", "", "01 02", "01;02", "../1", "-1",
                     ",".join("%02d" % n for n in range(1, 42))):
            code = self.imp(pick)[0]
            self.assertEqual(code, 2, pick)
        for sid in ("short", SID.upper(), SID + "0", "../" + SID[3:], SID[:-1] + "/", "..", "", "0123456789abcde-"):
            self.assertEqual(self.imp("01", sid=sid)[0], 2, sid)
        self.assertEqual(self.imp("01", reviewer="a b")[0], 2)
        self.assertEqual(self.imp("01", reviewer="")[0], 2)
        self.assertEqual(self.imp("01", app="nope")[0], 2)
        self.assertEqual(self.imp("01", app="../gimp")[0], 2)

    def test_forty_picks_are_allowed(self):
        for n in range(1, 41):
            self.capture(n)
        picks = ",".join("%02d" % n for n in range(1, 41))
        code, out, _ = self.imp(picks)
        self.assertEqual(code, 0)
        self.assertIn("imported 6, skipped 34", out)

    def test_unknown_session(self):
        code, out, err = self.imp("01")
        self.assertEqual(code, 1)
        self.assertIn("error", err)

    def test_session_directory_symlink(self):
        outside = os.path.join(os.path.dirname(self.captures), "outside")
        os.makedirs(outside)
        H.write(os.path.join(outside, "01-window-zh_CN.png"), make_png())
        os.symlink(outside, os.path.join(self.captures, SID))
        code, out, err = self.imp("01")
        self.assertEqual(code, 1)
        self.assertIn("error", err)
        self.assertEqual(os.listdir(self.root), [])

    def test_capture_file_symlink(self):
        secret = os.path.join(os.path.dirname(self.captures), "secret.png")
        H.write(secret, make_png())
        os.makedirs(os.path.join(self.captures, SID))
        os.symlink(secret, os.path.join(self.captures, SID, "01-window-zh_CN.png"))
        self.write_index("01-window-zh_CN.png")
        code, out, _ = self.imp("01")
        self.assertEqual(code, 1)
        self.assertIn("symlink", out)
        self.assertEqual(self.meta_exists(), False)

    def meta_exists(self):
        return os.path.exists(os.path.join(self.root, "gimp", "meta.json"))

    def test_directory_and_fifo_with_a_capture_name(self):
        base = os.path.join(self.captures, SID)
        os.makedirs(os.path.join(base, "01-window-zh_CN.png"))
        os.mkfifo(os.path.join(base, "02-window-zh_CN.png"))
        self.write_index("01-window-zh_CN.png", "02-window-zh_CN.png")
        code, out, _ = self.imp("01,02")
        self.assertEqual(code, 1)
        self.assertIn("problems 2", out)
        self.assertFalse(self.meta_exists())

    def test_names_that_are_not_exact_are_never_read(self):
        for name in ("01-window-fr_FR.png", "1-window-zh_CN.png", "01-window-zh_CN.png.bak", "01-full-zh_CN.png",
                     "01-window-zh_CN.PNG", "x01-window-zh_CN.png", "01-window-zh_CN.png\n"):
            H.write(os.path.join(self.captures, SID, name), make_png())
        code, out, _ = self.imp("01")
        self.assertEqual(code, 1)
        self.assertIn("capture 01: not found", out)
        self.assertFalse(self.meta_exists())

    def test_language_comes_from_the_name_only(self):
        self.capture(1, lang="en_US")
        self.imp("01")
        self.assertEqual([i["lang"] for i in self.images()], ["en_US"])

    def test_corrupt_png(self):
        data = bytearray(make_png())
        data[-20] ^= 0xFF
        self.capture(1, data=bytes(data))
        code, out, _ = self.imp("01")
        self.assertEqual(code, 1)
        self.assertFalse(self.meta_exists())

    def test_oversized_dimensions(self):
        self.capture(1, width=4097, height=300)
        code, out, _ = self.imp("01")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
