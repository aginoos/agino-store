# SPDX-License-Identifier: GPL-3.0-or-later
"""The intake-step state machine: from a submitted address to a recipe waiting for review."""

import json
import os
import time
import unittest
from unittest import mock

from agino_store_server import intake_step

from . import helpers as H
from .build_case import DPKG
from .intake_case import RECIPE, TAGS, IntakeCase, delivery, dump, tags_patch

BAD = dict(RECIPE, test=[])


class FlowCase(IntakeCase):
    def prepared(self, *submit_args):
        """A submitted task that intake-step has turned into an AI task."""
        intake_id = self.submit(*submit_args)
        self.step()
        self.assertEqual(self.istatus(intake_id), "ready")
        return intake_id

    def deliver(self, intake_id, files=None):
        """The agent delivers; build-step collects; intake-step validates."""
        self.run_ai(intake_id, files if files is not None else delivery())
        with tags_patch():
            return self.step()

    def verifying(self):
        intake_id = self.prepared()
        self.deliver(intake_id)
        self.assertEqual(self.istatus(intake_id), "verifying")
        return intake_id

    def ai_json(self, ai_id):
        with open(os.path.join(self.queue, ai_id, "task.json"), encoding="utf-8") as f:
            return json.load(f)


class TestPrepare(FlowCase):
    def test_a_submitted_task_gets_an_ai_task(self):
        intake_id = self.submit()
        out = self.step()
        rec = self.intake(intake_id)
        self.assertEqual((rec["status"], rec["attempt"], rec["agent"]), ("ready", 1, "claude"))
        self.assertTrue(rec["started"])
        self.assertIn("prepared intake %s" % intake_id, out)
        ai = self.st.build(rec["ai_task"])
        self.assertEqual((ai["status"], ai["mode"], ai["limits"]), ("ready", "ai", {"cpu_minutes": 120}))
        self.assertTrue(os.path.isfile(os.path.join(self.queue, ai["id"], "workspace.tar")))

    def test_a_task_stuck_in_fetching_is_retried(self):
        intake_id = self.submit()
        self.st.save_intake(dict(self.intake(intake_id), status="fetching"))
        self.step()
        self.assertEqual(self.istatus(intake_id), "ready")

    def test_the_ai_task_is_running_once_the_runner_started_it(self):
        intake_id = self.prepared()
        self.set_status(self.intake(intake_id)["ai_task"], status="building")
        self.step()
        self.assertEqual(self.istatus(intake_id), "running")

    def test_broken_content_makes_tasks_wait(self):
        intake_id = self.submit()
        H.write(os.path.join(self.cfg.content, "featured.yaml"), "not: a list\n", "w")
        out = self.step(expect=1)
        self.assertIn("store-content is invalid", out)
        self.assertEqual(self.istatus(intake_id), "submitted")
        H.write(os.path.join(self.cfg.content, "featured.yaml"), "[]\n", "w")
        self.step()
        self.assertEqual(self.istatus(intake_id), "ready")

    def test_nothing_to_do(self):
        self.assertEqual(self.step(), "")

    def test_a_second_run_at_the_same_time_does_nothing(self):
        intake_id = self.submit()
        with self.st.lock("intake-step"):
            code, out, _ = self.run_cli("intake-step")
        self.assertEqual((code, out.strip()), (0, "intake-step is already running"))
        self.assertEqual(self.istatus(intake_id), "submitted")

    def test_the_main_lock_is_only_waited_for_a_short_time(self):
        self.submit()
        with mock.patch.object(intake_step, "LOCK_WAIT", 0.3):
            with self.st.lock():
                self.assertEqual(self.run_cli("intake-step")[0], 3)

    def test_an_unexpected_error_fails_only_that_task(self):
        self.submit()
        self.submit("https://vendor.example.com/other")
        real, calls = intake_step.intake_materials.build_inputs, []

        def flaky(cfg, st, record, loaded):
            calls.append(record["id"])
            if len(calls) == 1:
                raise RuntimeError("boom")
            return real(cfg, st, record, loaded)

        with mock.patch.object(intake_step.intake_materials, "build_inputs", side_effect=flaky):
            code, out, _ = self.run_cli("intake-step")
        self.assertEqual(code, 1)
        self.assertIn("internal error: RuntimeError", out)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.istatus(calls[0]), "fetching")      # the next run starts it again
        self.assertEqual(self.istatus(calls[1]), "ready")
        self.step()
        self.assertEqual(self.istatus(calls[0]), "ready")


