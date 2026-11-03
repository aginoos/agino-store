# SPDX-License-Identifier: GPL-3.0-or-later
import fcntl
import hashlib
import json
import os
import stat
import unittest
from unittest import mock

from agino_store_server import cli, shots_store

from . import helpers as H
from .shots_helpers import ShotsCase, chunk_kinds, make_png


class TestIngest(ShotsCase):
    def test_happy_path(self):
        data = make_png(extra=[(b"tEXt", b"Comment\x00private"), (b"eXIf", b"gps")])
        code, out, err = self.ingest("upload-0001.png", data)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("stored zh_CN/01-main.png", out)
        self.assertFalse(os.path.exists(os.path.join(self.staging, "upload-0001.png")))
        stored_path = os.path.join(self.root, "gimp", "zh_CN", "01-main.png")
        with open(stored_path, "rb") as f:
            stored = f.read()
        self.assertEqual(chunk_kinds(stored), ["IHDR", "IDAT", "IEND"])
        self.assertEqual(stat.S_IMODE(os.stat(stored_path).st_mode), 0o644)
        image = self.image("zh_CN", "01-main.png")
        self.assertEqual(image, {
            "lang": "zh_CN", "file": "01-main.png", "scene": "main", "sha256": hashlib.sha256(stored).hexdigest(),
            "size": len(stored), "width": 900, "height": 320, "source": "upload", "status": "draft",
            "reviewer": None, "reviewed": None, "note": "", "origin": ""})

    def test_numbers_and_approved_reviewer(self):
        self.ingest("upload-0001.png", make_png(seed=1))
        code, out, _ = self.ingest("upload-0002.png", make_png(seed=2), "zh_CN", "files", "approved", "manual", "bob")
        self.assertEqual(code, 0, out)
        second = self.image("zh_CN", "02-files.png")
        self.assertEqual((second["source"], second["status"], second["reviewer"]), ("manual", "approved", "bob"))
        self.assertEqual(second["reviewed"], "2026-11-02T12:00:00Z")
        self.ingest("upload-0003.png", make_png(seed=3), "en_US", "main")
        self.assertEqual(self.image("en_US", "01-main.png")["status"], "draft")

    def test_staging_file_rules(self):
        outside = os.path.join(os.path.dirname(self.staging), "outside.png")
        H.write(outside, make_png())
        os.symlink(outside, os.path.join(self.staging, "linked-0001.png"))
        code, _, err = self.ingest("linked-0001.png")
        self.assertEqual(code, 1)
        self.assertIn("symlink", err)
        self.assertTrue(os.path.exists(outside))
        os.mkdir(os.path.join(self.staging, "adir-00001.png"))
        self.assertEqual(self.ingest("adir-00001.png")[0], 1)
        self.assertEqual(self.ingest("missing-0001.png")[0], 1)
        for bad in ("../outside.png", "sub/upload-0001.png", "short.png", "upload-0001.PNG", "upload 0001.png",
                    "x" * 70 + ".png", "upload-0001.png/", ".hidden-0001.png"):
            with self.subTest(bad):
                self.assertEqual(self.ingest(bad)[0], 2)
        self.assertFalse(os.path.exists(os.path.join(self.root, "gimp", "meta.json")))

    def test_staged_file_too_large_is_not_read(self):
        H.write(os.path.join(self.staging, "huge-00001.png"), b"\0" * ((4 << 20) + 1))
        code, _, err = self.ingest("huge-00001.png")
        self.assertEqual(code, 1)
        self.assertIn("larger", err)

    def test_image_rules(self):
        cases = {
            "garbage": b"not a png at all",
            "too small": make_png(width=800, height=400),
            "too short": make_png(width=1000, height=200),
            "too wide": make_png(width=4100, height=400),
            "too tall": make_png(width=900, height=4100),
            "too big": make_png(width=1300, height=600, noise=True),
        }
        for name, data in cases.items():
            with self.subTest(name):
                code, _, err = self.ingest("upload-0001.png", data)
                self.assertEqual(code, 1)
                self.assertTrue(err)
        self.assertFalse(os.path.exists(os.path.join(self.root, "gimp", "zh_CN")))
        self.assertTrue(os.path.exists(os.path.join(self.staging, "upload-0001.png")))

    def test_min_width_is_configurable(self):
        self.set_conf("\n[shots]\nmin_width = 1200\n")
        self.assertEqual(self.ingest("upload-0001.png", make_png(width=1000))[0], 1)
        self.set_conf("\n[shots]\nmin_width = 600\n")
        self.assertEqual(self.ingest("upload-0001.png", make_png(width=700))[0], 0)

    def test_duplicate_and_limit(self):
        self.assertEqual(self.ingest("upload-0001.png", make_png(seed=1))[0], 0)
        code, _, err = self.ingest("upload-0002.png", make_png(seed=1), scene="other")
        self.assertEqual(code, 1)
        self.assertIn("identical to zh_CN/01-main.png", err)
        for seed in range(2, 7):
            self.assertEqual(self.ingest("upload-%04d.png" % seed, make_png(seed=seed), scene="s%d" % seed)[0], 0)
        code, _, err = self.ingest("upload-0009.png", make_png(seed=9), scene="seven")
        self.assertEqual(code, 1)
        self.assertIn("at most 6", err)
        self.assertEqual(len(self.images()), 6)
        self.assertEqual(self.ingest("upload-0010.png", make_png(seed=10), "en_US", "seven")[0], 0)   # per language
        self.review("06-s6.png", "rejected")
        self.assertEqual(self.ingest("upload-0011.png", make_png(seed=11), scene="seven")[0], 0)   # rejected is free
        self.assertEqual(self.image("zh_CN", "07-seven.png")["status"], "draft")

    def test_limit_from_config(self):
        self.set_conf("\n[shots]\nmax_per_lang = 1\n")
        self.assertEqual(self.ingest("upload-0001.png", make_png(seed=1))[0], 0)
        self.assertEqual(self.ingest("upload-0002.png", make_png(seed=2), scene="b")[0], 1)

    def test_replace_same_scene_moves_old_to_prev(self):
        old = make_png(seed=1)
        self.ingest("upload-0001.png", old)
        old_stored = self._stored("zh_CN", "01-main.png")
        code, out, _ = self.ingest("upload-0002.png", make_png(seed=2), "zh_CN", "main", "approved", "manual", "bob",
                    replace="01-main.png")
        self.assertEqual(code, 0, out)
        images = self.images()
        self.assertEqual([i["file"] for i in images], ["01-main.png"])
        self.assertEqual((images[0]["status"], images[0]["reviewer"]), ("approved", "bob"))
        self.assertNotEqual(self._stored("zh_CN", "01-main.png"), old_stored)
        prev = self._prev_files()
        self.assertEqual([tuple(p.split(os.sep)[-2:]) for p in prev], [("zh_CN", "01-main.png")])
        with open(prev[0], "rb") as f:
            self.assertEqual(f.read(), old_stored)
        self.assertRegex(prev[0].split(os.sep)[-3], r"\A20261102T120000Z")

    def test_replace_with_other_scene_renames(self):
        self.ingest("upload-0001.png", make_png(seed=1))
        code, _, _ = self.ingest("upload-0002.png", make_png(seed=2), "zh_CN", "files", "draft", "upload", "alice",
                    replace="01-main.png")
        self.assertEqual(code, 0)
        self.assertEqual([i["file"] for i in self.images()], ["01-files.png"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "gimp", "zh_CN", "01-main.png")))
        self.assertEqual(len(self._prev_files()), 1)

    def test_replace_unknown_and_at_limit(self):
        self.assertEqual(self.ingest("upload-0001.png", make_png(seed=1), "zh_CN", "main", "draft", "upload", "alice",
                    replace="05-x.png")[0], 1)
        self.set_conf("\n[shots]\nmax_per_lang = 1\n")
        self.ingest("upload-0002.png", make_png(seed=2))
        code, _, _ = self.ingest("upload-0003.png", make_png(seed=3), "zh_CN", "main", "draft", "upload", "alice",
                    replace="01-main.png")
        self.assertEqual(code, 0)      # a replacement does not count twice

    def _stored(self, lang, file):
        with open(os.path.join(self.root, "gimp", lang, file), "rb") as f:
            return f.read()

    def _prev_files(self):
        return [os.path.join(base, name) for base, _d, files in os.walk(os.path.join(self.root, "gimp", ".prev"))
                for name in files]


