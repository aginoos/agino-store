# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import subprocess
import unittest
from unittest import mock

from agino_store_server import cli, status, status_sandbox

from . import helpers as H
from .test_cli import CliCase, sha


def snapshot(*roots):
    out = {}
    for root in roots:
        for dirpath, dirs, files in os.walk(root):
            for name in dirs + files:
                path = os.path.join(dirpath, name)
                info = os.lstat(path)
                out[path] = (info.st_mode, info.st_size, info.st_mtime_ns)
    return out


class StatusCase(CliCase):
    def setUp(self):
        super().setUp()
        base = os.path.dirname(self.conf)
        self.queue = os.path.join(base, "queue")
        self.sbx_state = os.path.join(base, "sbx-state")
        self.shots = os.path.join(base, "screenshots")
        with open(self.conf, encoding="utf-8") as f:
            text = f.read()
        extra = "[paths]\nsandbox_queue = %s\nsandbox_state = %s\nscreenshots = %s\n" % (
            self.queue, self.sbx_state, self.shots)
        H.write(self.conf, text.replace("[paths]\n", extra, 1), "w")
        patcher = mock.patch.object(status_sandbox.subprocess, "run", side_effect=self.fake_systemctl)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def fake_systemctl(argv, **kw):
        unit = argv[-1]
        out = "LoadState=loaded\nActiveState=active\nSubState=waiting\nResult=success\nExecMainStatus=0\n"
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    def doc(self, *sections):
        argv = ["status", "--json"]
        for s in sections:
            argv += ["--section", s]
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def errors(self, doc, section):
        return [e for e in doc["errors"] if e["section"] == section]


