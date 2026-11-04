# SPDX-License-Identifier: GPL-3.0-or-later
import os
import re
import time
import unittest

from agino_store_server import build_collect, fetch

from . import helpers as H
from .build_case import DPKG, BuildCase


def active_task(case, status="building"):
    case.seed_seen()
    build_id = case.request()
    os.makedirs(os.path.join(case.queue, build_id), exist_ok=True)
    case.set_status(build_id, status=status)
    return build_id


@unittest.skipUnless(DPKG, "dpkg-deb not available")
class TestCollectOk(BuildCase):
    def collect(self, deb=None, **extra):
        build_id = active_task(self)
        deb = deb or self.make_deb("1.0.0+agino1")
        self.write_result(build_id, self.result_for(deb, **extra), deb)
        with self.deps_ok():
            code, out, err = self.run_cli("build-step")
        return build_id, code, out, err

    def test_creates_a_pending_built_candidate(self):
        build_id, code, out, err = self.collect()
        self.assertEqual((code, err), (0, ""))
        task = self.task(build_id)
        self.assertEqual((task["status"], task["error"]), ("done", ""))
        self.assertTrue(task["finished"])
        candidates = self.st.candidates(app="tool")
        self.assertEqual(len(candidates), 1)
        cand = candidates[0]
        self.assertEqual(cand["origin"], {"type": "built", "source_url": "file://" + self.repo, "source_ref": "v1.0.0",
                                          "source_commit": self.commit, "build_job": build_id})
        for key, value in {"status": "pending", "version": "1.0.0+agino1", "package": "tool-agino",
                           "arch": "amd64", "vendor": None, "app": "tool"}.items():
            self.assertEqual(cand[key], value, key)
        self.assertEqual(set(cand["checks"]), {"deps", "inspect", "diff", "sandbox"})
        self.assertEqual(cand["checks"]["sandbox"], {"status": "missing"})
        self.assertEqual(cand["checks"]["diff"]["against"], None)
        self.assertEqual(task["candidate"], os.path.basename(cand["_path"])[:-5])
        self.assertEqual(task["artifact"], {"file": cand["filename"], "sha256": cand["sha256"], "size": cand["size"]})
        stored = os.path.join(self.cfg.downloads, cand["sha256"], task["artifact"]["file"])
        self.assertEqual(fetch.file_sha256(stored), cand["sha256"])
        self.assertIn("collected %s" % build_id, out)

    def test_collecting_twice_makes_one_candidate(self):
        build_id, *_ = self.collect()
        self.set_status(build_id, status="building", finished=None)
        with self.deps_ok():
            self.assertEqual(self.run_cli("build-step")[0], 0)
        self.assertEqual(len(self.st.candidates(app="tool")), 1)

    def test_scripts_and_flags_are_inspected(self):
        deb = self.make_deb("1.0.0+agino1", scripts={"postinst": "#!/bin/sh\nsystemctl enable tool\n"})
        self.collect(deb)
        cand = self.st.candidates(app="tool")[0]
        self.assertIn("postinst", cand["checks"]["inspect"]["scripts"])
        self.assertTrue(cand["checks"]["inspect"]["flags"])
        self.assertEqual(cand["checks"]["diff"]["scripts_changed"], ["postinst"])

    def test_architecture_all_is_fine(self):
        self.collect(self.make_deb("1.0.0+agino1", arch="all"))
        self.assertEqual(len(self.st.candidates(app="tool")), 1)

    def test_a_ready_task_with_a_result_is_collected_too(self):
        build_id = active_task(self, "ready")
        deb = self.make_deb("1.0.0+agino1")
        self.write_result(build_id, self.result_for(deb), deb)
        with self.deps_ok():
            self.run_cli("build-step")
        self.assertEqual(self.task(build_id)["status"], "done")

    def test_the_task_is_not_timed_out_when_its_result_is_there(self):
        build_id = active_task(self)
        self.set_status(build_id, started="2020-01-01T00:00:00Z")
        deb = self.make_deb("1.0.0+agino1")
        self.write_result(build_id, self.result_for(deb), deb)
        with self.deps_ok():
            self.run_cli("build-step")
        self.assertEqual(self.task(build_id)["status"], "done")

    def test_finished_tasks_are_left_alone(self):
        build_id = active_task(self, "cancelled")
        deb = self.make_deb("1.0.0+agino1")
        self.write_result(build_id, self.result_for(deb), deb)
        with self.deps_ok():
            self.run_cli("build-step")
        self.assertEqual((self.task(build_id)["status"], self.st.candidates()), ("cancelled", []))


