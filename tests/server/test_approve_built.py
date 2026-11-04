# SPDX-License-Identifier: GPL-3.0-or-later
import unittest

from agino_store_server import approve_rules

from . import helpers as H
from .test_cli import CliCase

ORIGIN = {"type": "built", "source_url": "https://github.com/example/tool.git", "source_ref": "v1.0.0",
          "source_commit": "c" * 40, "build_job": "0123456789abcdef"}


def checks(sandbox="pass", scripts_changed=(), flags=(), flags_added=()):
    return {"deps": {"ok": True, "detail": ""}, "sandbox": {"status": sandbox},
            "inspect": {"scripts": {}, "flags": list(flags)},
            "diff": {"against": None, "scripts_changed": list(scripts_changed), "flags_added": list(flags_added),
                     "summary": "x"}}


class BuiltCase(CliCase):
    def add_built(self, version="1.0.0+agino1", char="b", **kw):
        return self.add(version=version, char=char, app="tool", origin=dict(ORIGIN),
                        checks=kw.pop("checks", checks()), **kw)

    def approve(self, *argv):
        return self.run_cli("approve", "tool", *argv)


class TestSingle(BuiltCase):
    def test_a_clean_candidate_with_a_note_is_approved(self):
        self.add_built()
        code, out, err = self.approve("--note", "reviewed the diff and the build log")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.st.approved()["tool"]["note"], "reviewed the diff and the build log")
        line = self.log_lines()[-1]
        self.assertEqual((line["origin"], line["source_commit"], line["build_job"]),
                         ("built", "c" * 40, "0123456789abcdef"))

    def test_other_candidates_do_not_get_the_origin_fields(self):
        self.add(app="chrome")
        self.assertEqual(self.run_cli("approve", "chrome")[0], 0)
        self.assertNotIn("origin", self.log_lines()[-1])

    def test_a_note_is_required(self):
        self.add_built()
        for argv in ((), ("--note", ""), ("--note", "   ")):
            with self.subTest(argv=argv):
                code, _, err = self.approve(*argv)
                self.assertEqual(code, 1)
                self.assertIn("needs --note", err)
        self.assertEqual(self.status("1.0.0+agino1", "b", "tool"), "pending")

    def test_the_sandbox_must_pass(self):
        for sandbox in ("missing", "fail"):
            with self.subTest(sandbox=sandbox):
                self.add_built(checks=checks(sandbox=sandbox), version="1.0.%d+agino1" % len(sandbox))
        code, _, err = self.approve("--note", "x")
        self.assertEqual(code, 1)
        self.assertIn("sandbox is", err)
        self.assertNotIn("--no-sandbox to override", err)

    def test_no_sandbox_is_refused(self):
        self.add_built(checks=checks(sandbox="missing"))
        code, _, err = self.approve("--note", "x", "--no-sandbox")
        self.assertEqual(code, 1)
        self.assertIn("--no-sandbox is not allowed", err)
        self.assertIn("passing sandbox report", err)

    def test_no_sandbox_is_refused_even_when_the_sandbox_passed(self):
        self.add_built()
        code, _, err = self.approve("--note", "x", "--no-sandbox")
        self.assertEqual(code, 1)
        self.assertIn("--no-sandbox is not allowed", err)

    def test_flagged_candidates_need_include_flagged(self):
        cases = [checks(scripts_changed=["postinst"]), checks(flags=["setuid x"]), checks(flags_added=["systemd unit"])]
        for i, flagged in enumerate(cases):
            with self.subTest(case=i):
                self.add_built(checks=flagged, version="1.0.%d+agino1" % i, char="d")
                code, _, err = self.approve("--note", "x")
                self.assertEqual(code, 1)
                self.assertIn("--include-flagged", err)
                self.assertEqual(self.approve("--note", "x", "--include-flagged")[0], 0)
                self.st.remove_approved("tool", {"action": "revoke"})

    def test_all_the_blockers_are_listed_together(self):
        self.add_built(checks=checks(sandbox="fail", scripts_changed=["postinst"]))
        _, _, err = self.approve()
        for needle in ("sandbox is fail", "needs --note", "--include-flagged"):
            self.assertIn(needle, err)


class TestAll(BuiltCase):
    def test_all_skips_a_built_candidate_without_a_note(self):
        self.add_built()
        self.add(app="chrome")
        code, out, _ = self.run_cli("approve", "all")
        self.assertEqual(code, 1)
        self.assertIn("skipped tool 1.0.0+agino1", out)
        self.assertIn("needs --note", out)
        self.assertIn("approved chrome", out)

    def test_all_approves_it_with_a_note_and_flags_confirmed(self):
        self.add_built(checks=checks(scripts_changed=["postinst"]))
        code, out, _ = self.run_cli("approve", "all", "--note", "reviewed", "--include-flagged")
        self.assertEqual((code, out.strip()), (0, "approved tool 1.0.0+agino1"))

    def test_all_never_uses_no_sandbox_for_built(self):
        self.add_built(checks=checks(sandbox="missing"))
        code, out, _ = self.run_cli("approve", "all", "--note", "x", "--no-sandbox")
        self.assertEqual(code, 1)
        self.assertIn("--no-sandbox is not allowed", out)


class TestRules(unittest.TestCase):
    def test_only_built_candidates_are_restricted(self):
        record = H.candidate(origin={"type": "apt"})
        self.assertEqual(approve_rules.built_blockers(record, True, "", False, {"flags": ["x"]}), [])
        self.assertFalse(approve_rules.is_built({}))
        self.assertFalse(approve_rules.is_built({"origin": None}))
        self.assertTrue(approve_rules.is_built({"origin": {"type": "built"}}))


if __name__ == "__main__":
    unittest.main()
