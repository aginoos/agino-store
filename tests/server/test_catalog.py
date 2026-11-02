# SPDX-License-Identifier: GPL-3.0-or-later
import datetime
import json
import os
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

from agino_store_server import catalog, content as C
from agino_store_server.state import State

from . import helpers as H

UTC = datetime.timezone.utc
DAY = datetime.datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
MILKYWAY = {"gimp": {"version": "3.0.4-3", "first_seen": "2026-10-01T00:00:00Z", "size": 1, "installed_size": 2}}


class CatalogCase(unittest.TestCase):
    keep = 3

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = self._tmp.name
        self.key, self.pub = H.make_key(self.base)
        self.cfg = H.make_config(self.base, self.key, keep_catalogs=self.keep)
        H.make_content(self.cfg.content)
        self.st = State(self.cfg.state)
        env = mock.patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        self.set_time(DAY)

    def set_time(self, when):
        os.environ["SOURCE_DATE_EPOCH"] = str(int(when.timestamp()))

    def load(self):
        return C.load(self.cfg.content)

    def build(self, **kw):
        return catalog.build(self.cfg, self.st, self.load(), **kw)

    def publish_all(self, version="2.0-1", sha=H.SHA):
        """Approve a chrome deb and make gimp available in milkyway."""
        record = H.candidate(version=version, sha=sha, status="approved")
        H.write(os.path.join(self.cfg.downloads, record["file"]), b"x" * record["size"])
        added = self.st.add_candidate(record)
        entry = {"version": version, "sha256": sha, "candidate": os.path.basename(added["_path"]),
                 "approved_at": "2026-11-02T08:00:00Z", "by": "t", "note": ""}
        self.st.set_approved("chrome", entry, {"action": "approve"})
        self.st.save_milkyway(MILKYWAY)

    def current(self):
        return os.path.join(self.cfg.preview, "current")

    def read(self, *parts):
        with open(os.path.join(self.current(), *parts), encoding="utf-8") as f:
            return json.load(f)

    def tree_dirs(self):
        return sorted(n for n in os.listdir(self.cfg.preview) if n.startswith("catalog-"))


