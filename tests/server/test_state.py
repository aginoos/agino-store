# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import tempfile
import unittest

from agino_store_server import state

from . import helpers as H


class TestState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "state")
        self.st = state.State(self.root)

    def test_lock_is_exclusive(self):
        with self.st.lock():
            with self.assertRaises(state.StateLocked):
                with state.State(self.root).lock():
                    pass
        with state.State(self.root).lock():
            pass

    def test_vendor_memo_roundtrip_and_default(self):
        self.assertEqual(self.st.vendor_memo("chrome"), {})
        self.st.save_vendor_memo("chrome", {"etag": "x"})
        self.assertEqual(self.st.vendor_memo("chrome"), {"etag": "x"})

    def test_bad_ids_rejected(self):
        for bad in ("../x", "A", "", "a/b"):
            with self.assertRaises(state.StateError):
                self.st.vendor_memo(bad)

    def test_no_temporary_files_left(self):
        self.st.save_milkyway({"gimp": {"version": "1"}})
        self.assertEqual(os.listdir(self.root), ["milkyway.json"])

    def test_add_candidate_is_idempotent(self):
        first = self.st.add_candidate(H.candidate())
        second = self.st.add_candidate(H.candidate(status="rejected"))
        self.assertEqual(first["_path"], second["_path"])
        self.assertEqual(second["status"], "pending")
        self.assertEqual(len(self.st.candidates()), 1)

    def test_candidates_filter(self):
        self.st.add_candidate(H.candidate())
        self.st.add_candidate(H.candidate(version="3.0-1", sha="b" * 64, status="rejected"))
        self.assertEqual(len(self.st.candidates(app="chrome")), 2)
        self.assertEqual([r["version"] for r in self.st.candidates(status="rejected")], ["3.0-1"])
        self.assertEqual(self.st.candidates(app="gimp"), [])

    def test_find_candidate(self):
        self.st.add_candidate(H.candidate())
        self.assertIsNotNone(self.st.find_candidate("chrome", "2.0-1"))
        self.assertIsNotNone(self.st.find_candidate("chrome", "2.0-1", H.SHA))
        self.assertIsNone(self.st.find_candidate("chrome", "2.0-1", "b" * 64))

    def test_awaiting_upload_gets_real_name(self):
        record = self.st.add_candidate(H.candidate(sha=None, status="awaiting-upload"))
        self.assertTrue(record["_path"].endswith("2.0-1_upload.json"))
        updated = self.st.update_candidate(record, {"sha256": H.SHA, "status": "pending"})
        self.assertTrue(updated["_path"].endswith("2.0-1_aaaaaaaaaaaa.json"))
        self.assertFalse(os.path.exists(record["_path"]))
        self.assertEqual(len(self.st.candidates()), 1)

    def test_update_does_not_mutate_input(self):
        record = self.st.add_candidate(H.candidate())
        before = dict(record)
        self.st.update_candidate(record, {"status": "approved"})
        self.assertEqual(record, before)
        self.assertEqual(self.st.candidates()[0]["status"], "approved")

    def test_epoch_version_is_filename_safe(self):
        record = self.st.add_candidate(H.candidate(version="1:2.0-1"))
        self.assertNotIn(":", os.path.basename(record["_path"]))

    def test_approved_and_log(self):
        self.assertEqual(self.st.approved(), {})
        self.st.set_approved("chrome", {"version": "2.0-1"}, {"action": "approve", "app": "chrome"})
        self.st.set_approved("gimp", {"version": "1"}, {"action": "approve", "app": "gimp"})
        self.assertEqual(sorted(self.st.approved()), ["chrome", "gimp"])
        with open(os.path.join(self.root, "approvals.log"), encoding="utf-8") as f:
            lines = [json.loads(line) for line in f]
        self.assertEqual([x["app"] for x in lines], ["chrome", "gimp"])

    def test_cache_dir_and_inspect_dir(self):
        self.assertTrue(os.path.isdir(self.st.cache_dir("milkyway-cache")))
        with self.assertRaises(state.StateError):
            self.st.cache_dir("../evil")
        record = self.st.add_candidate(H.candidate())
        self.assertTrue(self.st.inspect_dir(record).endswith("_aaaaaaaaaaaa.inspect"))


if __name__ == "__main__":
    unittest.main()
