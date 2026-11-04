# SPDX-License-Identifier: GPL-3.0-or-later
"""intake-adopt: a recipe in review goes into the content repository and the first candidate or a rebuild follows."""

import os
import shutil
import unittest
from unittest import mock

import yaml
from agino_store_server import build_collect, content, trackers

from . import helpers as H
from .build_case import DPKG, GIT
from .intake_case import RECIPE, delivery, dump, git
from .test_intake_flow import FlowCase

EXTRA_HOST = "downloads.vendor.example"
ADDRESS_FOR_EXTRA = "https://vendor.example.com/extra"
EXTRA_RECIPE = dict(RECIPE, sources=RECIPE["sources"] + [
    {"name": "data", "kind": "tarball", "sha256": "a" * 64, "url": "https://%s/pub/newtool-${VERSION}.tar.gz" % EXTRA_HOST}])


def slurp(path):
    with open(path, "rb") as f:
        return f.read()


@unittest.skipUnless(DPKG and GIT, "dpkg-deb and git are needed")
class AdoptCase(FlowCase):
    def in_review(self, *submit_args, files=None):
        intake_id = self.prepared(*submit_args)
        self.deliver(intake_id, files)
        self.assertEqual(self.istatus(intake_id), "verifying")
        self.run_verify(intake_id)
        self.step()
        self.assertEqual(self.istatus(intake_id), "review")
        return intake_id

    def adopt(self, intake_id, *extra, by="bob"):
        with self.deps_ok():
            return self.run_cli("intake-adopt", intake_id, "--by", by, *extra)

    def head(self):
        return git(self.cfg.content, "rev-parse", "HEAD")

    def path(self, *parts):
        return os.path.join(self.cfg.content, *parts)

    def yaml(self, *parts):
        return yaml.safe_load(slurp(self.path(*parts)))

    def clean(self):
        return git(self.cfg.content, "status", "--porcelain")


