# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_helpers import TASK_ID  # noqa: E402,F401

from agino_store_sandbox import build_out as O  # noqa: E402
from agino_store_sandbox.util import SandboxError  # noqa: E402

DAY = 24 * 3600
OLD = "00000000000000aa"
FRESH = "00000000000000bb"


class OutCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "out")
        self.queue = os.path.join(self.tmp.name, "queue")
        os.makedirs(self.queue)


class OutDirTests(OutCase):
    def test_create_and_write(self):
        out = O.OutDir(self.out, TASK_ID, 1 << 20)
        out.create()
        out.write_status("building", "2026-11-04T00:00:00Z", 2, 5, "")
        out.write_result(True, "", [{"n": 1, "cmd": "make", "exit": 0, "seconds": 1.5}], [])
        art = out.artifact("a_1_amd64.deb")
        art.write(b"abc")
        sha, size = art.commit()
        out.close()
        directory = os.path.join(self.out, TASK_ID)
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o750)
        status = O.read_status(self.out, TASK_ID)
        self.assertEqual((status["state"], status["step"], status["steps_total"]), ("building", 2, 5))
        with open(os.path.join(directory, "result.json")) as f:
            self.assertEqual(json.load(f)["steps"][0]["exit"], 0)
        self.assertEqual(size, 3)
        self.assertEqual(sha, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        self.assertEqual(sorted(os.listdir(directory)), ["a_1_amd64.deb", "build.log", "result.json", "status.json"])

    def test_result_class_and_files(self):
        out = O.OutDir(self.out, TASK_ID, 1 << 20)
        out.create()
        path = os.path.join(self.out, TASK_ID, "result.json")

        def result():
            with open(path) as f:
                return json.load(f)
        out.write_result(True, "", [], [])
        self.assertEqual((result()["ok"], result()["class"]), (True, "ok"))
        self.assertNotIn("files", result())
        out.write_result(False, "x", [], [])
        self.assertEqual(result()["class"], "infra")
        for klass in ("recipe", "infra", "timeout", "model"):
            out.write_result(False, "x", [], [], klass)
            self.assertEqual(result()["class"], klass)
        out.write_result(False, "x", [], [], "ok")  # a failure can never be class ok
        self.assertEqual(result()["class"], "infra")
        out.write_result(True, "", [], [], "model")  # and a success always is
        self.assertEqual(result()["class"], "ok")
        out.write_result(True, "", [], [], "ok", ["recipe.yaml", "app.yaml"])
        self.assertEqual(result()["files"], ["recipe.yaml", "app.yaml"])
        out.close()

    def test_ai_directory_and_artifacts(self):
        out = O.OutDir(self.out, TASK_ID, 1 << 20)
        out.create()
        art = out.ai_artifact("recipe.yaml")
        art.write(b"abc")
        art.commit()
        aborted = out.ai_artifact("app.yaml")
        aborted.write(b"partial")
        aborted.abort()
        out.close()
        ai = os.path.join(self.out, TASK_ID, "ai")
        self.assertEqual(stat.S_IMODE(os.stat(ai).st_mode), 0o750)
        self.assertEqual(os.listdir(ai), ["recipe.yaml"])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(ai, "recipe.yaml")).st_mode), 0o640)

    def test_bad_id(self):
        for bad in ("../x", "ABCDEF0123456789", "short"):
            with self.assertRaises(SandboxError):
                O.OutDir(self.out, bad, 10)

    def test_aborted_artifact_leaves_nothing(self):
        out = O.OutDir(self.out, TASK_ID, 1 << 20)
        out.create()
        art = out.artifact("x.deb")
        art.write(b"partial")
        art.abort()
        out.close()
        self.assertEqual(os.listdir(os.path.join(self.out, TASK_ID)), ["build.log"])

    def test_status_is_replaced_atomically_and_keeps_its_mode(self):
        out = O.OutDir(self.out, TASK_ID, 1 << 20)
        out.create()
        for state in ("starting", "building", "done"):
            out.write_status(state, "t", 0, 0, "")
        out.close()
        names = os.listdir(os.path.join(self.out, TASK_ID))
        self.assertFalse([n for n in names if n.endswith(".tmp")])

    def test_error_text_is_capped(self):
        out = O.OutDir(self.out, TASK_ID, 1 << 20)
        out.create()
        out.write_result(False, "e" * 1000, [], [])
        out.close()
        with open(os.path.join(self.out, TASK_ID, "result.json")) as f:
            self.assertEqual(len(json.load(f)["error"]), 300)

    def test_read_status_variants(self):
        self.assertEqual(O.read_status(self.out, TASK_ID), {})
        os.makedirs(os.path.join(self.out, TASK_ID))
        path = os.path.join(self.out, TASK_ID, "status.json")
        with open(path, "w") as f:
            f.write("{not json")
        self.assertIsNone(O.read_status(self.out, TASK_ID))
        os.unlink(path)
        os.symlink("/etc/passwd", path)
        self.assertIsNone(O.read_status(self.out, TASK_ID))


class LogTests(OutCase):
    def test_clean_text(self):
        self.assertEqual(O.clean_log_text("a\x00b\x1b[0m\tc\r\nd\re‮f\x85g"), "ab[0m\tc\nd\nef" + "g")

    def test_cap_marks_once_and_stops(self):
        path = os.path.join(self.tmp.name, "log")
        sink = O.LogSink(path, 100)
        sink.feed(b"x" * 80)
        self.assertFalse(sink.full)
        sink.feed(b"y" * 80)
        self.assertTrue(sink.full)
        sink.feed(b"z" * 80)
        sink.note("ignored")
        sink.close()
        with open(path) as f:
            text = f.read()
        self.assertTrue(text.startswith("x" * 80 + "y" * 20))
        self.assertEqual(text.count("log truncated"), 1)
        self.assertNotIn("z", text)
        self.assertNotIn("ignored", text)

    def test_symlink_log_is_refused(self):
        target = os.path.join(self.tmp.name, "target")
        link = os.path.join(self.tmp.name, "link")
        os.symlink(target, link)
        with self.assertRaises(OSError):
            O.LogSink(link, 10)
        self.assertFalse(os.path.exists(target))

    def test_file_mode(self):
        path = os.path.join(self.tmp.name, "log")
        O.LogSink(path, 10).close()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)