class TestBuild(CatalogCase):
    def test_publishes_signed_tree(self):
        self.publish_all()
        summary = self.build()
        self.assertEqual(summary["serial"], 2026110201)
        self.assertEqual(summary["apps"], 2)
        self.assertTrue(summary["changed"])
        self.assertEqual(os.readlink(self.current()), "catalog-2026110201")
        self.assertTrue(H.verify_signature(self.current(), self.pub))
        catalog.verify_tree(self.current())
        manifest = self.read("manifest.json")
        self.assertEqual(manifest["serial"], 2026110201)
        self.assertEqual(manifest["created"], "2026-11-02T12:00:00Z")
        self.assertEqual(manifest["expires"], "2026-11-16T12:00:00Z")
        self.assertTrue(manifest["revision"].startswith("r2026110201-"))

    def test_catalog_contents(self):
        self.publish_all()
        self.build()
        data = self.read("catalog.json")
        self.assertEqual([a["id"] for a in data["apps"]], ["chrome", "gimp"])
        chrome, gimp = data["apps"]
        deb = chrome["package"]["deb"]
        self.assertEqual(deb["url"], "https://store.example.invalid/debs/%s/google-chrome-stable_2.0-1_amd64.deb" % H.SHA)
        self.assertEqual((deb["version"], deb["size"], deb["installed_size"]), ("2.0-1", 11, 4096))
        self.assertEqual(chrome["updated"], "2026-11-02")
        self.assertEqual(gimp["updated"], "2026-10-01")
        self.assertEqual(gimp["package"], {"kind": "milkyway", "name": "gimp"})
        self.assertEqual(gimp["launchable"], "gimp.desktop")
        self.assertRegex(gimp["icon"], r"\Aassets/[0-9a-f]{64}\.svg\Z")
        self.assertEqual([c["order"] for c in data["categories"]], [1, 2])
        self.assertEqual(data["featured"][0]["apps"], ["chrome", "gimp"])
        self.assertTrue(os.path.isfile(os.path.join(self.current(), gimp["icon"])))

    def test_details(self):
        self.publish_all()
        self.build()
        gimp = self.read("apps", "gimp.json")
        self.assertEqual(gimp["screenshots"]["zh_CN"][0]["width"], 320)
        self.assertEqual(gimp["screenshots"]["en_US"][0]["height"], 400)
        self.assertEqual(gimp["screenshots"]["en_US"][0]["image"], gimp["screenshots"]["en_US"][0]["thumb"])
        self.assertEqual(gimp["releases"][0]["version"], "3.0.4-3")
        chrome = self.read("apps", "chrome.json")
        self.assertEqual(chrome["install_profile"]["debconf"], ["code code/add-microsoft-repo boolean false"])
        self.assertEqual(chrome["install_profile"]["default_files"][0]["path"], "/etc/default/google-chrome")
        self.assertIsNone(chrome["maintainer"])
        self.assertEqual(chrome["releases"], [{"version": "2.0-1", "date": "2026-11-02",
                                               "notes": {"zh_CN": ["新版"], "en_US": ["New"]}}])

    def test_older_releases_need_candidates(self):
        self.publish_all()
        self.st.add_candidate(H.candidate(version="1.0-1", sha="b" * 64, status="superseded"))
        H.edit_yaml(os.path.join(self.cfg.content, "apps", "chrome", "app.yaml"), lambda d: dict(
            d, release_notes=dict(d["release_notes"], **{"0.5-1": {"zh_CN": ["x"], "en_US": ["y"]}})))
        self.build()
        versions = [r["version"] for r in self.read("apps", "chrome.json")["releases"]]
        self.assertEqual(versions, ["2.0-1", "1.0-1"])

    def test_deb_is_hard_linked(self):
        self.publish_all()
        self.build()
        name = "google-chrome-stable_2.0-1_amd64.deb"
        src = os.path.join(self.cfg.downloads, H.SHA, name)
        dest = os.path.join(self.cfg.preview, "debs", H.SHA, name)
        self.assertEqual(os.stat(src).st_ino, os.stat(dest).st_ino)

    def test_skips_apps_without_inputs(self):
        summary = self.build()
        reasons = {s["id"]: s["reason"] for s in summary["skipped"]}
        self.assertEqual(reasons["chrome"], "no approved version")
        self.assertEqual(reasons["gimp"], "package not in milkyway")
        self.assertEqual(summary["apps"], 0)
        self.assertEqual(self.read("catalog.json")["featured"], [])

    def test_skips_deb_with_bad_record_or_missing_file(self):
        self.publish_all()
        os.unlink(os.path.join(self.cfg.downloads, H.SHA, "google-chrome-stable_2.0-1_amd64.deb"))
        summary = self.build()
        self.assertEqual([s["id"] for s in summary["skipped"]], ["chrome"])
        self.assertIn("missing", summary["skipped"][0]["reason"])
        self.assertEqual(summary["apps"], 1)

    def test_record_with_path_traversal_is_skipped(self):
        self.publish_all()
        record = self.st.find_candidate("chrome", "2.0-1")
        self.st.update_candidate(record, {"filename": "../../evil.deb", "file": H.SHA + "/../../evil.deb"})
        summary = self.build()
        self.assertIn("bad file name", summary["skipped"][0]["reason"])

    def test_invalid_screenshot_skips_app(self):
        self.publish_all()
        H.write(os.path.join(self.cfg.content, "apps", "gimp", "screenshots", "en_US", "1.png"), b"not a png")
        summary = self.build()
        self.assertEqual([s["id"] for s in summary["skipped"]], ["gimp"])

    def test_failed_build_keeps_current(self):
        self.publish_all()
        self.build()
        broken = H.make_config(self.base, os.path.join(self.base, "missing.pem"), keep_catalogs=self.keep)
        self.set_time(DAY + datetime.timedelta(hours=30))
        with self.assertRaises(subprocess.CalledProcessError):
            catalog.build(broken, self.st, self.load())
        self.assertEqual(os.readlink(self.current()), "catalog-2026110201")
        self.assertEqual(self.tree_dirs(), ["catalog-2026110201"])
        self.assertEqual(self.st.catalog_state()["serial"], 2026110201)


