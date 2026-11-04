# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import tempfile
import time
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

    def test_version_must_start_with_a_digit(self):
        for bad in ("-rf", "~x", ".1", ":1", "a1", "1:", "1:-x", "1 2", "1/2", "1\n"):
            with self.assertRaises(state.StateError, msg=bad):
                self.st.add_candidate(H.candidate(version=bad))
        self.st.add_candidate(H.candidate(version="1:2.0~rc1+dfsg-1"))

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

    def test_named_lock_is_independent_of_the_main_lock(self):
        with self.st.lock():
            with self.st.lock("build-step"):
                with self.assertRaises(state.StateLocked):
                    with state.State(self.root).lock("build-step"):
                        pass
        with self.assertRaises(state.StateError):
            with self.st.lock("../x"):
                pass

    def test_lock_wait_gives_up(self):
        with self.st.lock():
            start = time.monotonic()
            with self.assertRaises(state.StateLocked):
                with state.State(self.root).lock(wait=0.6):
                    pass
            self.assertGreaterEqual(time.monotonic() - start, 0.5)

    def test_upstream_memo_roundtrip(self):
        self.assertEqual(self.st.upstream_memo("tool"), {})
        self.st.save_upstream_memo("tool", {"latest_version": "1.0.0"})
        self.assertEqual(self.st.upstream_memo("tool"), {"latest_version": "1.0.0"})
        with self.assertRaises(state.StateError):
            self.st.upstream_memo("../x")

    def test_build_records(self):
        self.assertEqual(self.st.builds(), [])
        self.assertIsNone(self.st.build("0123456789abcdef"))
        first, second = self.st.new_build_id(), self.st.new_build_id()
        self.assertRegex(first, r"\A[0-9a-f]{16}\Z")
        self.st.save_build({"id": second, "app": "tool", "created": "2026-11-04T10:00:00Z"})
        self.st.save_build({"id": first, "app": "tool", "created": "2026-11-04T09:00:00Z"})
        self.st.save_build({"id": "f" * 16, "app": "other", "created": "2026-11-04T11:00:00Z"})
        self.assertEqual([r["id"] for r in self.st.builds()], [first, second, "f" * 16])
        self.assertEqual([r["id"] for r in self.st.builds(app="tool")], [first, second])
        self.assertEqual(self.st.build(first)["app"], "tool")
        for bad in ("../x", "short", "G" * 16, None):
            with self.subTest(bad=bad):
                with self.assertRaises(state.StateError):
                    self.st.build(bad)
                with self.assertRaises(state.StateError):
                    self.st.save_build({"id": bad})

    def test_damaged_build_record_is_an_error(self):
        H.write(os.path.join(self.root, "builds", "0123456789abcdef.json"), b"{not json")
        with self.assertRaises(state.StateError):
            self.st.builds()


if __name__ == "__main__":
    unittest.main()
