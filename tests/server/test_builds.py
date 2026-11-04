# SPDX-License-Identifier: GPL-3.0-or-later
import datetime
import json
import os
import re
import tarfile
import time
import unittest
from unittest import mock

from agino_store_server import build_step, builds, upstream_fetch

from . import helpers as H
from .build_case import GIT, BuildCase


def stamp(seconds_ago=0):
    when = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=seconds_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestRequest(BuildCase):
    def test_creates_a_queued_task(self):
        self.seed_seen()
        build_id = self.request()
        self.assertRegex(build_id, r"\A[0-9a-f]{16}\Z")
        task = self.task(build_id)
        for key, value in {"app": "tool", "version": "1.0.0+agino1", "upstream_version": "1.0.0", "tag": "v1.0.0",
                           "commit": self.commit, "attempt": 1, "status": "queued", "by": "alice", "started": None,
                           "finished": None, "error": "", "artifact": None, "candidate": None}.items():
            self.assertEqual(task[key], value, key)
        self.assertRegex(task["created"], r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
        self.assertEqual(task["package"], "tool-agino")
        self.assertEqual(task["recipe"]["steps"][0], "meson setup build --prefix=/usr")
        self.assertEqual(task["limits"], {"cpu_minutes": 60, "memory_gib": 4, "disk_gib": 10})
        self.assertEqual(task["repo"], "file://" + self.repo)

    def test_the_recipe_is_frozen_in_the_task(self):
        self.seed_seen()
        build_id = self.request()
        H.edit_yaml(os.path.join(self.cfg.content, "apps", "tool", "build.yaml"), lambda d: dict(d, steps=["make"]))
        self.assertEqual(len(self.task(build_id)["recipe"]["steps"]), 3)

    def test_version_must_have_been_seen(self):
        for version in ("1.0.0", "9.9.9"):
            with self.subTest(version=version):
                code, _, err = self.run_cli("build-request", "tool", "--version", version, "--by", "alice")
                self.assertEqual(code, 1)
                self.assertIn("has not been seen", err)
        self.assertEqual(self.st.builds(), [])

    def test_unfinished_task_blocks_a_second_one(self):
        self.seed_seen()
        first = self.request()
        for status in ("queued", "fetching", "ready", "building", "collecting"):
            self.set_status(first, status=status)
            code, _, err = self.run_cli("build-request", "tool", "--version", "1.0.0", "--by", "bob")
            self.assertEqual(code, 1, status)
            self.assertIn("unfinished build", err)
        self.assertEqual(len(self.st.builds()), 1)

    def test_rebuilds_count_up_and_never_overwrite(self):
        self.seed_seen()
        ids = []
        for expected, status in ((1, "failed"), (2, "cancelled"), (3, "done")):
            ids.append(self.request())
            self.assertEqual(self.task(ids[-1])["version"], "1.0.0+agino%d" % expected)
            self.set_status(ids[-1], status=status)
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual([t["attempt"] for t in self.st.builds(app="tool")], [1, 2, 3])

    def test_argument_errors(self):
        self.seed_seen()
        for argv, code in [(("tool", "--version", "1.0 x", "--by", "alice"), 2),
                           (("tool", "--version", "-1", "--by", "alice"), 2),
                           (("tool", "--version", "1.0.0", "--by", "a b"), 2),
                           (("tool", "--version", "1.0.0", "--by", "x" * 33), 2),
                           (("Bad Id", "--version", "1.0.0", "--by", "alice"), 2),
                           (("gimp", "--version", "1.0.0", "--by", "alice"), 1)]:
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cli("build-request", *argv)[0], code)
        self.assertEqual(self.st.builds(), [])

    def test_auto_is_a_valid_requester(self):
        self.seed_seen()
        self.assertEqual(self.task(self.request(by="auto"))["by"], "auto")