class CleanupTests(OutCase):
    def make(self, name, state="done", age_days=8, files=("build.log", "result.json", "x.deb")):
        directory = os.path.join(self.out, name)
        os.makedirs(directory)
        for f in files:
            with open(os.path.join(directory, f), "w") as fh:
                fh.write("x")
        status = os.path.join(directory, "status.json")
        with open(status, "w") as fh:
            json.dump({"state": state}, fh)
        old = 1_000_000_000 - age_days * DAY
        os.utime(status, (old, old))
        return directory

    def run_cleanup(self):
        return O.cleanup_old(self.out, self.queue, wall=lambda: 1_000_000_000)

    def test_removes_old_terminal_directories_entirely(self):
        directory = self.make(OLD)
        self.assertEqual(self.run_cleanup(), [OLD])
        self.assertFalse(os.path.exists(directory))

    def test_keeps_recent_ones(self):
        directory = self.make(FRESH, age_days=6)
        self.assertEqual(self.run_cleanup(), [])
        self.assertTrue(os.path.exists(os.path.join(directory, "result.json")))

    def test_keeps_running_and_unreadable_tasks(self):
        running = self.make(OLD, state="building")
        self.assertEqual(self.run_cleanup(), [])
        self.assertTrue(os.path.exists(running))

    def test_only_sixteen_hex_names(self):
        other = self.make("not-a-task-dir")
        upper = self.make("00000000000000AA")
        long = self.make("00000000000000aaa")
        self.assertEqual(self.run_cleanup(), [])
        for d in (other, upper, long):
            self.assertTrue(os.path.exists(d))

    def test_status_stays_as_tombstone_while_the_queue_entry_exists(self):
        directory = self.make(OLD)
        os.makedirs(os.path.join(self.queue, OLD))
        self.assertEqual(self.run_cleanup(), [OLD])
        self.assertEqual(os.listdir(directory), ["status.json"])
        self.assertEqual(self.run_cleanup(), [OLD])  # idempotent
        self.assertEqual(O.read_status(self.out, OLD)["state"], "done")

    def test_symlinked_directory_is_not_followed(self):
        victim = os.path.join(self.tmp.name, "victim")
        os.makedirs(victim)
        with open(os.path.join(victim, "keep"), "w") as f:
            f.write("data")
        os.makedirs(self.out)
        os.symlink(victim, os.path.join(self.out, OLD))
        self.assertEqual(self.run_cleanup(), [])
        self.assertTrue(os.path.exists(os.path.join(victim, "keep")))

    def test_subdirectories_are_never_descended_into(self):
        directory = self.make(OLD)
        sub = os.path.join(directory, "sub")
        os.makedirs(sub)
        with open(os.path.join(sub, "file"), "w") as f:
            f.write("x")
        self.run_cleanup()
        self.assertTrue(os.path.exists(os.path.join(sub, "file")))
        self.assertFalse(os.path.exists(os.path.join(directory, "result.json")))

    def test_missing_out_directory(self):
        self.assertEqual(O.cleanup_old(os.path.join(self.tmp.name, "nope"), self.queue), [])

    def make_ai_files(self, name):
        directory = self.make(name)
        ai = os.path.join(directory, "ai")
        os.makedirs(ai)
        for f in ("recipe.yaml", "app.yaml", "x_1_amd64.deb"):
            with open(os.path.join(ai, f), "w") as fh:
                fh.write("x")
        return directory, ai

    def test_the_ai_directory_goes_with_the_old_output(self):
        directory, ai = self.make_ai_files(OLD)
        self.assertEqual(self.run_cleanup(), [OLD])
        self.assertFalse(os.path.exists(ai))
        self.assertFalse(os.path.exists(directory))

    def test_the_ai_directory_goes_but_the_tombstone_stays(self):
        directory, ai = self.make_ai_files(OLD)
        os.makedirs(os.path.join(self.queue, OLD))
        self.run_cleanup()
        self.assertFalse(os.path.exists(ai))
        self.assertEqual(os.listdir(directory), ["status.json"])

    def test_a_recent_ai_directory_is_kept(self):
        _directory, ai = self.make_ai_files(FRESH)
        self.assertEqual(self.run_cleanup(), [])
        self.assertTrue(os.path.exists(os.path.join(ai, "recipe.yaml")))

    def test_a_symlinked_ai_directory_is_not_followed(self):
        directory = self.make(OLD)
        victim = os.path.join(self.tmp.name, "victim")
        os.makedirs(victim)
        with open(os.path.join(victim, "keep"), "w") as f:
            f.write("data")
        os.symlink(victim, os.path.join(directory, "ai"))
        self.run_cleanup()
        self.assertTrue(os.path.exists(os.path.join(victim, "keep")))

    def test_the_ai_directory_is_not_descended_into(self):
        directory, ai = self.make_ai_files(OLD)
        sub = os.path.join(ai, "sub")
        os.makedirs(sub)
        with open(os.path.join(sub, "file"), "w") as f:
            f.write("x")
        self.run_cleanup()
        self.assertTrue(os.path.exists(os.path.join(sub, "file")))
        self.assertFalse(os.path.exists(os.path.join(ai, "recipe.yaml")))


if __name__ == "__main__":
    unittest.main()
