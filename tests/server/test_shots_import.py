# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import json
import os
import unittest

from . import helpers as H
from .shots_helpers import ShotsCase, chunk_kinds, make_png, slurp


class ImportCase(ShotsCase):
    def make_job(self, suffix="0123456789ab", scenes=(("main", 1),), seed=1, app="gimp", mtime=None, **png):
        """A finished sandbox job: <out>/<app>_<sha12>/{shots.json,<lang>/<n>-<scene>.png}."""
        job = "%s_%s" % (app, suffix)
        entries = []
        for scene, n in scenes:
            files = {}
            for lang in ("zh_CN", "en_US"):
                data = make_png(seed=seed + n * 10 + (5 if lang == "en_US" else 0), **png)
                name = "%d-%s.png" % (n, scene)
                H.write(os.path.join(self.out, job, lang, name), data)
                files[lang] = {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "width": 900, "height": 320}
            entries.append({"id": scene, "n": n, "caption": {"zh_CN": "x", "en_US": "y"}, "files": files,
                            "problems": []})
        doc = {"schema": 1, "app": app, "version": "1", "sha256": "a" * 64, "scenes": entries}
        path = os.path.join(self.out, job, "shots.json")
        H.write(path, json.dumps(doc), "w")
        if mtime:
            os.utime(path, (mtime, mtime))
        return job

    def imp(self, app="gimp"):
        return self.run_cli("shots-import", app)


