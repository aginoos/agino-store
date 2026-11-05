# SPDX-License-Identifier: GPL-3.0-or-later
"""approve: the first candidate of an AI-imported vendor application (origin.via == intake) and the applications
whose distribution mode is vendor are held to the strict rules; what `track` finds later is not."""

import types
import unittest
from unittest import mock

from agino_store_server import approve_rules, cli

from . import helpers as H
from .test_approve_built import checks
from .test_cli import CliCase

INTAKE_ORIGIN = {"type": "https", "url": "https://dl.google.com/x.deb", "via": "intake", "intake": "0123456789abcdef"}
TRACK_ORIGIN = {"type": "https", "url": "https://dl.google.com/x.deb"}


class VendorCase(CliCase):
    def add_intake(self, version="2.0-1", char="a", **kw):
        return self.add(version=version, char=char, origin=dict(INTAKE_ORIGIN), checks=kw.pop("checks", checks()), **kw)

    def approve(self, *argv):
        return self.run_cli("approve", "chrome", *argv)


class TestAiImported(VendorCase):
    def test_a_clean_candidate_with_a_note_is_approved_and_audited(self):
        self.add_intake()
        code, _, err = self.approve("--note", "read the postinst, the vendor's own deb")
        self.assertEqual(code, 0, err)
        line = self.log_lines()[-1]
        self.assertEqual((line["origin"], line["via"], line["intake"]), ("https", "intake", "0123456789abcdef"))

    def test_a_note_is_required(self):
        self.add_intake()
        code, _, err = self.approve()
        self.assertEqual(code, 1)
        self.assertIn("imported by the AI needs --note", err)
        self.assertEqual(self.status("2.0-1"), "pending")

    def test_the_sandbox_must_pass_and_no_sandbox_is_refused(self):
        self.add_intake(checks=checks(sandbox="missing"))
        code, _, err = self.approve("--note", "x")
        self.assertEqual(code, 1)
        self.assertIn("sandbox is missing", err)
        self.assertNotIn("--no-sandbox to override", err)
        code, _, err = self.approve("--note", "x", "--no-sandbox")
        self.assertEqual(code, 1)
        self.assertIn("--no-sandbox is not allowed", err)

    def test_flags_need_include_flagged(self):
        self.add_intake(checks=checks(flags=["postinst mentions systemctl enable"]))
        code, _, err = self.approve("--note", "x")
        self.assertEqual(code, 1)
        self.assertIn("--include-flagged", err)
        self.assertEqual(self.approve("--note", "x", "--include-flagged")[0], 0)

    def test_all_skips_it_without_a_note(self):
        self.add_intake()
        code, out, _ = self.run_cli("approve", "all")
        self.assertEqual(code, 1)
        self.assertIn("skipped chrome 2.0-1", out)
        self.assertIn("needs --note", out)

    def test_the_versions_track_finds_later_follow_the_ordinary_vendor_rules(self):
        self.add(version="2.0-1", char="a", origin=dict(TRACK_ORIGIN), checks=checks(sandbox="missing"))
        code, _, err = self.approve("--no-sandbox")
        self.assertEqual(code, 0, err)
        self.assertNotIn("via", self.log_lines()[-1])


class TestVendorHosted(VendorCase):
    def hosted(self):
        return mock.patch.object(cli, "_mode_of", return_value="vendor")

    def test_the_sandbox_must_pass(self):
        self.add(origin=dict(TRACK_ORIGIN), checks=checks(sandbox="missing"))
        with self.hosted():
            code, _, err = self.approve()
        self.assertEqual(code, 1)
        self.assertIn("sandbox is missing", err)
        self.assertNotIn("--no-sandbox to override", err)

    def test_no_sandbox_is_refused(self):
        self.add(origin=dict(TRACK_ORIGIN), checks=checks(sandbox="pass"))
        with self.hosted():
            code, _, err = self.approve("--no-sandbox")
        self.assertEqual(code, 1)
        self.assertIn("--no-sandbox is not allowed", err)

    def test_a_passing_sandbox_is_enough_no_note_no_flag_confirmation(self):
        self.add(origin=dict(TRACK_ORIGIN), checks=checks(flags=["postinst mentions useradd"]))
        with self.hosted():
            code, _, err = self.approve()
        self.assertEqual(code, 0, err)

    def test_all_follows_the_same_rule(self):
        self.add(origin=dict(TRACK_ORIGIN), checks=checks(sandbox="missing"))
        with self.hosted():
            code, out, _ = self.run_cli("approve", "all", "--no-sandbox")
        self.assertEqual(code, 1)
        self.assertIn("skipped chrome 2.0-1", out)

    def test_the_store_mode_of_a_real_application_changes_nothing(self):
        self.add(origin=dict(TRACK_ORIGIN), checks=checks(sandbox="missing"))
        self.assertEqual(self.approve("--no-sandbox")[0], 0)


class TestRules(unittest.TestCase):
    def test_which_candidates_are_strict(self):
        self.assertTrue(approve_rules.is_intake({"origin": {"type": "https", "via": "intake"}}))
        self.assertFalse(approve_rules.is_intake({"origin": {"type": "https"}}))
        self.assertFalse(approve_rules.is_intake({}))
        self.assertTrue(approve_rules.is_strict({"origin": {"type": "built"}}))
        self.assertFalse(approve_rules.is_strict(H.candidate(origin={"type": "apt"})))

    def test_the_mode_is_read_leniently(self):
        mode = approve_rules.distribution_mode
        self.assertEqual(mode(types.SimpleNamespace(distribution={"mode": "vendor"})), "vendor")
        self.assertEqual(mode({"distribution": {"mode": "vendor", "page": "https://x.example/"}}), "vendor")
        self.assertEqual(mode(types.SimpleNamespace(distribution=types.SimpleNamespace(mode="vendor"))), "vendor")
        for app in (types.SimpleNamespace(), types.SimpleNamespace(distribution=None), {},
                    types.SimpleNamespace(distribution={"mode": "bogus"}), {"distribution": "vendor"}):
            self.assertEqual(mode(app), "store")

    def test_blockers_for_a_vendor_hosted_application(self):
        record = H.candidate(origin={"type": "https"})
        self.assertEqual(approve_rules.built_blockers(record, True, "", False, {"flags": ["x"]}, "store"), [])
        text = " ".join(approve_rules.built_blockers(record, True, "", False, {}, "vendor"))
        self.assertIn("--no-sandbox is not allowed", text)
        self.assertEqual(approve_rules.built_blockers(record, False, "", False, {"flags": ["x"]}, "vendor"), [])


if __name__ == "__main__":
    unittest.main()
