# SPDX-License-Identifier: GPL-3.0-or-later
"""content-edit / content-confirm after an interrupted run: leftovers in the content checkout are repaired."""

import os
import signal
import subprocess
import time
import unittest
from unittest import mock

from agino_store_server import content_edit, content_git, content_repair

from . import helpers as H
from .edit_helpers import NEW_TEXT, EditCase, git, slurp

OLD = 20 * 60   # seconds; older than the 10 minute threshold


def git_may_fail(cwd, *args):
    return subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True)


class RepairCase(EditCase):
    def gitdir(self, *parts):
        return os.path.join(self.content, ".git", *parts)

    def lock(self, age):
        path = self.gitdir("index.lock")
        H.write(path, b"")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def conflict_with_side_branch(self, operation):
        """main (pushed) and a side branch change the same line; `operation` leaves git half way."""
        base = self.head()
        self.set_app(lambda d: dict(d, license="MIT"))
        git(self.content, "push", "origin", "main")
        git(self.content, "checkout", "-q", "-b", "side", base)
        H.edit_yaml(self.gimp, lambda d: dict(d, license="BSD-3-Clause"))
        self.commit_all("side change")
        git(self.content, "checkout", "-q", "main")
        self.assertNotEqual(git_may_fail(self.content, *operation).returncode, 0)