class TestReviewRemove(ShotsCase):
    def test_review_cycle(self):
        image = self.add_image(status="draft")
        code, out, err = self.review(image["file"], "approved", "zh_CN", "bob", "gimp", "--note", "looks good")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.strip(), "approved zh_CN/01-main.png")
        approved = self.image("zh_CN", "01-main.png")
        self.assertEqual((approved["status"], approved["reviewer"], approved["note"]), ("approved", "bob", "looks good"))
        self.assertEqual(approved["reviewed"], "2026-11-02T12:00:00Z")
        self.review("01-main.png", "rejected", reviewer="carol", app="gimp")
        rejected = self.image("zh_CN", "01-main.png")
        self.assertEqual((rejected["status"], rejected["reviewer"], rejected["note"]), ("rejected", "carol", "looks good"))
        self.assertTrue(os.path.exists(os.path.join(self.root, "gimp", "zh_CN", "01-main.png")))
        self.review("01-main.png", "draft")
        draft = self.image("zh_CN", "01-main.png")
        self.assertEqual((draft["status"], draft["reviewer"], draft["reviewed"]), ("draft", None, None))

    def test_approve_checks_the_file_again(self):
        self.add_image(status="draft")
        path = os.path.join(self.root, "gimp", "zh_CN", "01-main.png")
        with open(path, "ab") as f:
            f.write(b"x")
        code, _, err = self.review("01-main.png", "approved")
        self.assertEqual(code, 1)
        self.assertIn("sha256", err)
        self.assertEqual(self.image("zh_CN", "01-main.png")["status"], "draft")
        os.unlink(path)
        self.assertEqual(self.review("01-main.png", "approved")[0], 1)

    def test_approve_rejects_unclean_or_small_file_even_with_matching_hash(self):
        self.add_image(status="draft")
        path = os.path.join(self.root, "gimp", "zh_CN", "01-main.png")
        dirty = make_png(extra=[(b"tEXt", b"x\x00y")])
        with open(path, "wb") as f:
            f.write(dirty)
        meta = self.meta()
        meta["images"][0]["sha256"] = hashlib.sha256(dirty).hexdigest()
        H.write(os.path.join(self.root, "gimp", "meta.json"), json.dumps(meta), "w")
        self.assertEqual(self.review("01-main.png", "approved")[0], 1)
        self.set_conf("\n[shots]\nmin_width = 1000\n")
        self.assertEqual(self.review("01-main.png", "rejected")[0], 0)

    def test_limit_applies_when_leaving_rejected(self):
        self.set_conf("\n[shots]\nmax_per_lang = 2\n")
        first = self.add_image(seed=1, scene="a", status="draft")
        self.review(first["file"], "rejected")
        self.add_image(seed=2, scene="b", status="draft")
        self.add_image(seed=3, scene="c", status="draft")
        code, _, err = self.review(first["file"], "approved")
        self.assertEqual(code, 1)
        self.assertIn("at most 2", err)
        self.assertEqual(self.review(first["file"], "rejected")[0], 0)

    def test_unknown_image(self):
        self.add_image()
        self.assertEqual(self.review("09-nope.png", "approved")[0], 1)
        self.assertEqual(self.review("01-main.png", "approved", "en_US")[0], 1)

    def test_remove_moves_to_prev(self):
        self.add_image(seed=1, scene="a")
        self.add_image(seed=2, scene="b")
        with open(os.path.join(self.root, "gimp", "zh_CN", "01-a.png"), "rb") as f:
            kept = f.read()
        code, out, _ = self.run_cli("shots-remove", "gimp", "--lang", "zh_CN", "--file", "01-a.png", "--reviewer", "bob")
        self.assertEqual(code, 0, out)
        self.assertEqual([i["file"] for i in self.images()], ["02-b.png"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "gimp", "zh_CN", "01-a.png")))
        prev, = [os.path.join(b, n) for b, _d, fs in os.walk(os.path.join(self.root, "gimp", ".prev")) for n in fs]
        self.assertTrue(prev.endswith(os.path.join("zh_CN", "01-a.png")))
        with open(prev, "rb") as f:
            self.assertEqual(f.read(), kept)
        self.assertEqual(self.run_cli("shots-remove", "gimp", "--lang", "zh_CN", "--file", "01-a.png",
                                      "--reviewer", "bob")[0], 1)

    def test_two_removals_in_one_second_keep_both(self):
        self.add_image(seed=1, scene="a", status="draft")
        self.ingest("again-0001.png", make_png(seed=2), scene="a")
        self.run_cli("shots-remove", "gimp", "--lang", "zh_CN", "--file", "01-a.png", "--reviewer", "bob")
        self.ingest("again-0002.png", make_png(seed=3), scene="a")     # becomes 03-a.png (never reuses a number)
        self.run_cli("shots-remove", "gimp", "--lang", "zh_CN", "--file", "03-a.png", "--reviewer", "bob")
        names = sorted(n for _b, _d, fs in os.walk(os.path.join(self.root, "gimp", ".prev")) for n in fs)
        self.assertEqual(names, ["01-a.png", "03-a.png"])