class TestCancel(BuildCase):
    def test_cancel_a_queued_task(self):
        self.seed_seen()
        build_id = self.request()
        code, out, _ = self.run_cli("build-cancel", build_id, "--by", "bob")
        self.assertEqual((code, out.strip()), (0, "cancelled " + build_id))
        task = self.task(build_id)
        self.assertEqual((task["status"], task["error"]), ("cancelled", "cancelled by bob"))
        self.assertTrue(task["finished"])
        self.assertFalse(os.path.exists(os.path.join(self.queue, build_id)))

    def test_cancel_writes_the_cancel_file_for_a_task_with_a_queue_directory(self):
        self.seed_seen()
        build_id = self.request()
        os.makedirs(os.path.join(self.queue, build_id))
        self.set_status(build_id, status="building")
        self.assertEqual(self.run_cli("build-cancel", build_id, "--by", "bob")[0], 0)
        with open(os.path.join(self.queue, build_id, "cancel"), encoding="utf-8") as f:
            self.assertIn("bob", f.read())
        self.assertEqual(oct(os.stat(os.path.join(self.queue, build_id, "cancel")).st_mode & 0o777), "0o640")

    def test_finished_tasks_cannot_be_cancelled(self):
        self.seed_seen()
        build_id = self.request()
        for status in ("done", "failed", "cancelled"):
            self.set_status(build_id, status=status)
            code, _, err = self.run_cli("build-cancel", build_id, "--by", "bob")
            self.assertEqual(code, 1, status)
            self.assertIn("already " + status, err)

    def test_unknown_and_bad_ids(self):
        self.assertEqual(self.run_cli("build-cancel", "0123456789abcdef", "--by", "bob")[0], 2)
        self.assertEqual(self.run_cli("build-cancel", "../etc", "--by", "bob")[0], 2)
        self.assertEqual(self.run_cli("build-cancel", "0123456789abcdef", "--by", "b b")[0], 2)


@unittest.skipUnless(GIT, "git not available")
class TestFetchStep(BuildCase):
    def test_a_queued_task_becomes_ready(self):
        build_id = self.ready_task()
        directory = os.path.join(self.queue, build_id)
        self.assertEqual(sorted(os.listdir(directory)), ["source.tar", "task.json"])
        self.assertEqual(oct(os.stat(directory).st_mode & 0o777), "0o750")
        for name in os.listdir(directory):
            self.assertEqual(oct(os.stat(os.path.join(directory, name)).st_mode & 0o777), "0o640")
        with open(os.path.join(directory, "task.json"), encoding="utf-8") as f:
            doc = json.load(f)
        task = self.task(build_id)
        self.assertEqual(doc["source"], {"file": "source.tar", "sha256": task["source"]["sha256"],
                                         "size": task["source"]["size"]})
        for key, value in {"id": build_id, "app": "tool", "version": "1.0.0+agino1", "upstream_version": "1.0.0",
                           "tag": "v1.0.0", "commit": self.commit, "attempt": 1, "by": "alice",
                           "limits": {"cpu_minutes": 60, "memory_gib": 4, "disk_gib": 10}}.items():
            self.assertEqual(doc[key], value, key)
        # the runner rejects unknown keys: nothing but the agreed ones
        self.assertEqual(set(doc), {"id", "app", "version", "upstream_version", "tag", "commit", "attempt", "by",
                                    "created", "source", "recipe", "limits", "mode"})
        self.assertEqual(doc["mode"], "build")
        self.assertEqual(set(doc["recipe"]), {"base", "source", "build_depends", "steps", "package", "limits"})
        self.assertEqual(doc["recipe"]["source"], {"kind": "git", "checksum": "required"})
        self.assertEqual(set(doc["recipe"]["package"]), {"name", "section", "depends", "maintainer"})
        self.assertEqual(doc["recipe"]["package"]["name"], "tool-agino")
        with tarfile.open(os.path.join(directory, "source.tar")) as tar:
            self.assertEqual(sorted(tar.getnames()), ["README", "src", "src/main.c"])
        self.assertEqual([n for n in os.listdir(self.queue) if n.startswith(".fetch-")], [])

    def test_source_that_no_longer_matches_the_pinned_commit_fails_the_task(self):
        self.seed_seen(commit="0" * 40)
        build_id = self.request()
        code, out, _ = self.run_cli("build-step")
        self.assertEqual(code, 1)
        task = self.task(build_id)
        self.assertEqual(task["status"], "failed")
        self.assertIn("now points to", task["error"])
        self.assertIn("failed " + build_id, out)
        self.assertFalse(os.path.exists(os.path.join(self.queue, build_id)))
        self.assertEqual([n for n in os.listdir(self.queue) if n.startswith(".fetch-")], [])

    def test_cancel_during_the_fetch_wins(self):
        self.seed_seen()
        build_id = self.request()

        def fake_fetch(cfg, source, repo, tag, commit, workdir, dest):
            self.assertEqual(self.run_cli("build-cancel", build_id, "--by", "bob")[0], 0)   # the lock is free
            H.write(dest, b"not really a tar")
            return "a" * 64, 16

        with mock.patch.object(upstream_fetch, "fetch_source", side_effect=fake_fetch):
            code, out, _ = self.run_cli("build-step")
        self.assertEqual(code, 0)
        self.assertEqual(self.task(build_id)["status"], "cancelled")
        self.assertFalse(os.path.exists(os.path.join(self.queue, build_id)))
        self.assertIn("dropped the source", out)
        self.assertEqual([n for n in os.listdir(self.queue) if n.startswith(".fetch-")], [])

    def test_a_task_stuck_in_fetching_is_retried(self):
        self.seed_seen()
        build_id = self.request()
        self.set_status(build_id, status="fetching")
        self.assertEqual(self.run_cli("build-step")[0], 0)
        self.assertEqual(self.task(build_id)["status"], "ready")

    def test_an_unexpected_error_fails_only_that_task(self):
        self.seed_seen()
        build_id = self.request()
        with mock.patch.object(upstream_fetch, "fetch_source", side_effect=RuntimeError("boom")):
            self.assertEqual(self.run_cli("build-step")[0], 1)
        self.assertEqual(self.task(build_id)["status"], "failed")
        self.assertIn("RuntimeError", self.task(build_id)["error"])

    def test_a_second_run_at_the_same_time_does_nothing(self):
        self.seed_seen()
        build_id = self.request()
        with self.st.lock("build-step"):
            code, out, _ = self.run_cli("build-step")
        self.assertEqual((code, out.strip()), (0, "build-step is already running"))
        self.assertEqual(self.task(build_id)["status"], "queued")

    def test_the_main_lock_is_only_waited_for_a_short_time(self):
        self.seed_seen()
        self.request()
        with mock.patch.object(build_step, "LOCK_WAIT", 0.3):
            with self.st.lock():
                self.assertEqual(self.run_cli("build-step")[0], 3)

    def test_nothing_to_do(self):
        code, out, _ = self.run_cli("build-step")
        self.assertEqual((code, out), (0, ""))


