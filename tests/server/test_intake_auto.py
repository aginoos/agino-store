# SPDX-License-Identifier: GPL-3.0-or-later
"""Automatic repair tasks opened by build-step for recipe failures (intake_auto)."""

import os
import unittest

from . import helpers as H
from .build_case import GIT
from .intake_case import IntakeCase


@unittest.skipUnless(GIT, "git not available")
class AutoCase(IntakeCase):
    def failing_build(self, klass="recipe", **result):
        """A build task of tool 1.0.0 that the runner reports as failed with class `klass` (None: no class)."""
        self.seed_seen()
        build_id = self.request()
        os.makedirs(os.path.join(self.queue, build_id), exist_ok=True)
        self.set_status(build_id, status="building", started="2026-11-04T10:00:00Z")
        body = dict({"ok": False, "error": "build step 2 exited with 2", "steps": [], "artifacts": []}, **result)
        if klass:
            body["class"] = klass
        self.write_result(build_id, body)
        return build_id

    def build_step(self):
        code, out, err = self.run_cli("build-step")
        self.assertEqual(code, 1, out + err)        # a failed task is reported with exit code 1
        return out

    def repairs(self):
        return [r for r in self.st.intakes() if r["kind"] == "repair"]


class TestOpening(AutoCase):
    def test_a_recipe_failure_opens_a_repair_task(self):
        build_id = self.failing_build()
        out = self.build_step()
        repairs = self.repairs()
        self.assertEqual(len(repairs), 1)
        rec = repairs[0]
        self.assertEqual((rec["by"], rec["status"], rec["app"], rec["agent"]), ("auto", "submitted", "tool", "claude"))
        self.assertEqual(rec["repair_of"], {"app": "tool", "version": "1.0.0", "failed_task": build_id})
        self.assertEqual(rec["address"], "file://" + self.repo)
        self.assertIn("repair task %s opened for tool 1.0.0" % rec["id"], out)
        self.assertTrue(self.task(build_id)["repair_considered"])

    def test_every_failed_build_is_looked_at_once(self):
        self.failing_build()
        self.build_step()
        self.run_cli("build-step")
        self.assertEqual(len(self.repairs()), 1)

    def test_other_failure_classes_do_not_open_one(self):
        for klass in ("timeout", "infra", "model", None):
            with self.subTest(klass=klass):
                build_id = self.failing_build(klass)
                self.build_step()
                self.assertEqual(self.task(build_id)["status"], "failed")
                self.assertEqual(self.repairs(), [])
                self.set_status(build_id, status="cancelled")

    def test_a_timeout_of_the_server_does_not_open_one(self):
        build_id = self.failing_build()
        os.unlink(os.path.join(self.out, build_id, "result.json"))
        self.set_status(build_id, started="2020-01-01T00:00:00Z")
        self.run_cli("build-step")
        self.assertEqual((self.task(build_id)["class"], self.repairs()), ("timeout", []))

    def test_the_switch(self):
        self.write_conf("[intake]\nauto_repair = no\nproxy_allow_file = {allow}\n")
        build_id = self.failing_build()
        self.build_step()
        self.assertEqual(self.repairs(), [])
        self.assertNotIn("repair_considered", self.task(build_id))

    def test_verification_and_ai_tasks_never_open_one(self):
        build_id = self.failing_build()
        self.set_status(build_id, verify=True, intake="0123456789abcdef")
        self.build_step()
        self.assertEqual(self.repairs(), [])

    def test_an_app_without_a_recipe_gets_nothing(self):
        self.seed_seen()
        record = dict(self.task(self.request()), id=self.st.new_build_id(), app="gimp", status="failed",
                      **{"class": "recipe"})
        self.st.save_build(record)
        self.run_cli("build-step")
        self.assertEqual(self.repairs(), [])

    def test_broken_content_waits_and_nothing_is_lost(self):
        build_id = self.failing_build()
        H.write(os.path.join(self.cfg.content, "featured.yaml"), "not: a list\n", "w")
        out = self.build_step()
        self.assertIn("store-content is invalid", out)
        self.assertEqual(self.repairs(), [])
        self.assertNotIn("repair_considered", self.task(build_id))
        H.write(os.path.join(self.cfg.content, "featured.yaml"), "[]\n", "w")
        self.run_cli("build-step")
        self.assertEqual(len(self.repairs()), 1)

    def test_an_open_manual_repair_is_enough(self):
        self.seed_seen()
        self.assertEqual(self.run_cli("intake-repair", "tool", "--version", "1.0.0", "--by", "alice")[0], 0)
        self.failing_build()
        out = self.build_step()
        self.assertEqual(len(self.repairs()), 1)
        self.assertIn("already an unfinished repair", out)


class TestLimit(AutoCase):
    def test_at_most_two_automatic_repairs_per_version(self):
        for round_no in (1, 2):
            build_id = self.failing_build()
            out = self.build_step()
            self.assertEqual(len(self.repairs()), round_no, out)
            # the repair is over (it failed), the version is built again and fails again
            repair = self.repairs()[-1]
            self.st.save_intake(dict(repair, status="failed", finished="2026-11-04T11:00:00Z"))
            self.set_status(build_id, status="failed")
        build_id = self.failing_build()
        out = self.build_step()
        self.assertEqual(len(self.repairs()), 2)
        self.assertIn("2 automatic repairs were made already", out)
        self.assertTrue(self.task(build_id)["repair_considered"])

    def test_a_manual_repair_does_not_count_and_is_not_limited(self):
        for _ in range(2):
            build_id = self.failing_build()
            self.build_step()
            repair = self.repairs()[-1]
            self.st.save_intake(dict(repair, status="failed", finished="2026-11-04T11:00:00Z"))
            self.set_status(build_id, status="failed")
        self.assertEqual(self.run_cli("intake-repair", "tool", "--version", "1.0.0", "--by", "alice")[0], 0)
        self.assertEqual(len(self.repairs()), 3)

    def test_the_limit_is_configurable(self):
        self.write_conf("[intake]\nmax_auto_repairs = 0\nproxy_allow_file = {allow}\n")
        self.failing_build()
        out = self.build_step()
        self.assertEqual(self.repairs(), [])
        self.assertIn("0 automatic repairs", out)

    def test_other_versions_have_their_own_count(self):
        self.failing_build()
        self.build_step()
        memo = self.memo()
        seen = dict(memo["seen"], **{"1.1.0": {"tag": "v1.1.0", "commit": self.commit, "found": "x"}})
        self.st.save_upstream_memo("tool", dict(memo, seen=seen))
        build_id = self.request("1.1.0")
        os.makedirs(os.path.join(self.queue, build_id), exist_ok=True)
        self.set_status(build_id, status="building", started="2026-11-04T10:00:00Z")
        self.write_result(build_id, {"ok": False, "error": "x", "class": "recipe", "steps": [], "artifacts": []})
        self.build_step()
        self.assertEqual(sorted(r["repair_of"]["version"] for r in self.repairs()), ["1.0.0", "1.1.0"])
