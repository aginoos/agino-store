# SPDX-License-Identifier: GPL-3.0-or-later
"""Build tasks with recipe v2: request, source fetch, task.json, result classes, verification and AI tasks."""

import copy
import json
import os
import tarfile
import unittest

from agino_store_server import builds, config
from agino_store_server import content as C

from . import build_helpers as B
from . import helpers as H
from .build_case import DPKG, GIT, BuildCase
from .test_recipe_v2 import GIT as GIT_SOURCE
from .test_recipe_v2 import TARBALL, v2

UPSTREAM_REPO = "https://github.com/example/tool.git"


class V2BuildCase(BuildCase):
    """BuildCase with a v2 recipe (git source + tarball) and the upstream moved to an https address."""

    def setUp(self):
        super().setUp()
        with open(self.conf, "a", encoding="utf-8") as f:
            f.write("[intake]\nauto_repair = no\nproxy_allow_file = %s\n" % os.path.join(os.path.dirname(self.conf), "allow.list"))
        self.cfg = config.load(self.conf)
        self.make_v2()

    def make_v2(self, **changes):
        H.edit_yaml(os.path.join(self.cfg.content, "upstreams", "tool.yaml"), lambda d: dict(d, repo=UPSTREAM_REPO))
        H.edit_yaml(os.path.join(self.cfg.content, "apps", "tool", "build.yaml"),
                    lambda d: v2(d, **dict({"sources": [GIT_SOURCE, TARBALL]}, **changes)))

    def local_source(self, ref="v1.0.0"):
        """Point the git source of the queued task at the local repository (tests have no network)."""
        return [dict(GIT_SOURCE, url="file://" + self.repo, ref=ref)]


class TestRequest(V2BuildCase):
    def test_placeholders_are_replaced_and_the_tracked_source_is_pinned(self):
        self.seed_seen()
        task = self.task(self.request())
        recipe = task["recipe"]
        self.assertNotIn("source", recipe)
        self.assertEqual(recipe["sources"][0], {"name": "src", "kind": "git", "url": UPSTREAM_REPO, "ref": "v1.0.0"})
        self.assertTrue(recipe["sources"][1]["url"].endswith("/v1.0.0/tool_1.0.0.tar.gz"))
        self.assertEqual(task["pins"], {"src": self.commit})
        self.assertEqual(task["mode"], "build")
        self.assertEqual(task["repo"], UPSTREAM_REPO)

    def test_other_git_sources_are_not_pinned_by_the_task(self):
        self.make_v2(sources=[GIT_SOURCE, dict(GIT_SOURCE, name="lib", url="https://github.com/example/lib.git")])
        self.seed_seen()
        self.assertEqual(self.task(self.request())["pins"], {"src": self.commit})

    def test_v1_records_keep_their_shape(self):
        H.write_yaml(os.path.join(self.cfg.content, "apps", "tool", "build.yaml"), copy.deepcopy(B.RECIPE))
        self.seed_seen()
        task = self.task(self.request())
        self.assertNotIn("pins", task)
        self.assertIn("source", task["recipe"])

    def test_a_value_that_breaks_the_ref_is_refused(self):
        self.seed_seen(version="1.0.0~rc1", tag="v1.0.0-rc1")
        code, _, err = self.run_cli("build-request", "tool", "--version", "1.0.0~rc1", "--by", "alice")
        self.assertEqual(code, 1)
        self.assertIn("not valid after substituting", err)
        self.assertEqual(self.st.builds(), [])