class TestImport(ImportCase):
    def test_imports_drafts(self):
        job = self.make_job(scenes=(("main", 1), ("files", 2)))
        code, out, err = self.imp()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("imported 4, skipped 0, problems 0", out)
        self.assertEqual(sorted((i["lang"], i["file"]) for i in self.images()),
                         [("en_US", "01-main.png"), ("en_US", "02-files.png"),
                          ("zh_CN", "01-main.png"), ("zh_CN", "02-files.png")])
        for image in self.images():
            self.assertEqual((image["status"], image["source"], image["origin"], image["reviewer"], image["reviewed"]),
                             ("draft", "sandbox", job, None, None))
            path = os.path.join(self.root, "gimp", image["lang"], image["file"])
            with open(path, "rb") as f:
                data = f.read()
            self.assertEqual(hashlib.sha256(data).hexdigest(), image["sha256"])

    def test_strips_ancillary_chunks(self):
        self.make_job(extra=[(b"tEXt", b"a\x00b")])
        self.imp()
        with open(os.path.join(self.root, "gimp", "zh_CN", "01-main.png"), "rb") as f:
            self.assertEqual(chunk_kinds(f.read()), ["IHDR", "IDAT", "IEND"])

    def test_newest_job_wins(self):
        self.make_job("aaaaaaaaaaaa", seed=1, mtime=1_700_000_000)
        newer = self.make_job("bbbbbbbbbbbb", seed=2, mtime=1_700_100_000)
        self.make_job("cccccccccccc", seed=3, app="chrome", mtime=1_700_200_000)       # another app
        os.makedirs(os.path.join(self.out, "gimp_dddddddddddd"))                       # no shots.json yet
        os.makedirs(os.path.join(self.out, "gimp_eeeeeeeeeeee.prev"))
        self.imp()
        self.assertEqual({i["origin"] for i in self.images()}, {newer})

    def test_dedup_and_second_run(self):
        self.make_job()
        self.imp()
        before = slurp(os.path.join(self.root, "gimp", "meta.json"))
        code, out, _ = self.imp()
        self.assertEqual(code, 0)
        self.assertIn("imported 0, skipped 2", out)
        self.assertEqual(slurp(os.path.join(self.root, "gimp", "meta.json")), before)

    def test_rejected_is_not_imported_again(self):
        self.make_job()
        self.imp()
        self.review("01-main.png", "rejected")
        self.assertIn("imported 0, skipped 2", self.imp()[1])
        self.assertEqual(len(self.images()), 2)

    def test_never_touches_approved(self):
        self.make_job("aaaaaaaaaaaa", seed=1, mtime=1_700_000_000)
        self.imp()
        self.review("01-main.png", "approved", "zh_CN", "bob")
        approved_path = os.path.join(self.root, "gimp", "zh_CN", "01-main.png")
        approved_bytes = slurp(approved_path)
        approved_meta = self.image("zh_CN", "01-main.png")
        self.make_job("bbbbbbbbbbbb", seed=7, mtime=1_700_100_000)      # new pixels for the same scene
        code, out, _ = self.imp()
        self.assertEqual(code, 0)
        self.assertIn("scene main already has 01-main.png (approved)", out)
        self.assertEqual(slurp(approved_path), approved_bytes)
        self.assertEqual(self.image("zh_CN", "01-main.png"), approved_meta)
        prev = sorted(os.path.join(os.path.basename(b), n) for base, _d, fs in
                      os.walk(os.path.join(self.root, "gimp", ".prev")) for b in [base] for n in fs)
        self.assertEqual(prev, [os.path.join("en_US", "01-main.png")])    # only the English draft was replaced
        self.assertEqual(self.image("en_US", "01-main.png")["origin"], "gimp_bbbbbbbbbbbb")

    def test_manual_images_are_not_replaced(self):
        self.ingest("upload-0001.png", make_png(seed=99), source="manual")
        self.make_job()
        self.assertIn("(draft)", self.imp()[1])
        self.assertEqual(self.image("zh_CN", "01-main.png")["source"], "manual")

    def test_limit_per_language(self):
        scenes = tuple(("scene%d" % n, n) for n in range(1, 9))
        self.make_job(scenes=scenes)
        code, out, _ = self.imp()
        self.assertEqual(code, 0)
        self.assertIn("imported 12, skipped 4, problems 0", out)
        for lang in ("zh_CN", "en_US"):
            self.assertEqual(len([i for i in self.images() if i["lang"] == lang]), 6)

    def test_problems_are_reported_and_the_rest_imported(self):
        self.make_job(scenes=(("main", 1), ("files", 2), ("big", 3)))
        job = os.path.join(self.out, [n for n in os.listdir(self.out)][0])
        H.write(os.path.join(job, "zh_CN", "2-files.png"), b"not a png")                 # sha256 mismatch
        os.unlink(os.path.join(job, "en_US", "3-big.png"))                               # missing
        outside = os.path.join(self.out, "..", "outside.png")
        H.write(outside, make_png(seed=50))
        os.unlink(os.path.join(job, "zh_CN", "3-big.png"))
        os.symlink(os.path.abspath(outside), os.path.join(job, "zh_CN", "3-big.png"))   # symlink
        code, out, _ = self.imp()
        self.assertEqual(code, 1)
        self.assertIn("imported 3, skipped 0, problems 3", out)
        self.assertEqual(len(self.images()), 3)

    def test_small_image_is_a_problem(self):
        self.make_job(width=800)
        code, out, _ = self.imp()
        self.assertEqual(code, 1)
        self.assertIn("problems 2", out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "gimp", "meta.json")))

    def test_bad_shots_json(self):
        job = self.make_job()
        path = os.path.join(self.out, job, "shots.json")
        for text in ("{bad", json.dumps({"schema": 1, "app": "chrome", "scenes": []}),
                     json.dumps({"schema": 2, "app": "gimp", "scenes": []})):
            H.write(path, text, "w")
            self.assertEqual(self.imp()[0], 1)
        doc = {"schema": 1, "app": "gimp", "scenes": [
            {"id": "../x", "n": 1, "files": {}}, {"id": "ok", "n": 100, "files": {}},
            {"id": "main", "n": 1, "files": {"zh_CN": {"name": "../../etc/passwd", "sha256": "0" * 64}}}]}
        H.write(path, json.dumps(doc), "w")
        code, out, _ = self.imp()
        self.assertEqual(code, 1)
        self.assertIn("problems 3", out)

    def test_nothing_to_import(self):
        self.assertEqual(self.imp()[0], 1)
        os.rename(self.out, self.out + ".gone")
        code, _, err = self.imp()
        self.assertEqual(code, 1)
        self.assertIn("shots output directory", err)
        self.assertEqual(self.run_cli("shots-import", "nosuchapp")[0], 2)
        self.assertEqual(self.run_cli("shots-import", "../gimp")[0], 2)


if __name__ == "__main__":
    unittest.main()
