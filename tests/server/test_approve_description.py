# SPDX-License-Identifier: GPL-3.0-or-later
from . import helpers as H
from .test_cli import CliCase

BLOCKER = "the description is AI-generated and not confirmed; confirm it or pass --description-reviewed"


class TestApproveDescription(CliCase):
    def set_source(self, source, app="chrome"):
        H.edit_yaml("%s/apps/%s/app.yaml" % (self.cfg.content, app), lambda d: dict(d, description_source=source))

    def test_ai_description_blocks_approve(self):
        self.set_source("ai")
        self.add()
        code, _, err = self.run_cli("approve", "chrome")
        self.assertEqual(code, 1)
        self.assertIn(BLOCKER, err)
        self.assertEqual(self.st.approved(), {})
        self.assertEqual(self.status("2.0-1"), "pending")
        self.assertEqual(self.run_cli("approve", "chrome", "--no-sandbox")[0], 1)   # other overrides do not help

    def test_flag_lets_it_through_and_is_logged(self):
        self.set_source("ai")
        self.add()
        code, out, _ = self.run_cli("approve", "chrome", "--description-reviewed")
        self.assertEqual(code, 0)
        self.assertIn("approved chrome 2.0-1", out)
        self.assertEqual(self.st.approved()["chrome"]["version"], "2.0-1")
        self.assertIs(self.log_lines()[-1]["description_reviewed"], True)

    def test_other_sources_are_unaffected(self):
        for source in ("confirmed", "submitter", "official"):
            with self.subTest(source):
                self.set_source(source)
                self.add(version="%d.0-1" % (2 + ["confirmed", "submitter", "official"].index(source)))
                self.assertEqual(self.run_cli("approve", "chrome")[0], 0)
                self.assertIs(self.log_lines()[-1]["description_reviewed"], False)

    def test_approve_all_skips_ai_apps_and_lists_them(self):
        self.set_source("ai")
        self.add()
        self.add(app="gimp", char="b", version="3.0-1")
        code, out, _ = self.run_cli("approve", "all")
        self.assertEqual(code, 1)
        self.assertIn("approved gimp 3.0-1", out)
        self.assertIn("skipped chrome 2.0-1: " + BLOCKER, out)
        self.assertEqual(set(self.st.approved()), {"gimp"})

    def test_approve_all_does_not_take_the_flag(self):
        self.set_source("ai")
        self.add()
        code, _, err = self.run_cli("approve", "all", "--description-reviewed")
        self.assertEqual(code, 2)
        self.assertIn("by name", err)
        self.assertEqual(self.st.approved(), {})

    def test_candidate_for_an_app_missing_from_content_is_not_blocked(self):
        self.add(app="unknown-app")
        self.assertEqual(self.run_cli("approve", "unknown-app")[0], 0)