@unittest.skipUnless(GIT, "git not available")
class TestFetchV2(V2BuildCase):
    def queued(self, sources):
        self.seed_seen()
        build_id = self.request()
        recipe = dict(self.task(build_id)["recipe"], sources=sources)
        self.set_status(build_id, recipe=recipe)
        return build_id

    def test_sources_become_a_layout_with_one_directory_each(self):
        build_id = self.queued(self.local_source())
        self.assertEqual(self.run_cli("build-step")[0], 0)
        task = self.task(build_id)
        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["fetched"], {"src": {"kind": "git", "commit": self.commit}})
        directory = os.path.join(self.queue, build_id)
        with tarfile.open(os.path.join(directory, "source.tar")) as tar:
            self.assertEqual(sorted(tar.getnames()), ["src", "src/README", "src/src", "src/src/main.c"])

    def test_task_json_carries_the_v2_recipe_with_known_keys_only(self):
        self.make_v2(sources=[GIT_SOURCE], test=["tool --version"], recipe_version=2, derived_from=1,
                     evidence=[{"claim": "license", "url": "https://github.com/example/tool/COPYING"}],
                     files=[{"path": "agino/run.sh", "mode": "0755", "content": "#!/bin/sh\n"}])
        build_id = self.queued(self.local_source())
        self.run_cli("build-step")
        with open(os.path.join(self.queue, build_id, "task.json"), encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(doc["mode"], "build")
        self.assertEqual(set(doc["recipe"]), {"base", "build_depends", "steps", "package", "limits", "sources", "files",
                                              "test", "evidence", "recipe_version", "derived_from"})
        self.assertEqual(doc["recipe"]["sources"][0]["url"], "file://" + self.repo)
        self.assertEqual(doc["recipe"]["files"], [{"path": "agino/run.sh", "mode": "0755", "content": "#!/bin/sh\n"}])
        self.assertEqual((doc["recipe"]["recipe_version"], doc["recipe"]["derived_from"]), (2, 1))
        self.assertNotIn("pins", doc)
        self.assertEqual(doc["tag"], "v1.0.0")

    def test_a_missing_tag_fails_the_task_as_a_recipe_fault(self):
        build_id = self.queued(self.local_source("v9.9.9"))
        self.assertEqual(self.run_cli("build-step")[0], 1)
        task = self.task(build_id)
        self.assertEqual((task["status"], task["class"]), ("failed", "recipe"))
        self.assertIn("remote ref", task["error"])

    def test_a_moved_tag_is_not_a_recipe_fault(self):
        build_id = self.queued(self.local_source())
        self.set_status(build_id, pins={"src": "0" * 40})
        self.run_cli("build-step")
        self.assertEqual(self.task(build_id)["class"], "infra")


class TestResultClass(V2BuildCase):
    def active(self, status="building"):
        self.seed_seen()
        build_id = self.request()
        os.makedirs(os.path.join(self.queue, build_id), exist_ok=True)
        self.set_status(build_id, status=status)
        return build_id

    def failed_with(self, **result):
        build_id = self.active()
        self.write_result(build_id, dict({"ok": False, "error": "step 2 failed", "steps": [], "artifacts": []}, **result))
        self.run_cli("build-step")
        return self.task(build_id)

    def test_every_class_is_read_into_the_record(self):
        for klass in ("recipe", "infra", "timeout", "model"):
            with self.subTest(klass=klass):
                task = self.failed_with(**{"class": klass})
                self.assertEqual((task["status"], task["class"]), ("failed", klass))

    def test_unknown_or_missing_class_is_none(self):
        self.assertIsNone(self.failed_with(**{"class": "bogus"})["class"])

    def test_missing_class_is_none(self):
        self.assertIsNone(self.failed_with()["class"])

    def test_an_unreadable_result_is_an_infra_failure(self):
        build_id = self.active()
        os.makedirs(os.path.join(self.out, build_id))
        H.write(os.path.join(self.out, build_id, "result.json"), b"{not json")
        os.utime(os.path.join(self.out, build_id, "result.json"), (1, 1))
        self.run_cli("build-step")
        self.assertEqual(self.task(build_id)["class"], "infra")

    def test_a_timeout_is_class_timeout(self):
        build_id = self.active("building")
        self.set_status(build_id, started="2020-01-01T00:00:00Z")
        self.assertEqual(self.run_cli("build-step")[0], 1)
        task = self.task(build_id)
        self.assertEqual((task["status"], task["error"], task["class"]), ("failed", "timeout", "timeout"))

    @unittest.skipUnless(DPKG, "dpkg-deb not available")
    def test_success_is_class_ok(self):
        build_id = self.active()
        deb = self.make_deb("1.0.0+agino1")
        self.write_result(build_id, self.result_for(deb, **{"class": "ok"}), deb)
        with self.deps_ok():
            self.assertEqual(self.run_cli("build-step")[0], 0)
        self.assertEqual((self.task(build_id)["status"], self.task(build_id)["class"]), ("done", "ok"))

    @unittest.skipUnless(DPKG, "dpkg-deb not available")
    def test_a_refused_deb_is_a_recipe_failure(self):
        build_id = self.active()
        deb = self.make_deb("9.9.9", package="other-agino")
        self.write_result(build_id, self.result_for(deb), deb)
        with self.deps_ok():
            self.run_cli("build-step")
        self.assertEqual((self.task(build_id)["status"], self.task(build_id)["class"]), ("failed", "recipe"))


@unittest.skipUnless(DPKG, "dpkg-deb not available")
class TestVerificationTask(V2BuildCase):
    def verify_task(self):
        loaded = C.load(self.cfg.content, allow_insecure=True)
        record = builds.build_record(self.st, "tool", None, loaded.recipe("tool"), "1.0.0", "v1.0.0", self.commit, 1,
                                     "alice", "2026-11-04T10:00:00Z", verify=True, intake="0123456789abcdef",
                                     allow_hosts=["dl.vendor.example"])
        record.update(status="building", started="2026-11-04T10:00:00Z")
        self.st.save_build(record)
        os.makedirs(os.path.join(self.queue, record["id"]))
        return record["id"]

    def test_the_repo_of_a_recipe_without_tracking_is_its_first_source(self):
        record = self.st.build(self.verify_task())
        self.assertEqual(record["repo"], UPSTREAM_REPO)
        self.assertTrue(record["verify"])
        self.assertEqual(record["pins"], {})

    def test_the_deb_is_kept_and_no_candidate_is_made(self):
        build_id = self.verify_task()
        deb = self.make_deb("1.0.0+agino1")
        self.write_result(build_id, self.result_for(deb), deb)
        with self.deps_ok():
            self.assertEqual(self.run_cli("build-step")[0], 0)
        task = self.task(build_id)
        self.assertEqual((task["status"], task["class"], task["candidate"]), ("done", "ok", None))
        art = task["artifact"]
        self.assertTrue(os.path.isfile(os.path.join(self.cfg.downloads, art["sha256"], art["file"])))
        self.assertEqual(self.st.candidates(), [])

    def test_a_wrong_package_is_refused(self):
        build_id = self.verify_task()
        deb = self.make_deb("1.0.0+agino1", package="other-agino")
        self.write_result(build_id, self.result_for(deb), deb)
        self.run_cli("build-step")
        self.assertEqual((self.task(build_id)["status"], self.task(build_id)["class"]), ("failed", "recipe"))
        self.assertEqual(self.st.candidates(), [])

    def test_the_extra_hosts_travel_with_the_task(self):
        self.assertEqual(self.st.build(self.verify_task())["allow_hosts"], ["dl.vendor.example"])


class TestAiTask(V2BuildCase):
    def ai_task(self, status="building"):
        record = {"id": self.st.new_build_id(), "app": "intake-01234567", "version": "0", "upstream_version": "0",
                  "tag": None, "commit": None, "attempt": 1, "status": status, "by": "alice",
                  "created": "2026-11-04T10:00:00Z", "started": "2026-11-04T10:00:00Z", "finished": None, "error": "",
                  "artifact": None, "candidate": None, "mode": "ai", "kind": "import", "agent": "claude",
                  "minutes": 120, "intake": "0123456789abcdef", "limits": {"cpu_minutes": 120}, "repo": "https://x",
                  "package": None, "recipe": None, "source": None}
        self.st.save_build(record)
        return record["id"]

    def deliver(self, build_id, files, names=None):
        directory = os.path.join(self.out, build_id, "ai")
        os.makedirs(directory)
        for name, data in files.items():
            H.write(os.path.join(directory, name), data)
        self.write_result(build_id, {"ok": True, "error": "", "steps": [], "artifacts": [],
                                     "files": names if names is not None else sorted(files)})

    def test_delivered_files_are_recorded_without_a_candidate(self):
        build_id = self.ai_task()
        self.deliver(build_id, {"recipe.yaml": b"x: 1\n", "app.yaml": b"y: 2\n", "NOTES.md": b"notes\n"})
        self.assertEqual(self.run_cli("build-step")[0], 0)
        task = self.task(build_id)
        self.assertEqual((task["status"], task["class"], task["candidate"]), ("done", "ok", None))
        self.assertEqual(task["ai_files"], ["NOTES.md", "app.yaml", "recipe.yaml"])
        self.assertEqual(self.st.candidates(), [])

    def test_only_whitelisted_regular_files_count(self):
        build_id = self.ai_task()
        directory = os.path.join(self.out, build_id, "ai")
        os.makedirs(directory)
        H.write(os.path.join(directory, "recipe.yaml"), b"x: 1\n")
        H.write(os.path.join(directory, "evil.sh"), b"rm -rf /\n")
        H.write(os.path.join(directory, "big.yaml"), b"x")
        H.write(os.path.join(directory, "NOTES.md"), b"n" * ((256 << 10) + 1))
        os.symlink("/etc/passwd", os.path.join(directory, "app.yaml"))
        self.write_result(build_id, {"ok": True, "error": "", "steps": [], "artifacts": [],
                                     "files": ["recipe.yaml", "evil.sh", "NOTES.md", "app.yaml", "../x", 5, "gone.yaml",
                                               "recipe.yaml"]})
        self.run_cli("build-step")
        self.assertEqual(self.task(build_id)["ai_files"], ["recipe.yaml"])

    def test_without_a_recipe_the_task_fails(self):
        build_id = self.ai_task()
        self.deliver(build_id, {"NOTES.md": b"nothing\n"})
        self.assertEqual(self.run_cli("build-step")[0], 1)
        task = self.task(build_id)
        self.assertEqual((task["status"], task["class"]), ("failed", "recipe"))
        self.assertIn("no recipe.yaml and no vendor.yaml", task["error"])

    def test_a_vendor_file_instead_of_a_recipe_is_collected(self):
        build_id = self.ai_task()
        self.deliver(build_id, {"vendor.yaml": b"package: tool\n", "app.yaml": b"y: 2\n", "NOTES.md": b"notes\n"})
        self.assertEqual(self.run_cli("build-step")[0], 0)
        task = self.task(build_id)
        self.assertEqual((task["status"], task["class"], task["candidate"]), ("done", "ok", None))
        self.assertEqual(task["ai_files"], ["NOTES.md", "app.yaml", "vendor.yaml"])

    def test_a_model_failure_keeps_its_class(self):
        build_id = self.ai_task()
        self.write_result(build_id, {"ok": False, "error": "proxy_paused", "class": "model", "steps": [],
                                     "artifacts": []})
        self.run_cli("build-step")
        self.assertEqual((self.task(build_id)["status"], self.task(build_id)["class"]), ("failed", "model"))

    def test_the_ai_task_json_has_the_agreed_shape(self):
        build_id = self.ai_task("ready")
        doc = builds.ai_task_document(self.task(build_id))
        self.assertEqual(doc["mode"], "ai")
        self.assertEqual(doc["ai"], {"agent": "claude", "kind": "import", "minutes": 120})
        self.assertEqual(set(doc), {"id", "app", "version", "mode", "upstream_version", "attempt", "by", "created", "ai"})

    def test_ai_tasks_time_out_after_their_own_limit(self):
        build_id = self.ai_task("building")
        self.set_status(build_id, started="2020-01-01T00:00:00Z")
        self.run_cli("build-step")
        self.assertEqual((self.task(build_id)["status"], self.task(build_id)["class"]), ("failed", "timeout"))

    def test_an_ai_task_does_not_block_a_build_request(self):
        self.seed_seen()
        self.ai_task("building")
        self.set_status(self.st.builds()[0]["id"], app="tool", upstream_version="1.0.0")
        self.assertTrue(self.request())
