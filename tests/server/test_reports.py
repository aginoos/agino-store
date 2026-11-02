# SPDX-License-Identifier: GPL-3.0-or-later
import base64
import json
import os
import subprocess
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


def raw_public_key(private_pem):
    """The 32 raw bytes of the Ed25519 public key belonging to a PEM private key."""
    der = subprocess.run(["openssl", "pkey", "-in", private_pem, "-pubout", "-outform", "DER"],
                         check=True, capture_output=True).stdout
    return der[-32:]


def sign_bytes(private_pem, data, workdir):
    """One line of base64: the Ed25519 signature of data."""
    src, out = os.path.join(workdir, ".sign-in"), os.path.join(workdir, ".sign-out")
    H.write(src, data)
    subprocess.run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", private_pem, "-in", src, "-out", out],
                   check=True, capture_output=True)
    with open(out, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii") + "\n"


def install_key(private_pem, keys_dir, name="sandbox.pub"):
    H.write(os.path.join(keys_dir, name), base64.b64encode(raw_public_key(private_pem)) + b"\n")


class TestReports(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = H.make_config(self._tmp.name)
        self.st = State(self.cfg.state)
        self.st.add_candidate(H.candidate(checks={"deps": {"ok": True}, "sandbox": {"status": "missing"}}))
        self.keys = os.path.join(self._tmp.name, "sandbox-keys")
        self.private, _ = H.make_key(self._tmp.name)
        install_key(self.private, self.keys)

    def ingest(self):
        return reports.ingest(self.cfg, self.st, self.keys)

    def put(self, name, doc, sign=True):
        path = os.path.join(self.cfg.reports, name)
        data = doc if isinstance(doc, bytes) else json.dumps(doc).encode("utf-8")
        H.write(path, data)
        if sign:
            H.write(path + ".sig", sign_bytes(self.private, data, self._tmp.name), "w")
        return path

    def sandbox(self):
        return self.st.find_candidate("chrome", "2.0-1")["checks"]["sandbox"]

    def where(self, folder, name):
        return os.path.lexists(os.path.join(self.cfg.reports, folder, name))

    def test_valid_report_is_applied_and_moved(self):
        self.put("chrome-1.json", report())
        results = self.ingest()
        self.assertEqual([r["result"] for r in results], ["done"])
        sandbox = self.sandbox()
        self.assertEqual((sandbox["status"], sandbox["report"], sandbox["summary"]), ("pass", "chrome-1.json", "all good"))
        self.assertEqual(sandbox["details"]["desktop_file"], "google-chrome.desktop")
        self.assertTrue(self.where("done", "chrome-1.json"))
        self.assertFalse(self.where("", "chrome-1.json"))
        self.assertEqual(self.st.find_candidate("chrome", "2.0-1")["checks"]["deps"], {"ok": True})

    def test_fail_report_is_recorded(self):
        self.put("r.json", report(status="fail", summary="crashed"))
        self.ingest()
        self.assertEqual(self.sandbox()["status"], "fail")

    def rejected(self, name, doc):
        self.put(name, doc)
        results = self.ingest()
        self.assertEqual([r["result"] for r in results], ["rejected"], results)
        self.assertTrue(self.where("rejected", name))
        self.assertEqual(self.sandbox()["status"], "missing")
        return results[0]["reason"]

    def test_sandbox_details_are_accepted_but_not_copied(self):
        self.put("d.json", report(details={"apt_install": {"rc": 0}, "journal_errors": ["x"]}))
        self.assertEqual([r["result"] for r in self.ingest()], ["done"])
        self.assertNotIn("apt_install", str(self.sandbox()))

    def test_details_must_be_an_object(self):
        self.assertIn("details", self.rejected("a.json", report(details=["x"])))

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
        self.assertEqual(self.ingest()[0]["result"], "rejected")

    def test_unsigned_report_is_rejected(self):
        self.put("a.json", report(), sign=False)
        results = self.ingest()
        self.assertEqual([r["result"] for r in results], ["rejected"])
        self.assertIn("unsigned", results[0]["reason"])
        self.assertEqual(self.sandbox()["status"], "missing")

    def test_bad_signature_is_rejected(self):
        path = self.put("a.json", report())
        H.write(path, json.dumps(report(summary="tampered")).encode("utf-8"))
        self.assertIn("signature", self.ingest()[0]["reason"])
        self.assertEqual(self.sandbox()["status"], "missing")
        self.assertTrue(self.where("rejected", "a.json.sig"))

    def test_signature_by_unknown_key_is_rejected(self):
        other = os.path.join(self._tmp.name, "other")
        os.makedirs(other)
        key, _ = H.make_key(other)
        data = json.dumps(report()).encode("utf-8")
        H.write(os.path.join(self.cfg.reports, "a.json"), data)
        H.write(os.path.join(self.cfg.reports, "a.json.sig"), sign_bytes(key, data, other), "w")
        self.assertEqual(self.ingest()[0]["result"], "rejected")

    def test_garbage_signature_is_rejected(self):
        path = self.put("a.json", report())
        H.write(path + ".sig", "not base64!!\n", "w")
        self.assertEqual(self.ingest()[0]["result"], "rejected")

    def test_no_keys_configured_rejects_everything(self):
        self.put("a.json", report())
        results = reports.ingest(self.cfg, self.st, os.path.join(self._tmp.name, "nokeys"))
        self.assertEqual([r["result"] for r in results], ["rejected"])

    def test_sig_symlink_is_not_followed(self):
        path = self.put("a.json", report())
        os.unlink(path + ".sig")
        outside = os.path.join(self._tmp.name, "outside.sig")
        H.write(outside, sign_bytes(self.private, json.dumps(report()).encode("utf-8"), self._tmp.name), "w")
        os.symlink(outside, path + ".sig")
        self.assertEqual(self.ingest()[0]["result"], "rejected")

    def test_done_symlink_is_refused(self):
        self.put("a.json", report())
        outside = os.path.join(self._tmp.name, "elsewhere")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.cfg.reports, "done"))
        with self.assertRaises(reports.ReportError):
            self.ingest()
        self.assertEqual(os.listdir(outside), [])
        self.assertTrue(self.where("", "a.json"))
        self.assertEqual(self.sandbox()["status"], "missing")

    def test_rejected_not_a_directory_is_refused(self):
        self.put("a.json", report())
        H.write(os.path.join(self.cfg.reports, "rejected"), b"x")
        with self.assertRaises(reports.ReportError):
            self.ingest()
        self.assertTrue(self.where("", "a.json"))

    def test_reports_dir_symlink_is_refused(self):
        real = os.path.join(self._tmp.name, "real-reports")
        os.makedirs(real)
        os.symlink(real, self.cfg.reports)
        with self.assertRaises(reports.ReportError):
            self.ingest()

    def test_symlink_is_not_followed(self):
        outside = os.path.join(self._tmp.name, "outside.json")
        H.write(outside, json.dumps(report()).encode("utf-8"))
        os.makedirs(self.cfg.reports)
        os.symlink(outside, os.path.join(self.cfg.reports, "link.json"))
        H.write(os.path.join(self.cfg.reports, "link.json.sig"),
                sign_bytes(self.private, json.dumps(report()).encode("utf-8"), self._tmp.name), "w")
        results = self.ingest()
        self.assertEqual([r["result"] for r in results], ["rejected"])
        self.assertTrue(os.path.islink(os.path.join(self.cfg.reports, "rejected", "link.json")))
        self.assertTrue(os.path.isfile(outside))
        self.assertEqual(self.sandbox()["status"], "missing")

    def test_other_entries_are_ignored(self):
        self.put("notes.txt", b"hello")
        self.put(".hidden.json", report())
        os.makedirs(os.path.join(self.cfg.reports, "dir.json"))
        self.assertEqual(self.ingest(), [])
        self.assertTrue(self.where("", "notes.txt"))

    def test_name_collision_in_done(self):
        for _ in range(2):
            self.put("same.json", report())
            self.ingest()
        self.assertTrue(self.where("done", "same.json"))
        self.assertTrue(self.where("done", "same.json.1"))

    def test_missing_reports_dir(self):
        self.assertEqual(self.ingest(), [])


if __name__ == "__main__":
    unittest.main()