class TestDelivery(FlowCase):
    def test_a_good_delivery_starts_the_verification_build(self):
        intake_id = self.prepared()
        out = self.deliver(intake_id)
        rec = self.intake(intake_id)
        self.assertEqual((rec["status"], rec["app"], rec["version"], rec["tag"], rec["commit"]),
                         ("verifying", "newtool", "1.2.0", "v1.2.0", TAGS["v1.2.0"]))
        verify = self.st.build(rec["verify_task"])
        for key, value in {"status": "queued", "mode": "build", "verify": True, "intake": intake_id,
                           "app": "newtool", "version": "1.2.0+agino1", "upstream_version": "1.2.0", "tag": "v1.2.0",
                           "by": "alice", "attempt": 1, "allow_hosts": ["vendor.example.com"],
                           "package": "newtool-agino", "pins": {"src": TAGS["v1.2.0"]}}.items():
            self.assertEqual(verify[key], value, key)
        self.assertEqual(verify["recipe"]["sources"][0]["ref"], "v1.2.0")
        self.assertEqual(verify["recipe"]["recipe_version"], 1)
        self.assertEqual(verify["recipe"]["test"], ["newtool --version"])
        self.assertIn("verification build %s" % verify["id"], out)
        self.assertEqual(self.st.candidates(), [])

    def test_the_agents_files_are_kept_for_the_review(self):
        intake_id = self.prepared()
        self.deliver(intake_id)
        saved = os.path.join(self.st.intake_dir(intake_id), "delivery")
        self.assertEqual(sorted(os.listdir(saved)), ["NOTES.md", "app.yaml", "recipe.yaml", "upstream.yaml"])
        with open(os.path.join(saved, "recipe.yaml"), "rb") as f:
            self.assertEqual(f.read(), dump(RECIPE))

    def test_a_bad_delivery_goes_back_to_the_agent(self):
        intake_id = self.prepared()
        first_ai = self.intake(intake_id)["ai_task"]
        out = self.deliver(intake_id, delivery(recipe=BAD))
        rec = self.intake(intake_id)
        self.assertEqual((rec["status"], rec["rounds"], rec["attempt"]), ("ready", 1, 2))
        self.assertNotEqual(rec["ai_task"], first_ai)
        self.assertIn("correction round 1", out)
        files = self.workspace(rec["ai_task"])
        self.assertIn("`test` needs at least one command", files["inputs/validation-error.txt"].decode("utf-8"))
        self.assertEqual(files["inputs/previous/recipe.yaml"], dump(BAD))
        self.assertIn("inputs/page.txt", files)
        self.assertIn("Correction round 1", files["TASK.md"].decode("utf-8"))
        self.assertEqual(self.ai_json(rec["ai_task"])["ai"]["agent"], "claude")
        self.assertEqual(self.st.build(first_ai)["status"], "done")

    def test_three_correction_rounds_then_the_task_fails(self):
        intake_id = self.prepared()
        for round_no in (1, 2, 3):
            self.deliver(intake_id, delivery(recipe=BAD))
            self.assertEqual((self.istatus(intake_id), self.intake(intake_id)["rounds"]), ("ready", round_no))
        self.run_ai(intake_id, delivery(recipe=BAD))
        with tags_patch():
            out = self.step(expect=1)
        rec = self.intake(intake_id)
        self.assertEqual(rec["status"], "failed")
        self.assertIn("not accepted after 3 correction rounds", rec["error"])
        self.assertIn("failed intake %s" % intake_id, out)
        self.assertTrue(rec["finished"])

    def test_the_number_of_rounds_is_configurable(self):
        self.write_conf("[intake]\nmax_rounds = 0\nproxy_allow_file = {allow}\n")
        intake_id = self.prepared()
        self.run_ai(intake_id, delivery(recipe=BAD))
        with tags_patch():
            self.step(expect=1)
        self.assertEqual(self.istatus(intake_id), "failed")

    def test_a_delivery_without_a_recipe_fails_the_ai_task(self):
        intake_id = self.prepared()
        self.run_ai(intake_id, {"NOTES.md": b"could not do it\n"})
        self.step(expect=1)
        rec = self.intake(intake_id)
        self.assertEqual(rec["status"], "failed")
        self.assertIn("the AI task failed", rec["error"])
        self.assertIn("no recipe.yaml", rec["error"])

    def test_an_ai_failure_that_is_not_the_models_ends_the_task(self):
        for klass in ("timeout", "infra", "recipe", None):
            with self.subTest(klass=klass):
                intake_id = self.prepared("https://vendor.example.com/%s" % klass)
                result = {"ok": False, "error": "something broke", "steps": [], "artifacts": []}
                if klass:
                    result["class"] = klass
                self.run_ai(intake_id, result=result)
                self.step(expect=1)
                self.assertEqual(self.istatus(intake_id), "failed")
                self.assertIn("something broke", self.intake(intake_id)["error"])

    def test_a_cancelled_ai_task_cancels_the_intake(self):
        intake_id = self.prepared()
        self.assertEqual(self.run_cli("build-cancel", self.intake(intake_id)["ai_task"], "--by", "bob")[0], 0)
        self.step()
        self.assertEqual(self.istatus(intake_id), "cancelled")

    def test_the_tracking_file_names_a_repository_without_matching_tags(self):
        intake_id = self.prepared()
        with tags_patch() as patched:
            patched.return_value = {}
            self.run_ai(intake_id, delivery())
            self.step()
        rec = self.intake(intake_id)
        self.assertEqual((rec["status"], rec["rounds"]), ("ready", 1))
        self.assertIn("no tag of the repository matches",
                      self.workspace(rec["ai_task"])["inputs/validation-error.txt"].decode("utf-8"))