class TestAdoptImport(AdoptCase):
    def test_the_recipe_and_the_first_candidate(self):
        intake_id = self.in_review()
        before = self.head()
        code, out, err = self.adopt(intake_id, "--auto-task")
        self.assertEqual(code, 0, err)
        # the content repository
        self.assertNotEqual(self.head(), before)
        self.assertEqual(git(self.cfg.content, "log", "-1", "--format=%s"), "intake(newtool): add newtool by bob")
        self.assertEqual(self.clean(), "")
        changed = git(self.cfg.content, "show", "--name-only", "--format=", "HEAD").split("\n")
        self.assertEqual(sorted(changed), ["apps/newtool/app.yaml", "apps/newtool/build.yaml", "apps/newtool/icon.png",
                                           "upstreams/newtool.yaml"])
        app = self.yaml("apps", "newtool", "app.yaml")
        self.assertEqual((app["source"], app["icon"], app["description_source"]), ("personal", "icon.png", "ai"))
        self.assertEqual(app["maintainer"]["name"], "agino app store")
        self.assertTrue(slurp(self.path("apps", "newtool", "icon.png")).startswith(b"\x89PNG"))
        recipe = self.yaml("apps", "newtool", "build.yaml")
        self.assertEqual(recipe["recipe_version"], 1)
        self.assertEqual(recipe["sources"][0]["ref"], "v${VERSION}")       # the placeholders stay in the recipe
        self.assertTrue(self.yaml("upstreams", "newtool.yaml")["auto_task"])
        loaded = content.load(self.cfg.content, allow_insecure=True, policy=self.cfg.build_policy())
        self.assertIsNotNone(loaded.recipe("newtool"))
        self.assertTrue(loaded.upstream("newtool").auto_task)
        # the candidate
        candidates = self.st.candidates(app="newtool")
        self.assertEqual(len(candidates), 1)
        cand = candidates[0]
        rec = self.intake(intake_id)
        self.assertEqual(cand["origin"], {"type": "built", "source_url": "https://github.com/example/newtool.git",
                                          "source_ref": "v1.2.0", "source_commit": "b" * 40,
                                          "build_job": rec["verify_task"]})
        self.assertEqual((cand["status"], cand["version"], cand["package"]), ("pending", "1.2.0+agino1", "newtool-agino"))
        self.assertEqual(rec["status"], "adopted")
        self.assertEqual((rec["adopted_by"], rec["candidate"]), ("bob", os.path.basename(cand["_path"])[:-5]))
        self.assertTrue(rec["finished"])
        self.assertEqual(self.st.build(rec["verify_task"])["candidate"], rec["candidate"])
        self.assertIn("adopted %s" % intake_id, out)
        self.assertIn("candidate %s" % rec["candidate"], out)

    def test_auto_task_is_off_unless_asked_for(self):
        intake_id = self.in_review()
        self.assertEqual(self.adopt(intake_id)[0], 0)
        self.assertFalse(self.yaml("upstreams", "newtool.yaml")["auto_task"])

    def test_the_first_candidate_is_held_to_the_built_rules_and_the_ai_description(self):
        intake_id = self.in_review()
        self.adopt(intake_id)
        code, _, err = self.run_cli("approve", "newtool", "--note", "looks fine")
        self.assertEqual(code, 1)
        self.assertIn("needs a passing sandbox report", err)
        self.assertIn("AI-generated", err)
        self.assertEqual(self.run_cli("approve", "newtool", "--no-sandbox", "--note", "x")[0], 1)

    def test_a_task_that_is_adopted_cannot_be_adopted_again(self):
        intake_id = self.in_review()
        self.adopt(intake_id)
        code, _, err = self.adopt(intake_id)
        self.assertEqual(code, 1)
        self.assertIn("only a task in review", err)

    def test_only_a_task_in_review_can_be_adopted(self):
        intake_id = self.prepared()
        code, _, err = self.adopt(intake_id)
        self.assertEqual(code, 1)
        self.assertIn("is ready", err)

    def test_bad_arguments(self):
        self.assertEqual(self.adopt("0123456789abcdef")[0], 2)
        self.assertEqual(self.adopt("../x")[0], 2)
        intake_id = self.in_review()
        self.assertEqual(self.adopt(intake_id, by="b b")[0], 2)
        self.assertEqual(self.istatus(intake_id), "review")

    def test_the_lock_is_respected(self):
        intake_id = self.in_review()
        with self.st.lock():
            self.assertEqual(self.adopt(intake_id)[0], 3)

    def test_without_the_saved_deb_nothing_is_committed(self):
        intake_id = self.in_review()
        art = self.st.build(self.intake(intake_id)["verify_task"])["artifact"]
        os.unlink(os.path.join(self.cfg.downloads, art["sha256"], art["file"]))
        before = self.head()
        code, _, err = self.adopt(intake_id)
        self.assertEqual(code, 1)
        self.assertIn("missing from the downloads", err)
        self.assertEqual((self.head(), self.istatus(intake_id)), (before, "review"))
        self.assertFalse(os.path.exists(self.path("apps", "newtool")))

    def test_an_id_taken_meanwhile_is_refused(self):
        intake_id = self.in_review()
        shutil.copytree(self.path("apps", "gimp"), self.path("apps", "newtool"))
        before = self.head()
        code, _, err = self.adopt(intake_id)
        self.assertEqual(code, 1)
        self.assertIn("no longer validates", err)
        self.assertIn("already used", err)
        self.assertEqual(self.head(), before)

    def test_a_cancelled_or_rejected_task_cannot_be_adopted(self):
        intake_id = self.in_review()
        self.assertEqual(self.run_cli("intake-cancel", intake_id, "--by", "bob", "--reject")[0], 0)
        self.assertEqual(self.adopt(intake_id)[0], 1)
        self.assertFalse(os.path.exists(self.path("apps", "newtool")))

    def test_hosts_that_only_the_task_allowed_cannot_be_committed(self):
        files = delivery(recipe=EXTRA_RECIPE)
        intake_id = self.in_review(ADDRESS_FOR_EXTRA, "--allow-host", EXTRA_HOST, files=files)
        before = self.head()
        code, _, err = self.adopt(intake_id)
        self.assertEqual(code, 1)
        self.assertIn("allowed_hosts", err)
        self.assertIn("undone", err)
        self.assertEqual((self.head(), self.clean(), self.istatus(intake_id)), (before, "", "review"))
        self.assertFalse(os.path.exists(self.path("apps", "newtool")))
        self.assertFalse(os.path.exists(self.path("upstreams", "newtool.yaml")))
        # the administrator adds the host to [upstream] allowed_hosts and adopts again
        with open(self.conf, encoding="utf-8") as f:
            text = f.read()
        H.write(self.conf, text.replace("\n[intake]", "\n[upstream]\nallowed_hosts = github.com %s\n\n[intake]" % EXTRA_HOST), "w")
        self.assertEqual(self.adopt(intake_id)[0], 0)
        self.assertTrue(os.path.isfile(self.path("apps", "newtool", "build.yaml")))

    def test_a_failed_push_puts_everything_back(self):
        with open(self.conf, encoding="utf-8") as f:
            text = f.read()
        H.write(self.conf, text.replace("\n[intake]", "\n[content]\npush = yes\n\n[intake]"), "w")
        intake_id = self.in_review()
        before = self.head()
        code, _, err = self.adopt(intake_id)
        self.assertEqual(code, 1)
        self.assertNotEqual(err, "")
        self.assertEqual((self.head(), self.clean(), self.istatus(intake_id)), (before, "", "review"))
        self.assertFalse(os.path.exists(self.path("apps", "newtool")))
        self.assertEqual(self.st.candidates(), [])

    def test_a_candidate_that_fails_can_be_retried(self):
        intake_id = self.in_review()
        with mock.patch.object(build_collect, "candidate_from_deb", side_effect=trackers.TrackError("boom")):
            code, out, _ = self.adopt(intake_id)
        self.assertEqual(code, 1)
        self.assertIn("run intake-adopt again", out)
        rec = self.intake(intake_id)
        self.assertEqual((rec["status"], rec["candidate"]), ("adopted", None))
        self.assertIn("no candidate", rec["error"])
        self.assertIn("boom", rec["error"])
        self.assertTrue(os.path.isfile(self.path("apps", "newtool", "app.yaml")))     # the recipe is in
        committed = self.head()
        code, out, err = self.adopt(intake_id)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.head(), committed)                                        # no second commit
        self.assertEqual(len(self.st.candidates(app="newtool")), 1)
        self.assertEqual(self.intake(intake_id)["error"], "")
        self.assertIn("finishing the adoption", out)

    def test_the_review_snapshot_is_what_gets_committed(self):
        intake_id = self.in_review()
        # a later change of the agent's output files must not matter
        ai = self.intake(intake_id)["ai_task"]
        H.write(os.path.join(self.out, ai, "ai", "recipe.yaml"), b"evil: true\n")
        self.assertEqual(self.adopt(intake_id)[0], 0)
        self.assertNotIn("evil", slurp(self.path("apps", "newtool", "build.yaml")).decode("utf-8"))

    def test_adoption_is_audited_by_the_commit_and_the_record(self):
        intake_id = self.in_review()
        self.adopt(intake_id, by="carol")
        self.assertIn("by carol", git(self.cfg.content, "log", "-1", "--format=%s"))
        self.assertEqual(self.intake(intake_id)["adopted_by"], "carol")


