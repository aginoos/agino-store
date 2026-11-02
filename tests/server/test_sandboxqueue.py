# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import stat
import tempfile
import time
import unittest
from unittest import mock

from agino_store_server import content, sandboxqueue
from agino_store_server.state import State

from . import helpers as H

DEB = b"deb-bytes!\n"   # 11 bytes, like H.candidate's size


class TestSandboxQueue(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = self._tmp.name
        self.queue = os.path.join(base, "queue")
        self.cfg = H.make_config(base, sandbox_queue=self.queue)
        H.make_content(self.cfg.content)
        H.edit_yaml(os.path.join(self.cfg.content, "apps", "chrome", "app.yaml"),
                    lambda d: dict(d, launchable="google-chrome.desktop"))
        self.st = State(self.cfg.state)

    def add(self, version="2.0-1", sha="a" * 64, status="pending", sandbox="missing", download=True):
        record = H.candidate(version=version, sha=sha, status=status,
                             checks={"deps": {"ok": True}, "sandbox": {"status": sandbox}})
        if download:
            H.write(os.path.join(self.cfg.downloads, record["file"]), DEB)
        return self.st.add_candidate(record)

    def run_queue(self):
        return sandboxqueue.run(self.cfg, self.st, content.load(self.cfg.content))

    def names(self):
        return sorted(os.listdir(self.queue)) if os.path.isdir(self.queue) else []

    def doc(self, name="chrome_aaaaaaaaaaaa.json"):
        with open(os.path.join(self.queue, name), encoding="utf-8") as f:
            return json.load(f)

    def test_pending_missing_is_queued(self):
        self.add()
        messages = self.run_queue()
        self.assertEqual(self.names(), ["chrome_aaaaaaaaaaaa.deb", "chrome_aaaaaaaaaaaa.json"])
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith("queued chrome 2.0-1"))
        with open(os.path.join(self.queue, "chrome_aaaaaaaaaaaa.deb"), "rb") as f:
            self.assertEqual(f.read(), DEB)
        for name in self.names():
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.queue, name)).st_mode), 0o640)

    def test_json_content(self):
        self.add()
        self.run_queue()
        doc = self.doc()
        queued = doc.pop("queued")
        self.assertRegex(queued, r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
        self.assertEqual(doc["launchable"], "google-chrome.desktop")
        self.assertEqual(doc["install_profile"]["debconf"], ["code code/add-microsoft-repo boolean false"])
        self.assertEqual(doc["install_profile"]["default_files"][0]["path"], "/etc/default/google-chrome")
        self.assertEqual({k: v for k, v in doc.items() if k not in ("launchable", "install_profile")},
                         {"schema": 1, "app": "chrome", "version": "2.0-1", "sha256": "a" * 64, "size": 11,
                          "package": "google-chrome-stable", "deb": "chrome_aaaaaaaaaaaa.deb",
                          "filename": "google-chrome-stable_2.0-1_amd64.deb"})

    def test_null_launchable_and_profile(self):
        record = H.candidate(app="gimp", version="3", sha="b" * 64,
                             checks={"deps": {"ok": True}, "sandbox": {"status": "missing"}})
        H.write(os.path.join(self.cfg.downloads, record["file"]), DEB)
        self.st.add_candidate(record)
        self.run_queue()
        doc = self.doc("gimp_bbbbbbbbbbbb.json")
        self.assertEqual(doc["launchable"], "gimp.desktop")
        self.assertIsNone(doc["install_profile"])

    def test_other_states_are_skipped(self):
        self.add(version="1", sha="1" * 64, status="approved")
        self.add(version="2", sha="2" * 64, status="rejected")
        self.add(version="3", sha="3" * 64, sandbox="pass")
        self.add(version="4", sha="4" * 64, sandbox="fail")
        self.add(version="5", sha="5" * 64, download=False)
        messages = self.run_queue()
        # A pending candidate whose download is gone is reported, not silently dropped.
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith("skipped chrome 5"))
        self.assertEqual(self.names(), [])

    def test_failure_on_one_entry_does_not_stop_others(self):
        self.add(version="5", sha="5" * 64)
        self.add(version="6", sha="6" * 64)
        os.remove(os.path.join(self.cfg.downloads, "%s/google-chrome-stable_5_amd64.deb" % ("5" * 64)))
        messages = self.run_queue()
        self.assertEqual(self.names(), ["chrome_666666666666.deb", "chrome_666666666666.json"])
        self.assertTrue(any(m.startswith("skipped chrome 5") for m in messages))

    def test_stale_entries_are_removed(self):
        record = self.add()
        self.run_queue()
        self.st.update_candidate(record, {"status": "approved"})
        messages = self.run_queue()
        self.assertEqual(self.names(), [])
        self.assertEqual(sorted(messages), ["removed chrome_aaaaaaaaaaaa.deb", "removed chrome_aaaaaaaaaaaa.json"])

    def test_sandbox_result_removes_entry(self):
        record = self.add()
        self.run_queue()
        self.st.update_candidate(record, {"checks": {"sandbox": {"status": "pass"}}})
        self.run_queue()
        self.assertEqual(self.names(), [])

    def test_unrelated_files_and_symlinks_are_left_alone(self):
        self.add()
        self.run_queue()
        outside = os.path.join(self._tmp.name, "outside")
        H.write(outside, b"keep")
        for name in ("notes.txt", "Chrome_aaaaaaaaaaaa.deb", "chrome_aaaa.json", "x_0123456789ab.json.sig"):
            H.write(os.path.join(self.queue, name), b"x")
        os.symlink(outside, os.path.join(self.queue, "other_0123456789ab.json"))
        os.symlink(outside, os.path.join(self.queue, "chrome_bbbbbbbbbbbb.deb"))
        self.run_queue()
        names = self.names()
        for name in ("notes.txt", "Chrome_aaaaaaaaaaaa.deb", "chrome_aaaa.json", "x_0123456789ab.json.sig"):
            self.assertIn(name, names)
        self.assertNotIn("other_0123456789ab.json", names)   # exact name, so the link itself goes
        self.assertNotIn("chrome_bbbbbbbbbbbb.deb", names)
        with open(outside, "rb") as f:
            self.assertEqual(f.read(), b"keep")

    def test_symlink_at_wanted_name_is_not_followed(self):
        self.add()
        outside = os.path.join(self._tmp.name, "outside")
        H.write(outside, b"keep")
        os.makedirs(self.queue)
        os.symlink(outside, os.path.join(self.queue, "chrome_aaaaaaaaaaaa.deb"))
        self.run_queue()
        with open(outside, "rb") as f:
            self.assertEqual(f.read(), b"keep")
        self.assertFalse(os.path.islink(os.path.join(self.queue, "chrome_aaaaaaaaaaaa.deb")))

    def test_queue_dir_symlink_is_refused(self):
        self.add()
        target = os.path.join(self._tmp.name, "elsewhere")
        os.makedirs(target)
        os.symlink(target, self.queue)
        with self.assertRaises(sandboxqueue.QueueError):
            self.run_queue()
        self.assertEqual(os.listdir(target), [])

    def test_second_run_is_idempotent(self):
        self.add()
        self.run_queue()
        before = {n: os.stat(os.path.join(self.queue, n)).st_ino for n in self.names()}
        first = self.doc()
        with mock.patch("agino_store_server.sandboxqueue.catalogwriter.now", side_effect=AssertionError):
            self.assertEqual(self.run_queue(), [])
        self.assertEqual({n: os.stat(os.path.join(self.queue, n)).st_ino for n in self.names()}, before)
        self.assertEqual(self.doc(), first)

    def test_changed_content_rewrites_only_json(self):
        self.add()
        self.run_queue()
        deb_ino = os.stat(os.path.join(self.queue, "chrome_aaaaaaaaaaaa.deb")).st_ino
        H.edit_yaml(os.path.join(self.cfg.content, "apps", "chrome", "app.yaml"),
                    lambda d: dict(d, launchable="chrome2.desktop"))
        messages = self.run_queue()
        self.assertTrue(messages[0].startswith("updated chrome 2.0-1"))
        self.assertEqual(self.doc()["launchable"], "chrome2.desktop")
        self.assertEqual(os.stat(os.path.join(self.queue, "chrome_aaaaaaaaaaaa.deb")).st_ino, deb_ino)

    def test_json_is_written_after_deb(self):
        self.add()
        order = []
        real = os.rename

        def spy(src, dst, **kw):
            order.append(dst)
            return real(src, dst, **kw)

        with mock.patch("agino_store_server.sandboxqueue.os.rename", side_effect=spy):
            self.run_queue()
        self.assertEqual(order, ["chrome_aaaaaaaaaaaa.deb", "chrome_aaaaaaaaaaaa.json"])
        self.assertFalse([n for n in self.names() if n.startswith(".tmp")])

    def test_failed_copy_leaves_no_json_or_temp(self):
        self.add()
        with mock.patch("agino_store_server.sandboxqueue._copy", side_effect=OSError(28, "No space")):
            messages = self.run_queue()
        self.assertTrue(messages[0].startswith("skipped chrome 2.0-1"))
        self.assertEqual(self.names(), [])

    def test_old_temp_files_are_cleaned(self):
        self.add()
        self.run_queue()
        old = os.path.join(self.queue, ".tmp-sbxq-1-old")
        new = os.path.join(self.queue, ".tmp-sbxq-1-new")
        other = os.path.join(self.queue, ".tmp-other")
        for path in (old, new, other):
            H.write(path, b"x")
        past = time.time() - 7200
        os.utime(old, (past, past))
        os.utime(other, (past, past))
        self.run_queue()
        self.assertEqual([n for n in self.names() if n.startswith(".")], [".tmp-other", ".tmp-sbxq-1-new"])


if __name__ == "__main__":
    unittest.main()