@unittest.skipUnless(DPKG, "dpkg-deb not available")
class TestVerification(FlowCase):
    def test_a_passing_replay_makes_the_task_ready_for_review(self):
        intake_id = self.verifying()
        verify_id = self.run_verify(intake_id)
        self.assertEqual(self.st.build(verify_id)["status"], "done")
        out = self.step()
        self.assertEqual(self.istatus(intake_id), "review")
        self.assertIn("ready for review", out)
        self.assertEqual(self.st.candidates(), [])

    def test_a_failing_replay_goes_back_to_the_agent_with_the_log(self):
        intake_id = self.verifying()
        first_verify = self.intake(intake_id)["verify_task"]
        self.run_verify(intake_id, ok=False, error="test 1 failed", log=b"newtool: command not found\n\x1b[31mred\n")
        self.step()
        rec = self.intake(intake_id)
        self.assertEqual((rec["status"], rec["rounds"]), ("ready", 1))
        files = self.workspace(rec["ai_task"])
        feedback = files["inputs/replay-failure.log"].decode("utf-8")
        self.assertIn("test 1 failed", feedback)
        self.assertIn("newtool: command not found", feedback)
        self.assertNotIn("\x1b", feedback)
        self.assertEqual(files["inputs/previous/recipe.yaml"], dump(RECIPE))
        self.assertIn("Correction round 1", files["TASK.md"].decode("utf-8"))
        # the corrected delivery gets its own verification build (attempt 2)
        self.deliver(intake_id)
        second = self.st.build(self.intake(intake_id)["verify_task"])
        self.assertNotEqual(second["id"], first_verify)
        self.assertEqual((second["attempt"], second["version"]), (2, "1.2.0+agino2"))

    def test_an_infrastructure_failure_does_not_bother_the_agent(self):
        intake_id = self.verifying()
        self.run_verify(intake_id, ok=False, error="the VM died", klass="infra")
        self.step(expect=1)
        rec = self.intake(intake_id)
        self.assertEqual(rec["status"], "failed")
        self.assertIn("the VM died", rec["error"])

    def test_a_cancelled_verification_cancels_the_task(self):
        intake_id = self.verifying()
        self.assertEqual(self.run_cli("build-cancel", self.intake(intake_id)["verify_task"], "--by", "bob")[0], 0)
        self.step()
        self.assertEqual(self.istatus(intake_id), "cancelled")

    def test_the_wrong_package_in_the_deb_is_a_recipe_failure(self):
        intake_id = self.verifying()
        self.run_verify(intake_id, package="other-agino")
        self.step()
        self.assertEqual((self.istatus(intake_id), self.intake(intake_id)["rounds"]), ("ready", 1))