class TestRunnerStatus(BuildCase):
    def ready(self):
        self.seed_seen()
        build_id = self.request()
        self.set_status(build_id, status="ready")
        return build_id

    def write_status(self, build_id, data):
        H.write(os.path.join(self.out, build_id, "status.json"), json.dumps(data), "w")

    def test_every_running_state_means_building(self):
        for state in ("starting", "installing_deps", "building", "packaging", "collecting"):
            with self.subTest(state=state):
                build_id = self.ready()
                self.write_status(build_id, {"id": build_id, "state": state, "started": "2026-11-04T10:00:00Z",
                                             "updated": "x", "step": 1, "steps_total": 3, "error": ""})
                self.assertEqual(self.run_cli("build-step")[0], 0)
                task = self.task(build_id)
                self.assertEqual((task["status"], task["started"]), ("building", "2026-11-04T10:00:00Z"))
                self.set_status(build_id, status="cancelled")

    def test_junk_status_is_ignored(self):
        build_id = self.ready()
        for data in ({"state": "weird"}, {"status": "building"}, {"state": "done"}, [1], "x"):
            with self.subTest(data=data):
                self.set_status(build_id, status="ready", started=None)
                self.write_status(build_id, data)
                self.run_cli("build-step")
                self.assertEqual(self.task(build_id)["status"], "ready")

    def test_a_bad_start_time_is_replaced(self):
        build_id = self.ready()
        self.write_status(build_id, {"state": "building", "started": "yesterday"})
        self.run_cli("build-step")
        task = self.task(build_id)
        self.assertEqual(task["status"], "building")
        self.assertRegex(task["started"], r"\A\d{4}-\d\d-\d\dT")

    def test_a_stopped_runner_without_a_result_fails_the_task_after_a_while(self):
        build_id = self.ready()
        self.write_status(build_id, {"state": "failed", "error": "qga went away\x1b[31m"})
        self.run_cli("build-step")
        self.assertEqual(self.task(build_id)["status"], "ready")     # the result may still be coming
        old = time.time() - 600
        os.utime(os.path.join(self.out, build_id, "status.json"), (old, old))
        self.assertEqual(self.run_cli("build-step")[0], 1)
        task = self.task(build_id)
        self.assertEqual(task["status"], "failed")
        self.assertIn("qga went away", task["error"])
        self.assertNotIn("\x1b", task["error"])

    def test_a_symlinked_out_directory_is_not_followed(self):
        build_id = self.ready()
        elsewhere = os.path.join(self._tmp.name, "elsewhere")
        H.write(os.path.join(elsewhere, "status.json"), json.dumps({"status": "building"}), "w")
        os.symlink(elsewhere, os.path.join(self.out, build_id))
        self.run_cli("build-step")
        self.assertEqual(self.task(build_id)["status"], "ready")