class TestSerialAndRenewal(CatalogCase):
    def test_serial_counts_up_within_a_day_and_resets(self):
        self.publish_all()
        self.assertEqual([self.build()["serial"] for _ in range(2)], [2026110201, 2026110202])
        self.set_time(DAY + datetime.timedelta(days=1))
        self.assertEqual(self.build()["serial"], 2026110301)

    def test_serial_never_goes_backwards(self):
        self.publish_all()
        self.st.save_catalog_state({"serial": 2026110550, "inputs_sha256": "x"})
        self.assertEqual(self.build()["serial"], 2026110551)

    def test_serial_exhausted(self):
        self.publish_all()
        self.st.save_catalog_state({"serial": 2026110299, "inputs_sha256": "x"})
        with self.assertRaises(catalog.CatalogError):
            self.build()

    def test_if_changed_does_nothing_when_fresh(self):
        self.publish_all()
        first = self.build()
        self.set_time(DAY + datetime.timedelta(hours=5))
        again = self.build(if_changed=True)
        self.assertFalse(again["changed"])
        self.assertEqual(again["serial"], first["serial"])
        self.assertEqual(self.tree_dirs(), ["catalog-2026110201"])

    def test_if_changed_renews_when_old(self):
        self.publish_all()
        first = self.build()
        self.set_time(DAY + datetime.timedelta(hours=21))
        again = self.build(if_changed=True)
        self.assertTrue(again["changed"])
        self.assertTrue(again["renewed"])
        self.assertGreater(again["serial"], first["serial"])
        self.assertEqual(again["serial"], 2026110301)
        self.assertEqual(self.read("manifest.json")["created"], "2026-11-03T09:00:00Z")

    def test_if_changed_rebuilds_on_new_input(self):
        self.publish_all()
        self.build()
        self.st.save_milkyway({"gimp": dict(MILKYWAY["gimp"], version="3.0.5-1", first_seen="2026-11-02T01:00:00Z")})
        again = self.build(if_changed=True)
        self.assertTrue(again["changed"])
        self.assertFalse(again["renewed"])
        self.assertEqual(self.read("catalog.json")["apps"][1]["updated"], "2026-11-02")

    def test_if_changed_rebuilds_when_content_changes(self):
        self.publish_all()
        self.build()
        H.write(os.path.join(self.cfg.content, "apps", "gimp", "icon.svg"), b"<svg/>")
        self.assertTrue(self.build(if_changed=True)["changed"])

    def test_if_changed_rebuilds_when_current_is_gone(self):
        self.publish_all()
        self.build()
        os.unlink(self.current())
        self.assertTrue(self.build(if_changed=True)["changed"])

    def test_unrelated_milkyway_packages_do_not_change_inputs(self):
        self.publish_all()
        self.build()
        self.st.save_milkyway(dict(MILKYWAY, other={"version": "1", "first_seen": "2026-11-02T00:00:00Z"}))
        self.assertFalse(self.build(if_changed=True)["changed"])


class TestPrune(CatalogCase):
    keep = 2

    def test_keeps_newest(self):
        self.publish_all()
        for _ in range(4):
            self.build()
        self.assertEqual(self.tree_dirs(), ["catalog-2026110203", "catalog-2026110204"])
        self.assertEqual(os.readlink(self.current()), "catalog-2026110204")

    def test_ignores_foreign_names(self):
        self.publish_all()
        os.makedirs(os.path.join(self.cfg.preview, "catalog-old"))
        os.makedirs(os.path.join(self.cfg.preview, "notes"))
        for _ in range(3):
            self.build()
        self.assertIn("catalog-old", os.listdir(self.cfg.preview))
        self.assertIn("notes", os.listdir(self.cfg.preview))


class TestImageSize(unittest.TestCase):
    def size_of(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "image")
            H.write(path, data)
            return catalog.image_size(path)

    def test_png(self):
        self.assertEqual(self.size_of(H.png(123, 45)), (123, 45))

    def test_jpeg_skips_other_segments(self):
        app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        sof = b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 300, 400) + b"\x03" + b"\x00" * 12
        self.assertEqual(self.size_of(b"\xff\xd8" + app0 + sof), (400, 300))

    def test_webp_variants(self):
        riff = b"RIFF" + struct.pack("<I", 100) + b"WEBP"
        vp8x = riff + b"VP8X" + struct.pack("<I", 10) + b"\x00" * 4 + (799).to_bytes(3, "little") + (599).to_bytes(3, "little")
        vp8l = riff + b"VP8L" + struct.pack("<I", 5) + b"\x2f" + struct.pack("<I", 799 | (599 << 14))
        vp8 = riff + b"VP8 " + struct.pack("<I", 10) + b"\x00" * 3 + b"\x9d\x01\x2a" + struct.pack("<HH", 800, 600)
        for data in (vp8x, vp8l, vp8):
            self.assertEqual(self.size_of(data), (800, 600))

    def test_rejects_garbage(self):
        with self.assertRaises(catalog.CatalogError):
            self.size_of(b"<svg/>")


class TestVersionCompare(unittest.TestCase):
    def test_order(self):
        cmp = catalog.compare_versions
        self.assertGreater(cmp("1.0", "1.0~rc1"), 0)
        self.assertGreater(cmp("1:0.1", "2.0"), 0)
        self.assertLess(cmp("1.0-1", "1.0-2"), 0)
        self.assertGreater(cmp("1.10", "1.9"), 0)
        self.assertGreater(cmp("1.0a", "1.0"), 0)
        self.assertGreater(cmp("156.0.8078.79-1", "156.0.8078.9-1"), 0)
        self.assertEqual(cmp("2.0-1", "2.0-1"), 0)
        self.assertEqual(cmp("1.0", "1.00"), 0)


if __name__ == "__main__":
    unittest.main()
