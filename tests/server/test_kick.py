# SPDX-License-Identifier: GPL-3.0-or-later
"""Kicks: a stage that did something wakes the next one through a file in the kick directory."""

import datetime
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

from agino_store_server import kick

from . import helpers as H
from .build_case import BuildCase
from .test_cli import CliCase


class TestKickFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def cfg(self, directory):
        return SimpleNamespace(kick_dir=directory)

    def test_writes_the_named_file(self):
        kick.kick(self.cfg(self.tmp.name), "build")
        with open(os.path.join(self.tmp.name, "build"), "rb") as f:
            self.assertEqual(f.read(), b"1")

    def test_nothing_without_a_directory_and_nothing_is_created(self):
        kick.kick(self.cfg(None), "build")
        kick.kick(self.cfg(os.path.join(self.tmp.name, "absent")), "build")
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_only_known_names(self):
        kick.kick(self.cfg(self.tmp.name), "../evil")
        kick.kick(self.cfg(self.tmp.name), "other")
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_a_symlink_in_place_of_the_file_is_not_followed(self):
        target = os.path.join(self.tmp.name, "target")
        os.symlink(target, os.path.join(self.tmp.name, "build"))
        kick.kick(self.cfg(self.tmp.name), "build")        # warns, does not raise
        self.assertFalse(os.path.exists(target))


class TestKicksOfTheCommands(BuildCase):
    def setUp(self):
        super().setUp()
        self.kicks = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.kicks, ignore_errors=True)
        with open(self.conf) as f:
            text = f.read()
        with open(self.conf, "w") as f:
            f.write(text.replace("[paths]\n", "[paths]\nkick = %s\n" % self.kicks, 1))

    def kicked(self):
        return sorted(os.listdir(self.kicks))

    def test_build_request_kicks_the_build_step(self):
        self.seed_seen()
        code, _, err = self.run_cli("build-request", "tool", "--version", "1.0.0", "--by", "alice")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.kicked(), ["build"])

    def test_a_build_step_that_did_something_kicks_the_intake_step(self):
        self.seed_seen()
        build_id = self.request()
        self.set_status(build_id, status="ready")
        started = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        H.write(os.path.join(self.out, build_id, "status.json"), json.dumps(
            {"id": build_id, "state": "building", "started": started, "updated": "x", "step": 1, "steps_total": 3,
             "error": ""}), "w")
        shutil.rmtree(self.kicks)       # request() kicked already; start from a clean directory
        os.makedirs(self.kicks)
        code, out, _ = self.run_cli("build-step")
        self.assertEqual(code, 0)
        self.assertTrue(out.strip())
        self.assertEqual(self.kicked(), ["intake"])

    def test_a_build_step_with_nothing_to_do_kicks_nobody(self):
        code, out, _ = self.run_cli("build-step")
        self.assertEqual((code, out.strip()), (0, ""))
        self.assertEqual(self.kicked(), [])

    def test_adopting_an_import_kicks_the_sandbox_queue_stage(self):
        # intake-adopt is covered with a real delivery in test_intake_adopt; here the call is only checked to exist
        from agino_store_server import intake_cmds
        import inspect
        self.assertIn('kick.kick(cfg, "queue")', inspect.getsource(intake_cmds.cmd_adopt))


class TestSandboxQueueKick(CliCase):
    def setUp(self):
        super().setUp()
        base = os.path.dirname(self.conf)
        self.kicks = os.path.join(base, "kicks")
        os.makedirs(self.kicks)
        with open(self.conf) as f:
            text = f.read()
        with open(self.conf, "w") as f:
            f.write(text.replace("[paths]\n", "[paths]\nkick = %s\nsandbox_queue = %s\n" % (
                self.kicks, os.path.join(base, "queue")), 1))

    def test_nothing_queued_nobody_is_kicked(self):
        code, _, _ = self.run_cli("sandbox-queue")
        self.assertEqual(code, 0)
        self.assertEqual(os.listdir(self.kicks), [])

    def test_a_new_job_kicks_the_sandbox_runner_once(self):
        record = self.add(checks={"deps": {"ok": True}, "sandbox": {"status": "missing"}})
        H.write(os.path.join(self.cfg.downloads, record["file"]), b"deb-bytes!\n")
        code, out, _ = self.run_cli("sandbox-queue")
        self.assertEqual(code, 0)
        self.assertIn("queued", out)
        self.assertEqual(os.listdir(self.kicks), ["sandbox"])
        os.remove(os.path.join(self.kicks, "sandbox"))
        code, out, _ = self.run_cli("sandbox-queue")      # nothing changed: no news, no kick
        self.assertNotIn("queued", out)
        self.assertEqual(os.listdir(self.kicks), [])


if __name__ == "__main__":
    unittest.main()