class TestTimeouts(BuildCase):
    def building(self, started_ago):
        self.seed_seen()
        build_id = self.request()
        os.makedirs(os.path.join(self.queue, build_id))
        self.set_status(build_id, status="building", started=stamp(started_ago))
        return build_id

    def test_a_task_past_its_limit_fails_with_a_timeout_and_a_cancel_file(self):
        build_id = self.building((60 + 35) * 60 + 120)
        code, out, _ = self.run_cli("build-step")
        self.assertEqual(code, 1)
        task = self.task(build_id)
        self.assertEqual((task["status"], task["error"]), ("failed", "timeout"))
        self.assertTrue(task["finished"])
        self.assertTrue(os.path.exists(os.path.join(self.queue, build_id, "cancel")))

    def test_a_task_inside_its_limit_keeps_running(self):
        build_id = self.building(60 * 60)
        self.assertEqual(self.run_cli("build-step")[0], 0)
        self.assertEqual(self.task(build_id)["status"], "building")

    def test_the_margin_covers_the_runner_phases(self):
        build_id = self.building((60 + 30) * 60)   # the runner may still be packaging and retrieving
        self.assertEqual(self.run_cli("build-step")[0], 0)
        self.assertEqual(self.task(build_id)["status"], "building")

    def test_a_ready_task_that_never_started_does_not_time_out(self):
        self.seed_seen()
        build_id = self.request()
        self.set_status(build_id, status="ready", created=stamp(10 * 86400))
        self.run_cli("build-step")
        self.assertEqual(self.task(build_id)["status"], "ready")


class TestHousekeeping(BuildCase):
    def finished(self, seconds_ago, status="done"):
        self.seed_seen()
        build_id = self.request()
        os.makedirs(os.path.join(self.queue, build_id))
        H.write(os.path.join(self.queue, build_id, "source.tar"), b"x")
        self.set_status(build_id, status=status, finished=stamp(seconds_ago))
        return build_id

    def test_old_queue_directories_of_finished_tasks_are_removed(self):
        old = self.finished(2 * 3600)
        self.run_cli("build-step")
        self.assertFalse(os.path.exists(os.path.join(self.queue, old)))
        self.assertEqual(self.task(old)["status"], "done")

    def test_recent_ones_stay(self):
        recent = self.finished(600, status="failed")
        self.run_cli("build-step")
        self.assertTrue(os.path.isdir(os.path.join(self.queue, recent)))

    def test_active_tasks_keep_their_directory(self):
        build_id = self.finished(2 * 3600)
        self.set_status(build_id, status="ready", finished=None)
        self.run_cli("build-step")
        self.assertTrue(os.path.isdir(os.path.join(self.queue, build_id)))

    def test_stale_fetch_directories_are_removed(self):
        stale, fresh = (os.path.join(self.queue, n) for n in (".fetch-old", ".fetch-new"))
        for path in (stale, fresh):
            H.write(os.path.join(path, "source.tar"), b"x")
        old = time.time() - 2 * 86400
        os.utime(stale, (old, old))
        self.run_cli("build-step")
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(fresh))

    def test_only_fetch_directories_are_touched(self):
        other = os.path.join(self.queue, "keep-me")
        H.write(os.path.join(other, "x"), b"x")
        old = time.time() - 5 * 86400
        os.utime(other, (old, old))
        self.run_cli("build-step")
        self.assertTrue(os.path.exists(other))


class TestTaskDocumentShape(unittest.TestCase):
    def test_ids_look_like_the_contract(self):
        self.assertTrue(re.match(r"\A[0-9a-f]{16}\Z", "0123456789abcdef"))
        self.assertEqual(builds.TERMINAL, ("done", "failed", "cancelled"))


if __name__ == "__main__":
    unittest.main()
