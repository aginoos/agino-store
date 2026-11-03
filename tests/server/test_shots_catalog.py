# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import os
import unittest

from agino_store_server import catalog, shots_store as S

from . import helpers as H
from .shots_helpers import make_png, slurp
from .test_catalog import CatalogCase
from .test_status import StatusCase


class DirectoryCase(CatalogCase):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.base, "screenshots")
        os.makedirs(self.root)
        self.cfg = H.make_config(self.base, self.key, keep_catalogs=self.keep, screenshots=self.root)
        self.publish_all()

    def put(self, lang="zh_CN", scene="main", seed=1, status="approved", app="gimp", **png):
        data = make_png(seed=seed, **png)
        payload, width, height = S.check_png(self.cfg, data)
        with S.locked(self.cfg, app) as album:
            record = album.store(lang, scene, payload, width, height, source="manual", status=status,
                                 reviewer="bob", origin="")
            album.save()
        return record

    def approve(self, record, status="approved"):
        with S.locked(self.cfg, "gimp") as album:
            album.set_status(record["lang"], record["file"], status, "bob")
            album.save()

    def gimp_shots(self):
        return self.read("apps", "gimp.json")["screenshots"]


class TestCatalogFromDirectory(DirectoryCase):
    def test_only_approved_ordered_by_lang_and_file(self):
        two = self.put("zh_CN", "files", seed=2)
        one = self.put("zh_CN", "main", seed=1)       # 02-main: later number, later in the list
        draft = self.put("zh_CN", "draft", seed=3, status="draft")
        rejected = self.put("en_US", "gone", seed=4)
        self.approve(rejected, "rejected")
        en = self.put("en_US", "main", seed=5)
        summary = self.build()
        self.assertEqual(summary["warnings"], [])
        shots = self.gimp_shots()
        self.assertEqual(set(shots), {"zh_CN", "en_US"})
        self.assertEqual([s["image"] for s in shots["zh_CN"]], ["assets/%s.png" % two["sha256"],
                                                                "assets/%s.png" % one["sha256"]])
        self.assertEqual([s["image"] for s in shots["en_US"]], ["assets/%s.png" % en["sha256"]])
        first = shots["zh_CN"][0]
        self.assertEqual((first["width"], first["height"], first["thumb"]), (900, 320, first["image"]))
        published = os.path.join(self.current(), first["image"])
        self.assertEqual(hashlib.sha256(slurp(published)).hexdigest(), two["sha256"])
        for hidden in (draft, rejected):
            self.assertFalse(os.path.exists(os.path.join(self.current(), "assets", hidden["sha256"] + ".png")))
        catalog.verify_tree(self.current())
        files = {f["path"] for f in self.read("manifest.json")["files"]}
        self.assertIn(first["image"], files)                    # covered by the signature
        self.assertTrue(H.verify_signature(self.current(), self.pub))

    def test_directory_wins_but_app_yaml_still_works(self):
        self.build()
        self.assertEqual(self.gimp_shots()["zh_CN"][0]["width"], 320)      # app.yaml only
        record = self.put("zh_CN", "main", seed=1)
        self.build()
        shots = self.gimp_shots()
        self.assertEqual([s["width"] for s in shots["zh_CN"]], [900])
        self.assertEqual(shots["en_US"], [])                   # app.yaml's English image is not mixed in
        self.approve(record, "draft")
        self.build()
        self.assertEqual(self.gimp_shots()["zh_CN"][0]["width"], 320)      # back to app.yaml

    def test_empty_on_both_sides_is_empty(self):
        H.edit_yaml(os.path.join(self.cfg.content, "apps", "gimp", "app.yaml"), lambda d: dict(d, screenshots={}))
        self.build()
        self.assertEqual(self.gimp_shots(), {})

    def test_approval_changes_inputs_and_triggers_rebuild(self):
        self.assertTrue(self.build(if_changed=True)["changed"])
        self.assertFalse(self.build(if_changed=True)["changed"])
        record = self.put(status="draft")
        self.assertFalse(self.build(if_changed=True)["changed"])           # a draft changes nothing
        before = self.st.catalog_state()["inputs_sha256"]
        self.approve(record)
        summary = self.build(if_changed=True)
        self.assertTrue(summary["changed"])
        self.assertNotEqual(self.st.catalog_state()["inputs_sha256"], before)
        self.assertFalse(self.build(if_changed=True)["changed"])
        self.approve(record, "rejected")
        self.assertTrue(self.build(if_changed=True)["changed"])            # withdrawing counts too
        self.assertEqual(self.gimp_shots()["zh_CN"][0]["width"], 320)

    def test_inputs_unchanged_when_there_are_no_directory_screenshots(self):
        facts, _ = catalog.resolve(self.cfg, self.st, self.load())
        plain = catalog.inputs_hash(self.cfg, self.load(), facts)
        self.assertEqual(catalog.inputs_hash(self.cfg, self.load(), facts, {}), plain)
        self.assertNotEqual(catalog.inputs_hash(self.cfg, self.load(), facts, {"gimp": [
            {"lang": "zh_CN", "file": "01-main.png", "sha256": "a" * 64}]}), plain)

    def test_missing_directory_builds_as_before(self):
        os.rename(self.root, self.root + ".gone")
        summary = self.build()
        self.assertEqual(summary["warnings"], [])
        self.assertEqual(self.gimp_shots()["zh_CN"][0]["width"], 320)

    def test_tampered_file_is_left_out_with_a_warning(self):
        keep = self.put("zh_CN", "main", seed=1)
        broken = self.put("zh_CN", "files", seed=2)
        with open(os.path.join(self.root, "gimp", "zh_CN", broken["file"]), "ab") as f:
            f.write(b"x")
        os.unlink(os.path.join(self.root, "gimp", "zh_CN", keep["file"]))
        summary = self.build()
        self.assertEqual(len(summary["warnings"]), 2)
        self.assertEqual(self.gimp_shots()["zh_CN"][0]["width"], 320)      # nothing usable: app.yaml again
        self.assertEqual([s["id"] for s in summary["skipped"]], [])

    def test_damaged_meta_is_a_warning_not_a_failure(self):
        self.put()
        H.write(os.path.join(self.root, "gimp", "meta.json"), "{broken", "w")
        summary = self.build()
        self.assertTrue(summary["warnings"])
        self.assertEqual(self.gimp_shots()["zh_CN"][0]["width"], 320)

    def test_damaged_meta_warning_text(self):
        self.put()
        H.write(os.path.join(self.root, "gimp", "meta.json"), "{broken", "w")
        summary = self.build()
        self.assertIn("meta.json", summary["warnings"][0])