class TestModelFailures(FlowCase):
    MODEL = {"ok": False, "error": "proxy_paused", "class": "model", "steps": [], "artifacts": []}

    def test_the_task_pauses_and_retries_later(self):
        intake_id = self.prepared()
        first_ai = self.intake(intake_id)["ai_task"]
        self.run_ai(intake_id, result=self.MODEL)
        before = time.time()
        out = self.step()
        rec = self.intake(intake_id)
        self.assertEqual((rec["status"], rec["retries"]), ("paused", 1))
        self.assertIn("model service is unavailable", rec["error"])
        self.assertGreater(intake_step.epoch_of(rec["next_retry"]), before + 4 * 60)
        self.assertLess(intake_step.epoch_of(rec["next_retry"]), before + 6 * 60)
        self.assertIn("paused intake", out)
        self.step()                                   # too early: nothing happens
        self.assertEqual((self.istatus(intake_id), self.intake(intake_id)["ai_task"]), ("paused", first_ai))
        self.st.save_intake(dict(self.intake(intake_id), next_retry="2020-01-01T00:00:00Z"))
        self.step()
        rec = self.intake(intake_id)
        self.assertEqual((rec["status"], rec["agent"], rec["retries"]), ("ready", "claude", 1))
        self.assertNotEqual(rec["ai_task"], first_ai)
        self.assertIn("inputs/page.txt", self.workspace(rec["ai_task"]))

    def test_a_paused_task_does_not_hold_the_proxy_open(self):
        intake_id = self.prepared("https://vendor.example.com/x", "--allow-host", "dl.vendor.example")
        self.run_ai(intake_id, result=self.MODEL)
        self.step()
        with open(self.allow_file, encoding="utf-8") as f:
            self.assertEqual(f.read(), "")

    def test_after_twelve_retries_the_task_fails(self):
        intake_id = self.prepared()
        self.st.save_intake(dict(self.intake(intake_id), retries=12))
        self.run_ai(intake_id, result=self.MODEL)
        self.step(expect=1)
        rec = self.intake(intake_id)
        self.assertEqual(rec["status"], "failed")
        self.assertIn("stayed unavailable", rec["error"])

    def test_the_other_agent_takes_over_when_switching_is_on(self):
        self.write_conf("[intake]\nagents = claude codex\nswitch_agent = yes\nproxy_allow_file = {allow}\n")
        intake_id = self.prepared()
        self.run_ai(intake_id, result=self.MODEL)
        out = self.step()
        rec = self.intake(intake_id)
        self.assertEqual((rec["status"], rec["agent"], rec["agents_tried"]), ("ready", "codex", ["claude", "codex"]))
        self.assertEqual(rec["retries"], 0)
        self.assertEqual(self.ai_json(rec["ai_task"])["ai"]["agent"], "codex")
        self.assertIn("trying codex", out)
        self.run_ai(intake_id, result=self.MODEL)       # codex fails too: nobody left, the task pauses
        self.step()
        self.assertEqual(self.istatus(intake_id), "paused")

    def test_no_switch_when_it_is_off_or_only_one_agent_is_set_up(self):
        for conf in ("[intake]\nagents = claude codex\nswitch_agent = no\nproxy_allow_file = {allow}\n",
                     "[intake]\nagents = claude\nswitch_agent = yes\nproxy_allow_file = {allow}\n"):
            with self.subTest(conf=conf):
                self.write_conf(conf)
                intake_id = self.prepared("https://vendor.example.com/%d" % len(conf))
                self.run_ai(intake_id, result=self.MODEL)
                self.step()
                self.assertEqual((self.istatus(intake_id), self.intake(intake_id)["agent"]), ("paused", "claude"))

    def test_a_correction_keeps_its_feedback_over_a_pause(self):
        intake_id = self.prepared()
        self.deliver(intake_id, delivery(recipe=BAD))
        self.assertEqual(self.intake(intake_id)["rounds"], 1)
        self.run_ai(intake_id, result=self.MODEL)
        self.step()
        self.st.save_intake(dict(self.intake(intake_id), next_retry="2020-01-01T00:00:00Z"))
        self.step()
        files = self.workspace(self.intake(intake_id)["ai_task"])
        self.assertIn("inputs/validation-error.txt", files)
        self.assertIn("inputs/previous/recipe.yaml", files)


class TestHousekeeping(FlowCase):
    def test_working_files_of_old_finished_tasks_are_removed(self):
        old, recent = self.new_task("https://vendor.example.com/a"), self.new_task("https://vendor.example.com/b")
        self.st.save_intake(dict(self.intake(old), status="failed", finished="2020-01-01T00:00:00Z"))
        self.st.save_intake(dict(self.intake(recent), status="failed", finished=intake_step.stamp(time.time())))
        for intake_id in (old, recent):
            os.makedirs(self.st.intake_dir(intake_id, create=True), exist_ok=True)
            H.write(os.path.join(self.st.intake_dir(intake_id), "inputs.tar"), b"x")
        self.step()
        self.assertFalse(os.path.exists(self.st.intake_dir(old)))
        self.assertTrue(os.path.exists(self.st.intake_dir(recent)))
        self.assertEqual(self.istatus(old), "failed")      # the record stays

    def test_open_tasks_keep_their_files(self):
        intake_id = self.prepared()
        self.step()
        self.assertTrue(os.path.isfile(os.path.join(self.st.intake_dir(intake_id), "inputs.tar")))
