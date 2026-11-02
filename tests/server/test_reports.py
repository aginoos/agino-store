# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import tempfile
import unittest

from agino_store_server import reports
from agino_store_server.state import State

from . import helpers as H


def report(**changes):
    doc = {"schema": 1, "app": "chrome", "version": "2.0-1", "sha256": H.SHA, "status": "pass",
           "started": "2026-11-02T08:00:00Z", "finished": "2026-11-02T08:05:00Z", "host": "store",
           "checks": {"installed": True, "third_party_sources": [], "desktop_file": "google-chrome.desktop",
                      "window_seen": True, "new_errors": 0, "removed_clean": True},
           "summary": "all good"}
    doc.update(changes)
    return doc


class TestReports(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = H.make_config(self._tmp.name)
        self.st = State(self.cfg.state)
        self.st.add_candidate(H.candidate(checks={"deps": {"ok": True}, "sandbox": {"status": "missing"}}))

    def put(self, name, doc):
        path = os.path.join(self.cfg.reports, name)
        H.write(path, doc if isinstance(doc, bytes) else json.dumps(doc).encode("utf-8"))
        return path

    def sandbox(self):
        return self.st.find_candidate("chrome", "2.0-1")["checks"]["sandbox"]

    def where(self, folder, name):
        return os.path.lexists(os.path.join(self.cfg.reports, folder, name))

    def test_valid_report_is_applied_and_moved(self):
        self.put("chrome-1.json", report())
        results = reports.ingest(self.cfg, self.st)
        self.assertEqual([r["result"] for r in results], ["done"])
        sandbox = self.sandbox()
        self.assertEqual((sandbox["status"], sandbox["report"], sandbox["summary"]), ("pass", "chrome-1.json", "all good"))
        self.assertEqual(sandbox["details"]["desktop_file"], "google-chrome.desktop")
        self.assertTrue(self.where("done", "chrome-1.json"))
        self.assertFalse(self.where("", "chrome-1.json"))
        self.assertEqual(self.st.find_candidate("chrome", "2.0-1")["checks"]["deps"], {"ok": True})

    def test_fail_report_is_recorded(self):
        self.put("r.json", report(status="fail", summary="crashed"))
        reports.ingest(self.cfg, self.st)
        self.assertEqual(self.sandbox()["status"], "fail")

    def rejected(self, name, doc):
        self.put(name, doc)
        results = reports.ingest(self.cfg, self.st)
        self.assertEqual([r["result"] for r in results], ["rejected"], results)
        self.assertTrue(self.where("rejected", name))
        self.assertEqual(self.sandbox()["status"], "missing")
        return results[0]["reason"]

    def test_unknown_field(self):
        self.assertIn("unexpected", self.rejected("a.json", report(extra=1)))

    def test_missing_field(self):
        doc = report()
        del doc["sha256"]
        self.assertIn("missing", self.rejected("a.json", doc))

    def test_wrong_hash(self):
        self.assertIn("no candidate", self.rejected("a.json", report(sha256="b" * 64)))

    def test_unknown_version(self):
        self.assertIn("no candidate", self.rejected("a.json", report(version="9.9")))

    def test_bad_status_and_schema(self):
        self.rejected("a.json", report(status="maybe"))
        self.rejected("b.json", report(schema=2))
        self.rejected("c.json", report(schema=True))

    def test_bad_app_id(self):
        self.rejected("a.json", report(app="../x"))

    def test_bad_check_types(self):
        self.rejected("a.json", report(checks={"installed": "yes"}))
        self.rejected("b.json", report(checks={"unknown": True}))
        self.rejected("c.json", report(checks={"new_errors": True}))

    def test_not_json(self):
        self.assertIn("not valid JSON", self.rejected("a.json", b"\xff\xfe{"))
        self.rejected("b.json", b"[1, 2]")

    def test_too_large(self):
        self.assertIn("1 MiB", self.rejected("a.json", b" " * ((1 << 20) + 1)))

    def test_candidate_must_be_open(self):
        record = self.st.find_candidate("chrome", "2.0-1")
        self.st.update_candidate(record, {"status": "rejected"})
        self.put("a.json", report())
        self.assertEqual(reports.ingest(self.cfg, self.st)[0]["result"], "rejected")

    def test_symlink_is_not_followed(self):
        outside = os.path.join(self._tmp.name, "outside.json")
        H.write(outside, json.dumps(report()).encode("utf-8"))
        os.makedirs(self.cfg.reports)
        os.symlink(outside, os.path.join(self.cfg.reports, "link.json"))
        results = reports.ingest(self.cfg, self.st)
        self.assertEqual([r["result"] for r in results], ["rejected"])
        self.assertTrue(os.path.islink(os.path.join(self.cfg.reports, "rejected", "link.json")))
        self.assertTrue(os.path.isfile(outside))
        self.assertEqual(self.sandbox()["status"], "missing")

    def test_other_entries_are_ignored(self):
        self.put("notes.txt", b"hello")
        self.put(".hidden.json", report())
        os.makedirs(os.path.join(self.cfg.reports, "dir.json"))
        self.assertEqual(reports.ingest(self.cfg, self.st), [])
        self.assertTrue(self.where("", "notes.txt"))

    def test_name_collision_in_done(self):
        for _ in range(2):
            self.put("same.json", report())
            reports.ingest(self.cfg, self.st)
        self.assertTrue(self.where("done", "same.json"))
        self.assertTrue(self.where("done", "same.json.1"))

    def test_missing_reports_dir(self):
        self.assertEqual(reports.ingest(self.cfg, self.st), [])


if __name__ == "__main__":
    unittest.main()