class TestStatusShots(StatusCase):
    def setUp(self):
        super().setUp()
        self.cfg = H.make_config(os.path.dirname(self.conf), self.key, screenshots=self.shots,
                                 shots_max_per_lang=6)

    def put(self, lang, scene, seed, status, app="gimp"):
        data, width, height = S.check_png(self.cfg, make_png(seed=seed))
        os.makedirs(self.shots, exist_ok=True)
        with S.locked(self.cfg, app) as album:
            album.store(lang, scene, data, width, height, source="sandbox", status=status, reviewer="bob",
                        origin="job1", note="nöte")
            album.save()

    def test_apps_summary_and_shots_section(self):
        self.put("zh_CN", "main", 1, "approved")
        self.put("en_US", "main", 2, "draft")
        self.put("en_US", "files", 3, "rejected")
        doc = self.doc("apps", "shots")
        apps = {a["id"]: a for a in doc["sections"]["apps"]}
        self.assertEqual(apps["gimp"]["shots"], {"state": "approved", "scenes": 1, "langs": ["zh_CN", "en_US"],
                                                 "counts": {"draft": 1, "approved": 1, "rejected": 1}})
        self.assertEqual(apps["chrome"]["shots"], {"state": "none", "scenes": 0, "langs": [],
                                                   "counts": {"draft": 0, "approved": 0, "rejected": 0}})
        section = {e["id"]: e for e in doc["sections"]["shots"]}
        self.assertEqual(set(section), {"chrome", "gimp"})
        self.assertEqual(section["chrome"], {"id": "chrome", "images": [],
                                             "counts": {"draft": 0, "approved": 0, "rejected": 0}})
        images = section["gimp"]["images"]
        self.assertEqual([(i["lang"], i["file"], i["status"]) for i in images],
                         [("en_US", "01-main.png", "draft"), ("en_US", "02-files.png", "rejected"),
                          ("zh_CN", "01-main.png", "approved")])
        self.assertEqual(set(images[0]), set(S.FIELDS))
        self.assertEqual((images[2]["reviewer"], images[2]["origin"], images[2]["width"], images[2]["note"]),
                         ("bob", "job1", 900, "nöte"))
        self.assertEqual(doc["errors"], [])

    def test_states(self):
        self.put("zh_CN", "main", 1, "draft")
        self.assertEqual(self.app_shots("gimp")["state"], "draft")
        self.put("en_US", "x", 2, "rejected")
        self.assertEqual(self.app_shots("gimp")["langs"], ["zh_CN"])

    def app_shots(self, app):
        return next(a for a in self.doc("apps")["sections"]["apps"] if a["id"] == app)["shots"]

    def test_missing_directory_and_damaged_meta(self):
        doc = self.doc("apps", "shots")
        self.assertEqual(doc["errors"], [])
        self.assertTrue(all(a["shots"]["state"] == "none" for a in doc["sections"]["apps"]))
        H.write(os.path.join(self.shots, "gimp", "meta.json"), "{nope", "w")
        doc = self.doc("apps", "shots")
        self.assertTrue(self.errors(doc, "apps"))
        self.assertTrue(self.errors(doc, "shots"))
        self.assertEqual({a["id"] for a in doc["sections"]["apps"]}, {"chrome", "gimp"})

    def test_section_is_registered_and_read_only(self):
        self.put("zh_CN", "main", 1, "approved")
        code, _, err = self.run_cli("status", "--json", "--section", "nosuch")
        self.assertEqual(code, 2)
        self.assertIn("shots", err)
        before = sorted((os.path.join(b, n), os.lstat(os.path.join(b, n)).st_mtime_ns)
                        for b, ds, fs in os.walk(self.shots) for n in ds + fs)
        self.doc("shots")
        after = sorted((os.path.join(b, n), os.lstat(os.path.join(b, n)).st_mtime_ns)
                       for b, ds, fs in os.walk(self.shots) for n in ds + fs)
        self.assertEqual(before, after)
        with self.st.lock():
            self.assertIn("shots", self.doc("shots")["sections"])


if __name__ == "__main__":
    unittest.main()