@unittest.skipUnless(DPKG and GIT, "dpkg-deb and git are needed")
class TestAdoptRepair(AdoptCase):
    def setUp(self):
        super().setUp()
        self.seed_seen()
        build_id = self.request()
        self.set_status(build_id, status="failed", error="step 2 failed", **{"class": "recipe"})
        self.repair_id = self.run_cli("intake-repair", "tool", "--version", "1.0.0", "--by", "alice")[1].strip()
        self.step()
        self.run_ai(self.repair_id, {"recipe.yaml": dump(dict(RECIPE, package=dict(RECIPE["package"], name="tool-agino"))),
                                     "NOTES.md": b"fixed the build\n",
                                     "app.yaml": slurp(os.path.join(self.cfg.content, "apps", "tool", "app.yaml"))})
        self.step()
        self.assertEqual(self.istatus(self.repair_id), "verifying")
        self.run_verify(self.repair_id, package="tool-agino")
        self.step()
        self.assertEqual(self.istatus(self.repair_id), "review")

    def test_the_new_recipe_is_committed_and_the_version_is_built_again(self):
        before = self.head()
        code, out, err = self.adopt(self.repair_id)
        self.assertEqual(code, 0, err)
        self.assertEqual(git(self.cfg.content, "log", "-1", "--format=%s"), "intake(tool): recipe 2 of tool by bob")
        self.assertEqual(git(self.cfg.content, "show", "--name-only", "--format=", "HEAD"), "apps/tool/build.yaml")
        self.assertNotEqual(self.head(), before)
        recipe = self.yaml("apps", "tool", "build.yaml")
        self.assertEqual((recipe["recipe_version"], recipe["derived_from"]), (2, 1))
        self.assertIn("sources", recipe)
        self.assertNotIn("source", recipe)
        self.assertEqual(self.st.candidates(), [])          # a repair makes no candidate itself
        rebuilt = [b for b in self.st.builds() if b["by"] == "auto"]
        self.assertEqual(len(rebuilt), 1)
        self.assertEqual((rebuilt[0]["status"], rebuilt[0]["app"], rebuilt[0]["upstream_version"]), ("queued", "tool", "1.0.0"))
        self.assertEqual(rebuilt[0]["recipe"]["sources"][0]["ref"], "v1.0.0")
        self.assertIn("build task %s requested" % rebuilt[0]["id"], out)
        rec = self.intake(self.repair_id)
        self.assertEqual((rec["status"], rec["app"], rec["adopted_by"]), ("adopted", "tool", "bob"))

    def test_auto_task_sets_the_flag_of_the_tracked_upstream(self):
        self.assertFalse(self.yaml("upstreams", "tool.yaml")["auto_task"])
        self.assertEqual(self.adopt(self.repair_id, "--auto-task")[0], 0)
        self.assertTrue(self.yaml("upstreams", "tool.yaml")["auto_task"])
        self.assertEqual(git(self.cfg.content, "show", "--name-only", "--format=", "HEAD").split("\n"),
                         ["apps/tool/build.yaml", "upstreams/tool.yaml"])

    def test_an_unfinished_build_of_the_version_only_costs_the_rebuild(self):
        self.request()      # somebody queued the version meanwhile
        code, out, _ = self.adopt(self.repair_id)
        self.assertEqual(code, 0)
        self.assertIn("no new build was requested", out)
        self.assertEqual(self.intake(self.repair_id)["status"], "adopted")
        self.assertEqual(self.yaml("apps", "tool", "build.yaml")["recipe_version"], 2)