class TestArgumentsAndTree(ShotsCase):
    def snapshot(self):
        return sorted(os.path.join(b, n) for b, ds, fs in os.walk(self.root) for n in ds + fs)

    def test_every_argument_is_validated(self):
        ok = dict(app="gimp", lang="zh_CN", scene="main", file="upload-0001.png", source="upload",
                  status="draft", reviewer="alice")
        self.stage("upload-0001.png", make_png())

        def ingest(**over):
            a = dict(ok, **over)
            argv = ["shots-ingest", a["app"], "--lang", a["lang"], "--scene", a["scene"], "--file", a["file"],
                    "--source", a["source"], "--status", a["status"], "--reviewer", a["reviewer"]]
            if "replace" in a:
                argv += ["--replace", a["replace"]]
            if "note" in a:
                argv += ["--note", a["note"]]
            return self.run_cli(*argv)

        bad_values = {
            "app": ["", "Gimp", "-gimp", "../gimp", "gimp/x", "nosuchapp", "a" * 65, "gimp\n"],
            "lang": ["fr", "zh_cn", "", "zh_CN/"],
            "scene": ["", "Main", "-x", "a/b", "a" * 42, "a_b", "ma in"],
            "source": ["sandbox", "vendor", "", "robot"],
            "status": ["rejected", "", "done"],
            "reviewer": ["", "a b", "x" * 33, "a/b", "bob;rm", "ünï"],
            "replace": ["", "1-main.png", "01-main.jpg", "../01-main.png", "01-Main.png"],
        }
        before = self.snapshot()
        for field, values in bad_values.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assertEqual(ingest(**{field: value})[0], 2)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(os.path.exists(os.path.join(self.staging, "upload-0001.png")))
        self.add_image()
        bad_reviews = [dict(lang="fr"), dict(file="1-main.png"), dict(file="../01-main.png"), dict(status="done"),
                       dict(reviewer="a b"), dict(app="nosuchapp"), dict(app="Gimp"),
                       dict(note="x" * 201), dict(note="a\x07b"), dict(note="a\nb"), dict(note="-rf"),
                       dict(note="bad \udcff utf8")]
        for case in bad_reviews:
            with self.subTest(**case):
                a = dict(app="gimp", lang="zh_CN", file="01-main.png", status="approved", reviewer="bob")
                a.update({k: v for k, v in case.items() if k != "note"})
                argv = ["shots-review", a["app"], "--lang", a["lang"], "--file", a["file"], "--status", a["status"],
                        "--reviewer", a["reviewer"]]
                if "note" in case:
                    argv.append("--note=" + case["note"])
                self.assertEqual(self.run_cli(*argv)[0], 2)
        for case in (dict(lang="fr"), dict(file="1-main.png"), dict(file="../01-main.png"), dict(reviewer="a b"),
                     dict(app="nosuchapp"), dict(app="Gimp")):
            with self.subTest(remove=case):
                a = dict(dict(app="gimp", lang="zh_CN", file="01-main.png", reviewer="bob"), **case)
                self.assertEqual(self.run_cli("shots-remove", a["app"], "--lang", a["lang"], "--file", a["file"],
                                              "--reviewer", a["reviewer"])[0], 2)
        self.assertEqual(self.image("zh_CN", "01-main.png")["status"], "approved")
        self.assertEqual(self.run_cli("shots-review", "gimp")[0], 2)         # missing options (argparse)
        okay = self.review("01-main.png", "draft", "zh_CN", "bob", "gimp", "--note=ünï ok 中文")
        self.assertEqual(okay[0], 0)
        self.assertEqual(self.image("zh_CN", "01-main.png")["note"], "ünï ok 中文")
        self.assertIn("\\u", open(os.path.join(self.root, "gimp", "meta.json"), encoding="ascii").read())

    def test_meta_schema_and_atomic_write(self):
        self.add_image(status="approved")
        meta = self.meta()
        self.assertEqual((meta["schema"], meta["app"]), (1, "gimp"))
        self.assertRegex(meta["updated"], r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
        path = os.path.join(self.root, "gimp", "meta.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
        with open(path, "rb") as f:
            f.read().decode("ascii")
        self.assertEqual([n for n in os.listdir(os.path.join(self.root, "gimp")) if n.startswith(".tmp")], [])
        before = open(path, "rb").read()
        real = os.replace

        def failing(src, dst, *a, **k):
            if str(dst).endswith("meta.json"):
                raise OSError(5, "simulated disk error")
            return real(src, dst, *a, **k)
        with mock.patch.object(shots_store.os, "replace", side_effect=failing):
            code, _, err = self.review("01-main.png", "rejected")
        self.assertEqual(code, 1)
        self.assertEqual(open(path, "rb").read(), before)
        self.assertEqual([n for n in os.listdir(os.path.join(self.root, "gimp")) if n.startswith(".tmp")], [])

    def test_damaged_meta_is_not_overwritten(self):
        self.add_image()
        path = os.path.join(self.root, "gimp", "meta.json")
        for bad in ("{not json", json.dumps({"schema": 2, "app": "gimp", "images": []}),
                    json.dumps({"schema": 1, "app": "other", "images": []}),
                    json.dumps({"schema": 1, "app": "gimp", "images": [{"lang": "xx"}]})):
            with self.subTest(bad[:20]):
                H.write(path, bad, "w")
                self.assertEqual(self.ingest("upload-0009.png", make_png(seed=9))[0], 1)
                self.assertEqual(self.review("01-main.png", "rejected")[0], 1)
                self.assertEqual(open(path, encoding="utf-8").read(), bad)

    def test_missing_directories(self):
        self.stage("upload-0001.png", make_png())
        os.rename(self.staging, self.staging + ".gone")
        code, _, err = self.ingest()
        self.assertEqual(code, 1)
        self.assertIn("staging directory", err)
        os.rename(self.staging + ".gone", self.staging)
        os.rename(self.root, self.root + ".gone")
        for argv in (["shots-ingest", "gimp", "--lang", "zh_CN", "--scene", "main", "--file", "upload-0001.png",
                      "--source", "upload", "--status", "draft", "--reviewer", "alice"],
                     ["shots-review", "gimp", "--lang", "zh_CN", "--file", "01-main.png", "--status", "draft",
                      "--reviewer", "a"],
                     ["shots-remove", "gimp", "--lang", "zh_CN", "--file", "01-main.png", "--reviewer", "a"],
                     ["shots-import", "gimp"]):
            with self.subTest(argv[0]):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(code, 1)
                self.assertTrue(err.startswith("error:"))
        self.assertFalse(os.path.exists(self.root))     # never created behind the operator's back
        self.assertTrue(os.path.exists(os.path.join(self.staging, "upload-0001.png")))

    def test_per_app_lock_busy_exits_3_and_others_are_free(self):
        self.add_image()
        fd = os.open(os.path.join(self.root, "gimp", ".lock"), os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertEqual(self.review("01-main.png", "rejected")[0], cli.EXIT_LOCKED)
        self.assertEqual(self.ingest("upload-0001.png", make_png(seed=5), app="chrome")[0], 0)

    def test_global_state_lock_is_not_taken(self):
        with self.st.lock():
            self.assertEqual(self.ingest("upload-0001.png", make_png())[0], 0)
            self.assertEqual(self.review("01-main.png", "approved")[0], 0)
            self.assertEqual(self.run_cli("shots-remove", "gimp", "--lang", "zh_CN", "--file", "01-main.png",
                                          "--reviewer", "a")[0], 0)
        with mock.patch.object(cli.State, "lock", side_effect=AssertionError("global lock taken")):
            self.assertEqual(self.ingest("upload-0002.png", make_png(seed=2))[0], 0)

    def test_candidates_and_approved_are_untouched(self):
        self.add("2.0-1")
        self.st.set_approved("chrome", {"version": "1.0-1", "approved_at": "t", "by": "b"}, {"a": 1})
        def tree():
            return sorted((os.path.join(b, n), os.stat(os.path.join(b, n)).st_mtime_ns)
                          for b, _d, fs in os.walk(self.cfg.state) for n in fs if n != ".lock")
        before = tree()
        self.add_image()
        self.review("01-main.png", "rejected")
        self.assertEqual(tree(), before)


if __name__ == "__main__":
    unittest.main()