@unittest.skipUnless(DPKG, "dpkg-deb not available")
class TestCollectRefusals(BuildCase):
    def failed(self, result=None, deb=None, mutate=None):
        """Run build-step on a task whose runner output is odd; returns the failed task."""
        build_id = active_task(self)
        deb = deb or self.make_deb("1.0.0+agino1")
        result = result if result is not None else self.result_for(deb)
        self.write_result(build_id, result, deb)
        if mutate:
            mutate(os.path.join(self.out, build_id))
        with self.deps_ok():
            code, _, _ = self.run_cli("build-step")
        task = self.task(build_id)
        self.assertEqual((task["status"], code), ("failed", 1), task)
        self.assertEqual(self.st.candidates(), [])
        return task

    def test_a_failed_build_keeps_the_error_without_control_characters(self):
        task = self.failed({"ok": False, "error": "step 2 failed\x1b[31m\nbad", "steps": [], "artifacts": []})
        self.assertEqual(task["error"], "step 2 failed[31mbad")

    def test_a_failed_build_without_text_names_the_step(self):
        task = self.failed({"ok": False, "error": "", "steps": [{"n": 1, "exit": 0}, {"n": 2, "exit": 2}],
                            "artifacts": []})
        self.assertEqual(task["error"], "build step 2 exited with 2")

    def test_long_errors_are_cut(self):
        task = self.failed({"ok": False, "error": "x" * 5000, "steps": [], "artifacts": []})
        self.assertEqual(len(task["error"]), 500)

    def test_wrong_package_name(self):
        task = self.failed(deb=self.make_deb("1.0.0+agino1", package="evil-package"))
        self.assertIn("is not 'tool-agino'", task["error"])

    def test_wrong_version(self):
        task = self.failed(deb=self.make_deb("1.0.1+agino1"))
        self.assertIn("differs from tracked version", task["error"])

    def test_wrong_architecture(self):
        task = self.failed(deb=self.make_deb("1.0.0+agino1", arch="arm64"))
        self.assertIn("Architecture", task["error"])

    def test_sha256_mismatch(self):
        deb = self.make_deb("1.0.0+agino1")
        result = self.result_for(deb)
        result["artifacts"][0]["sha256"] = "0" * 64
        self.assertIn("sha256", self.failed(result, deb)["error"])

    def test_size_mismatch(self):
        deb = self.make_deb("1.0.0+agino1")
        result = self.result_for(deb)
        result["artifacts"][0]["size"] += 1
        self.assertIn("wrong size", self.failed(result, deb)["error"])

    def test_artifact_limit(self):
        with open(self.conf, encoding="utf-8") as f:
            text = re.sub(r"max_deb_bytes = \d+", "max_deb_bytes = 10", f.read())
        H.write(self.conf, text, "w")
        self.assertIn("exceeds the artifact limit", self.failed()["error"])

    def test_not_exactly_one_deb(self):
        deb = self.make_deb("1.0.0+agino1")
        result = self.result_for(deb)
        self.assertIn("exactly one deb", self.failed(dict(result, artifacts=[]), deb)["error"])
        again = self.make_deb("1.0.0+agino1")
        self.assertIn("exactly one deb", self.failed(dict(result, artifacts=result["artifacts"] * 2), again)["error"])

    def test_unsafe_artifact_names(self):
        deb = self.make_deb("1.0.0+agino1")
        for name in ("../x.deb", "a/b.deb", ".hidden.deb", "x y.deb", "x\n.deb", "x" * 300 + ".deb"):
            with self.subTest(name=name):
                result = self.result_for(deb)
                result["artifacts"][0]["file"] = name
                self.assertIn("bad artifact file name", self.failed(result, deb)["error"])

    def test_missing_artifact(self):
        deb = self.make_deb("1.0.0+agino1")
        task = self.failed(self.result_for(deb), deb, mutate=lambda d: os.unlink(os.path.join(d, os.path.basename(deb[0]))))
        self.assertIn("missing", task["error"])

    def test_symlinked_artifact(self):
        deb = self.make_deb("1.0.0+agino1")

        def link(directory):
            path = os.path.join(directory, os.path.basename(deb[0]))
            os.unlink(path)
            os.symlink(deb[0], path)

        self.assertIn("not a regular file", self.failed(self.result_for(deb), deb, mutate=link)["error"])

    def test_not_a_deb(self):
        junk = (os.path.join(self._tmp.name, "tool-agino_1.0.0+agino1_amd64.deb"), b"this is not a deb")
        H.write(junk[0], junk[1])
        self.assertIn("deb was refused", self.failed(self.result_for(junk), junk)["error"])

    def test_malformed_artifact_fields(self):
        deb = self.make_deb("1.0.0+agino1")
        for patch in ({"sha256": "XYZ"}, {"size": "12"}, {"size": True}, {"sha256": None}):
            with self.subTest(patch=patch):
                result = self.result_for(deb)
                result["artifacts"][0].update(patch)
                self.assertIn("bad artifact checksum or size", self.failed(result, deb)["error"])

    def test_result_that_is_not_an_object(self):
        build_id = active_task(self)
        H.write(os.path.join(self.out, build_id, "result.json"), b"[1, 2]")
        self.run_cli("build-step")
        self.assertEqual(self.task(build_id)["status"], "failed")
        self.assertIn("not an object", self.task(build_id)["error"])