class TestStatus(StatusCase):
    def test_apps(self):
        self.add(version="2.0-1")
        self.st.set_approved("chrome", {"version": "1.0-1", "approved_at": "t", "by": "bob"}, {"a": 1})
        image = {"lang": "zh_CN", "file": "01-main.png", "scene": "main", "sha256": "a" * 64, "size": 1, "width": 900,
                 "height": 320, "source": "manual", "status": "approved", "reviewer": "bob", "reviewed": "t",
                 "note": "", "origin": ""}
        H.write(os.path.join(self.shots, "gimp", "meta.json"), json.dumps({
            "schema": 1, "app": "gimp", "updated": "t",
            "images": [image, dict(image, lang="en_US", sha256="b" * 64)]}), "w")
        doc = self.doc("apps")
        apps = {a["id"]: a for a in doc["sections"]["apps"]}
        self.assertEqual(set(apps), {"chrome", "gimp"})
        self.assertEqual(apps["gimp"]["shots"]["state"], "approved")
        self.assertEqual(apps["gimp"]["shots"]["scenes"], 1)
        self.assertEqual(apps["gimp"]["shots"]["langs"], ["zh_CN", "en_US"])
        self.assertEqual(apps["chrome"]["shots"]["state"], "none")
        self.assertEqual(apps["chrome"]["approved"], {"version": "1.0-1", "approved_at": "t", "by": "bob"})
        self.assertEqual(apps["chrome"]["current_candidate_status"], "pending")
        self.assertEqual(apps["chrome"]["description_source"], "submitter")
        self.assertTrue(apps["chrome"]["proprietary"])
        self.assertEqual(apps["gimp"]["kind"], "milkyway")
        self.assertIsNone(apps["gimp"]["approved"])
        self.assertEqual(doc["schema"], 1)

    def test_apps_broken_content_is_an_error(self):
        os.unlink(os.path.join(self.cfg.content, "apps", "gimp", "icon.svg"))
        doc = self.doc("apps")
        self.assertEqual(doc["sections"]["apps"], [])
        self.assertTrue(self.errors(doc, "apps"))

    def test_candidates(self):
        self.add(version="2.0-1", origin={"type": "apt", "signed_by": "ABCD", "inrelease_sha256": "x"},
                 inspect_dir_listing=["secret"])
        self.add(version="1.0-1", char="b", origin={"type": "manual", "by": "me"}, status="approved",
                 checks={"deps": {"ok": False, "detail": "missing x"},
                         "inspect": {"scripts": {"postinst": "AAA"}, "flags": ["setuid usr/lib/x"]},
                         "diff": {"against": None, "scripts_changed": ["postinst"], "flags_added": [], "summary": "s"},
                         "sandbox": {"status": "fail", "summary": "boom", "report": "r.json", "details": {"x": 1}}})
        doc = self.doc("candidates")
        by_version = {c["version"]: c for c in doc["sections"]["candidates"]}
        self.assertEqual(by_version["2.0-1"]["source_class"], "official")
        self.assertEqual(by_version["2.0-1"]["origin"], {"type": "apt", "signed_by": "ABCD"})
        second = by_version["1.0-1"]
        self.assertEqual(second["source_class"], "personal")
        self.assertEqual(second["checks"]["sandbox"], {"status": "fail", "summary": "boom", "report": "r.json"})
        self.assertEqual(second["checks"]["inspect"], {"flags": ["setuid usr/lib/x"]})
        self.assertNotIn("AAA", json.dumps(second))
        self.assertNotIn("inspect_dir_listing", json.dumps(by_version["2.0-1"]))
        self.assertEqual(second["checks"]["deps"], {"ok": False, "detail": "missing x"})

    def test_approved(self):
        self.st.set_approved("chrome", {"version": "1.0-1", "sha256": sha("a"), "approved_at": "t", "by": "x",
                                        "note": ""}, {"action": "approve", "app": "chrome"})
        with open(os.path.join(self.cfg.state, "approvals.log"), "a", encoding="utf-8") as f:
            f.write("not json\n")
        for i in range(60):
            self.st.append_log({"n": i})
        doc = self.doc("approved")["sections"]["approved"]
        self.assertEqual(doc["apps"]["chrome"]["version"], "1.0-1")
        self.assertEqual(len(doc["log"]), 50)
        self.assertEqual(doc["log"][-1], {"n": 59})

    def test_vendors(self):
        self.st.save_vendor_memo("chrome", {"last_check": "a", "last_ok": "b", "last_error": None,
                                            "seen_version": "2.0-1", "etag": "zzz"})
        self.st.save_vendor_memo("gone", {"last_check": "c"})
        vendors = {v["id"]: v for v in self.doc("vendors")["sections"]["vendors"]}
        self.assertEqual(vendors["chrome"]["seen_version"], "2.0-1")
        self.assertEqual(vendors["chrome"]["name"], "google-chrome-stable")
        self.assertNotIn("etag", vendors["chrome"])
        self.assertFalse(vendors["gone"]["in_content"])

    def test_catalog(self):
        self.st.save_catalog_state({"serial": 7, "revision": "r7-x", "built_at": "t", "inputs_sha256": "h", "apps": 2})
        cur = os.path.join(self.cfg.preview, "catalog-7")
        H.write(os.path.join(cur, "manifest.json"), json.dumps({"serial": 7, "expires": "2026-12-01T00:00:00Z"}), "w")
        H.write(os.path.join(cur, "manifest.json.sig"), "sig", "w")
        H.write(os.path.join(cur, "catalog.json"), json.dumps({"apps": [{}, {}]}), "w")
        os.symlink("catalog-7", os.path.join(self.cfg.preview, "current"))
        cat = self.doc("catalog")["sections"]["catalog"]
        self.assertEqual(cat["state"]["serial"], 7)
        self.assertEqual(cat["published"]["valid_until"], "2026-12-01T00:00:00Z")
        self.assertEqual(cat["published"]["apps"], 2)
        self.assertTrue(cat["published"]["signature_present"])

    def test_sandbox(self):
        name = "chrome_" + "a" * 12
        H.write(os.path.join(self.queue, name + ".json"),
                json.dumps({"mode": "shots", "app": "chrome", "version": "2.0-1", "size": 11,
                            "queued": "2026-11-02T08:00:00Z"}), "w")
        H.write(os.path.join(self.queue, name + ".deb"), b"x")
        H.write(os.path.join(self.sbx_state, name + ".json"),
                json.dumps({"status": "error", "attempts": 2, "reason": "qga"}), "w")
        H.write(os.path.join(self.cfg.reports, "r1.json"),
                json.dumps({"app": "chrome", "version": "2.0-1", "status": "pass", "summary": "ok"}), "w")
        H.write(os.path.join(self.cfg.reports, "done", "r2.json"), json.dumps({"app": "x", "status": "fail"}), "w")
        sbx = self.doc("sandbox")["sections"]["sandbox"]
        self.assertEqual(sbx["queue"][0]["job"], name)
        self.assertEqual(sbx["queue"][0]["mode"], "shots")
        self.assertEqual(sbx["jobs"][0]["attempts"], 2)
        self.assertEqual({r["name"] for r in sbx["reports"]}, {"r1.json", "r2.json"})
        self.assertEqual([r for r in sbx["reports"] if r["name"] == "r1.json"][0]["result"], "pass")

    def test_timers(self):
        timers = self.doc("timers")["sections"]["timers"]
        self.assertEqual(len(timers), 10)
        self.assertEqual(timers[0]["unit"], "agino-store-track.service")
        self.assertEqual(timers[0]["ActiveState"], "active")

    def test_timers_without_systemctl(self):
        with mock.patch.object(status_sandbox.subprocess, "run", side_effect=FileNotFoundError):
            doc = self.doc("timers")
        self.assertEqual(doc["sections"]["timers"], [])
        self.assertTrue(self.errors(doc, "timers"))

    def test_invalid_json_tolerated(self):
        good = self.add(version="2.0-1")
        bad = os.path.join(self.cfg.state, "candidates", "chrome", "9.0-1_bbbbbbbbbbbb.json")
        H.write(bad, b'{"app": "chr', "wb")
        H.write(os.path.join(self.cfg.state, "approved.json"), b"[1,", "wb")
        H.write(os.path.join(self.cfg.state, "catalog.json"), b"nope", "wb")
        doc = self.doc()
        self.assertEqual(len(doc["sections"]["candidates"]), 1)
        for section in ("candidates", "approved", "catalog"):
            self.assertTrue(self.errors(doc, section), section)
        self.assertIn("apps", doc["sections"])
        self.assertTrue(os.path.exists(good["_path"]))

    def test_no_lock_taken(self):
        self.add()
        with self.st.lock():
            code, out, err = self.run_cli("status", "--json", "--section", "candidates")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(json.loads(out)["sections"]["candidates"]), 1)

    def test_sanitizer(self):
        self.add(version="2.0-1", checks={"deps": {"ok": True, "detail": "bad\x1b[31mred\x07"},
                                         "sandbox": {"status": "pass", "summary": "x\x00y"}})
        code, out, _ = self.run_cli("status", "--json", "--section", "candidates")
        self.assertNotIn("\\u001b", out)
        self.assertNotIn("\\u0000", out)
        self.assertNotIn("\\u0007", out)
        self.assertIn("bad?[31mred?", out)
        self.assertTrue(out.isascii())

    def test_unknown_section(self):
        code, out, err = self.run_cli("status", "--json", "--section", "nope")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("unknown section", err)
        self.assertEqual(out, "")

    def test_future_section_is_absent(self):
        doc = self.doc("builds", "timers")
        self.assertEqual(list(doc["sections"]), ["timers"])

    def test_requires_json_flag(self):
        self.assertEqual(self.run_cli("status")[0], cli.EXIT_USAGE)

    def test_read_only(self):
        self.add()
        self.st.set_approved("chrome", {"version": "1.0-1"}, {"n": 1})
        self.st.save_vendor_memo("chrome", {"last_check": "a"})
        H.write(os.path.join(self.queue, "chrome_" + "a" * 12 + ".json"), "{}", "w")
        H.write(os.path.join(self.cfg.reports, "r.json"), "{}", "w")
        base = os.path.dirname(self.conf)
        before = snapshot(base)
        self.doc()
        self.assertEqual(snapshot(base), before)

    def test_symlinked_state_file_is_refused(self):
        os.makedirs(self.cfg.state, exist_ok=True)
        target = os.path.join(os.path.dirname(self.conf), "elsewhere.json")
        H.write(target, "{}", "w")
        os.symlink(target, os.path.join(self.cfg.state, "approved.json"))
        doc = self.doc("approved")
        self.assertTrue(self.errors(doc, "approved"))

    def test_size_guard(self):
        for i in range(16):
            self.add(version="%d.0-1" % (i + 1), char="%x" % i, found="2026-11-01T08:%02d:00Z" % i)
        with mock.patch.object(status, "MAX_BYTES", 3000):
            code, out, _ = self.run_cli("status", "--json", "--section", "candidates")
        doc = json.loads(out)
        self.assertEqual(doc["truncated"], ["candidates"])
        self.assertLess(len(out), 3200)
        self.assertGreater(len(doc["sections"]["candidates"]), 0)
        newest = max(c["found"] for c in doc["sections"]["candidates"])
        self.assertEqual(newest, "2026-11-01T08:15:00Z")


if __name__ == "__main__":
    unittest.main()