class TestUnfinishedOperations(RepairCase):
    def test_unfinished_rebase_is_aborted(self):
        self.conflict_with_side_branch(["rebase", "side"])
        self.assertTrue(os.path.isdir(self.gitdir("rebase-merge")) or os.path.isdir(self.gitdir("rebase-apply")))
        code, out, err = self.confirm("chrome")
        self.assertEqual((code, out.strip().splitlines()[-1]), (0, "confirmed the description of chrome"))
        self.assertIn("aborting an unfinished rebase", err)
        self.assertFalse(os.path.exists(self.gitdir("rebase-merge")) or os.path.exists(self.gitdir("rebase-apply")))
        self.assertEqual(git(self.content, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(self.app_data()["license"], "MIT")
        self.assertEqual(self.origin_head(), self.head())

    def test_unfinished_merge_is_aborted(self):
        self.conflict_with_side_branch(["merge", "--no-edit", "side"])
        self.assertTrue(os.path.exists(self.gitdir("MERGE_HEAD")))
        code, _, err = self.confirm("chrome")
        self.assertEqual(code, 0)
        self.assertIn("aborting an unfinished merge", err)
        self.assertFalse(os.path.exists(self.gitdir("MERGE_HEAD")))
        self.assertEqual(self.origin_head(), self.head())

    def test_another_git_process_blocks_the_repair(self):
        self.conflict_with_side_branch(["rebase", "side"])
        with mock.patch.object(content_repair, "busy_git_process", return_value=4242):
            code, _, err = self.confirm("chrome")
        self.assertEqual(code, 1)
        self.assertIn("another git process (pid 4242)", err)
        self.assertTrue(os.path.isdir(self.gitdir("rebase-merge")) or os.path.isdir(self.gitdir("rebase-apply")))

    def test_busy_git_process_is_found_by_its_working_directory(self):
        child = subprocess.Popen(["git", "cat-file", "--batch"], cwd=self.content, stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL)
        self.addCleanup(child.wait)
        self.addCleanup(child.stdin.close)
        deadline = time.monotonic() + 5
        found = None
        while found is None and time.monotonic() < deadline:
            found = content_repair.busy_git_process(self.content)
            time.sleep(0.05)
        self.assertEqual(found, child.pid)
        child.stdin.close()
        child.wait()
        self.assertIsNone(content_repair.busy_git_process(self.content))


class TestIndexLock(RepairCase):
    def test_stale_lock_is_removed(self):
        lock = self.lock(OLD)
        code, _, err = self.edit(NEW_TEXT)
        self.assertEqual(code, 0)
        self.assertIn("removed a stale .git/index.lock", err)
        self.assertFalse(os.path.exists(lock))
        self.assertEqual(self.origin_head(), self.head())

    def test_fresh_lock_is_left_alone(self):
        lock = self.lock(30)
        before = slurp(self.gimp)
        code, _, err = self.edit(NEW_TEXT)
        self.assertEqual(code, 1)
        self.assertIn("index.lock", err)
        self.assertTrue(os.path.exists(lock))
        self.assertEqual(slurp(self.gimp), before)

    def test_stale_lock_of_a_running_git_is_not_removed(self):
        lock = self.lock(OLD)
        with mock.patch.object(content_repair, "busy_git_process", return_value=77):
            code, _, err = self.edit(NEW_TEXT)
        self.assertEqual(code, 1)
        self.assertIn("pid 77", err)
        self.assertTrue(os.path.exists(lock))


class TestUncommittedAndUnpushed(RepairCase):
    def test_uncommitted_change_under_apps_is_logged_and_discarded(self):
        with open(self.gimp, "a", encoding="utf-8") as f:
            f.write("# hand edit\n")
        code, _, err = self.edit({"license": "MIT"})
        self.assertEqual(code, 0)
        self.assertIn("discarded uncommitted changes under apps/", err)
        self.assertIn("+# hand edit", err)
        self.assertNotIn("# hand edit", slurp(self.gimp).decode())
        folder = os.path.join(self.cfg.state, content_repair.RECOVERED_DIR)
        saved = [slurp(os.path.join(folder, n)).decode() for n in os.listdir(folder)]
        self.assertEqual(len(saved), 1)
        self.assertIn("+# hand edit", saved[0])

    def test_unpushed_commit_is_pushed_first(self):
        self.set_content("no")
        self.assertEqual(self.edit(NEW_TEXT)[0], 0)
        self.assertNotEqual(self.origin_head(), self.head())
        self.set_content("yes")
        code, _, err = self.confirm("chrome")
        self.assertEqual(code, 0)
        self.assertIn("1 local commit(s) were never pushed", err)
        self.assertEqual(self.origin_head(), self.head())
        self.assertEqual(git(self.origin, "log", "--format=%s", "-2", "main"),
                         "info(chrome): confirm description by alice\ninfo(gimp): summary, description by alice")

    def test_unpushed_commit_that_cannot_be_pushed_is_kept(self):
        self.set_content("no")
        self.assertEqual(self.edit(NEW_TEXT)[0], 0)
        kept = self.head()
        self.set_content("yes")
        git(self.content, "remote", "set-url", "origin", os.path.join(os.path.dirname(self.origin), "missing.git"))
        code, _, err = self.confirm("chrome")
        self.assertEqual(code, 1)
        self.assertIn("not on origin", err)
        self.assertIn("kept", err)
        self.assertEqual(self.head(), kept)

    def test_push_off_never_pushes_old_commits(self):
        self.set_content("no")
        self.assertEqual(self.edit(NEW_TEXT)[0], 0)
        origin = self.origin_head()
        self.assertEqual(self.confirm("chrome")[0], 0)
        self.assertEqual(self.origin_head(), origin)


class TestSignals(RepairCase):
    def test_sigterm_during_the_push_rolls_everything_back(self):
        before, original = self.head(), slurp(self.gimp)
        saved = signal.getsignal(signal.SIGTERM)

        def killed(*_a, **_k):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(1)   # the handler raises before this returns

        with mock.patch.object(content_git, "push_retry", killed):
            code, _, err = self.edit(NEW_TEXT)
        self.assertEqual(code, 1)
        self.assertIn("interrupted by SIGTERM", err)
        self.assertEqual((self.head(), slurp(self.gimp)), (before, original))
        self.assertEqual(git(self.content, "status", "--porcelain"), "")
        self.assertTrue(os.path.exists(self.staged("patch-0001.json")))
        self.assertEqual(signal.getsignal(signal.SIGTERM), saved)

    def test_sigint_before_the_commit_restores_the_file(self):
        before, original = self.head(), slurp(self.gimp)

        def interrupted(*_a, **_k):
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(1)

        with mock.patch.object(content_git, "commit", interrupted):
            code, _, err = self.confirm()
        self.assertEqual(code, 1)
        self.assertIn("interrupted by SIGINT", err)
        self.assertEqual((self.head(), slurp(self.gimp)), (before, original))
        self.assertEqual(git(self.content, "status", "--porcelain"), "")

    def test_interruption_clears_a_lock_left_by_the_killed_git(self):
        def killed(*_a, **_k):
            H.write(self.gitdir("index.lock"), b"")   # what a killed `git pull` leaves behind
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(1)

        with mock.patch.object(content_git, "push_retry", killed):
            self.assertEqual(self.edit(NEW_TEXT)[0], 1)
        self.assertFalse(os.path.exists(self.gitdir("index.lock")))
        self.assertEqual(self.confirm()[0], 0)   # the next command works at once

    def test_handlers_are_restored_and_ignored_while_rolling_back(self):
        old = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(content_edit.Interrupted):
            with content_edit.interruptible():
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(1)
        self.assertEqual(signal.getsignal(signal.SIGTERM), old)


if __name__ == "__main__":
    unittest.main()