class TestResultFileTiming(BuildCase):
    def setUp(self):
        super().setUp()
        self.build_id = active_task(self)
        self.path = os.path.join(self.out, self.build_id, "result.json")
        H.write(self.path, b'{"ok": tr')

    def test_a_half_written_result_is_waited_for(self):
        self.assertEqual(self.run_cli("build-step")[0], 0)
        self.assertEqual(self.task(self.build_id)["status"], "building")

    def test_an_old_unreadable_result_fails_the_task(self):
        old = time.time() - 3600
        os.utime(self.path, (old, old))
        self.assertEqual(self.run_cli("build-step")[0], 1)
        task = self.task(self.build_id)
        self.assertEqual(task["status"], "failed")
        self.assertIn("result.json is unreadable", task["error"])

    def test_a_symlinked_result_is_unreadable(self):
        os.unlink(self.path)
        os.symlink(os.path.join(self._tmp.name, "elsewhere.json"), self.path)
        old = time.time() - 3600
        os.utime(self.path, (old, old), follow_symlinks=False)
        self.run_cli("build-step")
        self.assertEqual(self.task(self.build_id)["status"], "failed")


class TestHelpers(unittest.TestCase):
    def test_deb_name_pattern(self):
        for good in ("a.deb", "tool-agino_1.0.0+agino1_amd64.deb", "x~y.deb"):
            self.assertTrue(build_collect.DEB_NAME_RE.match(good), good)
        for bad in (".a.deb", "a.deb\n", "a b.deb", "a/b.deb", "a.deb.sig"):
            self.assertFalse(build_collect.DEB_NAME_RE.match(bad), bad)


if __name__ == "__main__":
    unittest.main()
