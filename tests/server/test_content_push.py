# SPDX-License-Identifier: GPL-3.0-or-later
import os
import stat
import unittest
from unittest import mock

from agino_store_server import config, content_git

from . import helpers as H
from .edit_helpers import NEW_TEXT, EditCase, git, slurp


class TestPush(EditCase):
    def test_push_success_to_the_bare_origin(self):
        code, out, err = self.edit(NEW_TEXT)
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("not pushed", out)
        self.assertEqual(self.origin_head(), self.head())
        self.assertEqual(git(self.origin, "log", "-1", "--format=%s", "main"), "info(gimp): summary, description by alice")

    def test_rejected_push_gets_one_rebase_and_succeeds(self):
        other = self.push_from_other()
        self.assertNotEqual(self.head(), self.origin_head())
        code, out, err = self.edit(NEW_TEXT)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.origin_head(), self.head())
        self.assertEqual(git(self.content, "log", "--format=%s", "-2"), "info(gimp): summary, description by alice\nother change")
        self.assertIn("# other", slurp(os.path.join(self.content, "apps", "chrome", "app.yaml")).decode())
        self.assertEqual(self.app_data()["summary"], NEW_TEXT["summary"])
        git(other, "pull", "--ff-only")
        self.assertEqual(self.head(other), self.head())

    def test_push_failing_twice_undoes_the_local_commit(self):
        original, before = slurp(self.gimp), self.head()
        git(self.content, "remote", "set-url", "origin", os.path.join(os.path.dirname(self.origin), "missing.git"))
        code, out, err = self.edit(NEW_TEXT)
        self.assertEqual(code, 1)
        self.assertIn("undone", err)
        self.assertEqual((self.head(), slurp(self.gimp)), (before, original))
        self.assertEqual(git(self.content, "status", "--porcelain"), "")
        self.assertTrue(os.path.exists(self.staged("patch-0001.json")))

    def test_rebased_commit_is_dropped_but_fetched_commits_are_kept(self):
        original = slurp(self.gimp)
        self.push_from_other()
        hook = os.path.join(self.origin, "hooks", "pre-receive")
        H.write(hook, b"#!/bin/sh\necho no >&2\nexit 1\n")
        os.chmod(hook, os.stat(hook).st_mode | stat.S_IXUSR)
        code, _, err = self.edit(NEW_TEXT)
        self.assertEqual(code, 1)
        self.assertIn("undone", err)
        self.assertEqual(self.subject(), "other change")
        self.assertEqual(self.head(), self.origin_head())
        self.assertEqual(slurp(self.gimp), original)

    def test_push_off_commits_locally_with_a_warning(self):
        self.set_content("no")
        origin_before = self.origin_head()
        code, out, _ = self.edit(NEW_TEXT)
        self.assertEqual(code, 0)
        self.assertIn("warning: not pushed (content.push is off)", out)
        self.assertEqual(self.origin_head(), origin_before)
        self.assertEqual(self.subject(), "info(gimp): summary, description by alice")

    def test_push_is_off_by_default(self):
        self.set_content()
        self.assertFalse(config.load(self.conf).content_push)
        code, out, _ = self.confirm("chrome")
        self.assertEqual(code, 0)
        self.assertIn("not pushed", out)

    def test_ssh_command_reaches_git(self):
        self.set_content("yes", "ssh -i /k -o BatchMode=yes")
        self.assertEqual(config.load(self.conf).content_push_ssh_command, "ssh -i /k -o BatchMode=yes")
        seen = []
        real = content_git.run

        def spy(root, args, ssh_command="", check=True):
            seen.append((args[0], ssh_command))
            return real(root, args, "", check)
        with mock.patch.object(content_git, "run", spy):
            self.assertEqual(self.confirm()[0], 0)
        self.assertIn(("push", "ssh -i /k -o BatchMode=yes"), seen)
        self.assertIn(("commit", ""), seen)

    def test_bad_push_value_is_a_configuration_error(self):
        self.set_content("maybe")
        self.assertEqual(self.run_cli("pending")[0], 2)
        with self.assertRaises(config.ConfigError):
            config.load(self.conf)


if __name__ == "__main__":
    unittest.main()
