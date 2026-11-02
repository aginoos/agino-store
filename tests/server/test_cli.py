# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import datetime
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import agino_store_server
from agino_store_server import cli
from agino_store_server.state import State

from . import helpers as H

SCRIPT = os.path.join(H.REPO, "server", "agino-store-server")


def sha(char):
    return char * 64


class CliCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = self._tmp.name
        self.key, self.pub = H.make_key(base)
        self.cfg = H.make_config(base, self.key)
        self.conf = os.path.join(base, "server.conf")
        H.write_conf_file(self.conf, self.cfg)
        H.make_content(self.cfg.content)
        self.st = State(self.cfg.state)
        env = mock.patch.dict(os.environ, {"SUDO_USER": "alice", "SOURCE_DATE_EPOCH": "1793620800"})
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--config", self.conf, *argv])
        return code, out.getvalue(), err.getvalue()

    def add(self, version="2.0-1", char="a", app="chrome", **extra):
        record = H.candidate(app=app, version=version, sha=sha(char), **extra)
        return self.st.add_candidate(record)

    def status(self, version, char="a", app="chrome"):
        return self.st.find_candidate(app, version, sha(char))["status"]

    def log_lines(self):
        with open(os.path.join(self.cfg.state, "approvals.log"), encoding="utf-8") as f:
            return [json.loads(line) for line in f]


class TestApprove(CliCase):
    def test_approve_records_everything(self):
        self.add()
        code, out, _ = self.run_cli("approve", "chrome", "--note", "looks fine")
        self.assertEqual(code, 0, out)
        entry = self.st.approved()["chrome"]
        self.assertEqual((entry["version"], entry["sha256"], entry["by"], entry["note"]),
                         ("2.0-1", sha("a"), "alice", "looks fine"))
        self.assertEqual(entry["candidate"], "2.0-1_aaaaaaaaaaaa")
        self.assertEqual(entry["approved_at"], "2026-11-02T12:00:00Z")
        self.assertEqual(self.status("2.0-1"), "approved")
        line = self.log_lines()[0]
        self.assertEqual((line["action"], line["by"], line["no_sandbox"]), ("approve", "alice", False))

    def test_dependency_failure_blocks(self):
        self.add(checks={"deps": {"ok": False, "detail": "unsatisfiable"}, "sandbox": {"status": "pass"}})
        code, _, err = self.run_cli("approve", "chrome@2.0-1", "--no-sandbox")
        self.assertEqual(code, 1)
        self.assertIn("dependency check", err)
        self.assertEqual(self.st.approved(), {})

    def test_sandbox_needs_override(self):
        self.add(checks={"deps": {"ok": True}, "sandbox": {"status": "missing"}})
        self.assertEqual(self.run_cli("approve", "chrome")[0], 1)
        self.assertEqual(self.st.approved(), {})
        self.assertEqual(self.run_cli("approve", "chrome", "--no-sandbox")[0], 0)
        line = self.log_lines()[0]
        self.assertTrue(line["no_sandbox"])
        self.assertEqual(line["sandbox"], "missing")

    def test_failed_sandbox_blocks(self):
        self.add(checks={"deps": {"ok": True}, "sandbox": {"status": "fail"}})
        self.assertEqual(self.run_cli("approve", "chrome")[0], 1)

    def test_supersedes_older_pending_only(self):
        self.add("1.0-1", "b")
        self.add("2.0-1", "a")
        self.add("3.0-1", "c")
        self.assertEqual(self.run_cli("approve", "chrome@2.0-1")[0], 0)
        self.assertEqual(self.status("1.0-1", "b"), "superseded")
        self.assertEqual(self.status("2.0-1", "a"), "approved")
        self.assertEqual(self.status("3.0-1", "c"), "pending")

    def test_default_version_is_highest_pending(self):
        self.add("1.0-1", "b")
        self.add("10.0-1", "c")
        self.add("9.0-1", "a")
        self.run_cli("approve", "chrome")
        self.assertEqual(self.st.approved()["chrome"]["version"], "10.0-1")

    def test_downgrade_refused(self):
        self.add("2.0-1", "a")
        self.run_cli("approve", "chrome")
        self.add("1.0-1", "b")
        code, _, err = self.run_cli("approve", "chrome@1.0-1")
        self.assertEqual(code, 2)
        self.assertIn("not newer", err)
        self.assertEqual(self.st.approved()["chrome"]["version"], "2.0-1")

    def test_unknown_target_and_bad_ids(self):
        self.assertEqual(self.run_cli("approve", "chrome")[0], 2)
        self.assertEqual(self.run_cli("approve", "Bad Id")[0], 2)
        self.assertEqual(self.run_cli("approve", "chrome@")[0], 2)

    def test_awaiting_upload_cannot_be_approved(self):
        self.st.add_candidate(H.candidate(sha=None, status="awaiting-upload"))
        self.assertEqual(self.run_cli("approve", "chrome")[0], 2)

    def test_approve_all_skips_blocked(self):
        self.add()
        self.add(app="tg", checks={"deps": {"ok": False}, "sandbox": {"status": "pass"}})
        code, out, _ = self.run_cli("approve", "all")
        self.assertEqual(code, 1)
        self.assertIn("approved chrome 2.0-1", out)
        self.assertIn("skipped tg", out)
        self.assertEqual(sorted(self.st.approved()), ["chrome"])


class TestReject(CliCase):
    def test_reject(self):
        self.add()
        code, out, _ = self.run_cli("reject", "chrome@2.0-1", "--note", "bad postinst")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.status("2.0-1"), "rejected")
        line = self.log_lines()[0]
        self.assertEqual((line["action"], line["note"], line["by"]), ("reject", "bad postinst", "alice"))
        self.assertEqual(self.st.approved(), {})

    def test_needs_version_and_candidate(self):
        self.assertEqual(self.run_cli("reject", "chrome")[0], 2)
        self.assertEqual(self.run_cli("reject", "chrome@9")[0], 2)

    def test_approved_cannot_be_rejected(self):
        self.add()
        self.run_cli("approve", "chrome")
        self.assertEqual(self.run_cli("reject", "chrome@2.0-1")[0], 2)


class TestPending(CliCase):
    def test_lists_open_candidates_only(self):
        self.add("1.0-1", "b", status="approved")
        self.add("2.0-1", "a")
        self.st.add_candidate(H.candidate(app="tg", sha=None, status="awaiting-upload"))
        code, out, _ = self.run_cli("pending", "--json")
        self.assertEqual(code, 0)
        listed = json.loads(out)
        self.assertEqual(sorted((r["app"], r["status"]) for r in listed),
                         [("chrome", "pending"), ("tg", "awaiting-upload")])
        self.assertTrue(all("_path" not in r for r in listed))

    def test_text_output(self):
        self.add()
        _, out, _ = self.run_cli("pending")
        self.assertIn("chrome", out)
        self.assertIn("deps:ok", out)
        self.assertIn("sandbox:pass", out)


class TestLockingAndConfig(CliCase):
    def test_locked_exit_code(self):
        self.add()
        with self.st.lock():
            for argv in (("approve", "chrome"), ("reject", "chrome@2.0-1"), ("build",), ("reports-ingest",)):
                self.assertEqual(self.run_cli(*argv)[0], 3, argv)

    def test_bad_config(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--config", os.path.join(self._tmp.name, "nope.conf"), "pending"])
        self.assertEqual(code, 2)

    def test_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            cli.main(["--config", self.conf, "frobnicate"])
        self.assertEqual(ctx.exception.code, 2)

    def test_script_entry_point(self):
        done = subprocess.run([sys.executable, "-I", SCRIPT, "--config", self.conf, "pending", "--json"],
                              capture_output=True, text=True)
        self.assertEqual((done.returncode, json.loads(done.stdout)), (0, []), done.stderr)
        self.assertTrue(os.access(SCRIPT, os.X_OK))


class TestBuildCommand(CliCase):
    def prepare(self):
        record = self.add(status="approved")
        H.write(os.path.join(self.cfg.downloads, record["file"]), b"x" * record["size"])
        self.st.set_approved("chrome", {"version": "2.0-1", "sha256": sha("a"), "candidate": "c",
                                        "approved_at": "2026-11-02T08:00:00Z", "by": "t", "note": ""}, {})
        self.st.save_milkyway({"gimp": {"version": "1", "first_seen": "2026-10-01T00:00:00Z"}})

    def test_build_and_if_changed(self):
        self.prepare()
        code, out, _ = self.run_cli("build")
        self.assertEqual(code, 0, out)
        self.assertIn("2 apps", out)
        self.assertTrue(os.path.isdir(os.path.join(self.cfg.preview, "current")))
        code, out, _ = self.run_cli("build", "--if-changed")
        self.assertEqual(code, 0)
        self.assertIn("unchanged", out)

    def test_invalid_content_is_reported(self):
        os.unlink(os.path.join(self.cfg.content, "categories.yaml"))
        code, _, err = self.run_cli("build")
        self.assertEqual(code, 1)
        self.assertIn("categories.yaml", err)
        self.assertFalse(os.path.exists(os.path.join(self.cfg.preview, "current")))


class TestMetrics(CliCase):
    def test_metrics_file(self):
        self.add()
        self.add("3.0-1", "c", app="tg", status="approved")
        self.st.set_approved("tg", {"version": "3.0-1"}, {})
        self.st.save_vendor_memo("chrome", {"last_ok": "2026-11-02T08:00:00Z", "last_error": "boom"})
        self.st.save_catalog_state({"serial": 2026110201, "built_at": "2026-11-02T09:00:00Z"})
        code, _, _ = self.run_cli("metrics")
        self.assertEqual(code, 0)
        with open(os.path.join(self.cfg.metrics, "agino_store_server.prom"), encoding="utf-8") as f:
            text = f.read()
        ok = int(datetime.datetime(2026, 11, 2, 8, tzinfo=datetime.timezone.utc).timestamp())
        built = int(datetime.datetime(2026, 11, 2, 9, tzinfo=datetime.timezone.utc).timestamp())
        for line in ("agino_store_pending_candidates 1", "agino_store_awaiting_upload_candidates 0",
                     "agino_store_approved_apps 1", "agino_store_catalog_serial 2026110201",
                     "agino_store_catalog_last_build_timestamp_seconds %d" % built,
                     'agino_store_vendor_last_ok_timestamp_seconds{vendor="chrome"} %d' % ok,
                     'agino_store_vendor_last_error{vendor="chrome"} 1'):
            self.assertIn(line + "\n", text)
        self.assertNotIn("expires", text)
        self.assertEqual(os.listdir(self.cfg.metrics), ["agino_store_server.prom"])

    def test_expiry_after_build(self):
        TestBuildCommand.prepare(self)
        self.run_cli("build")
        self.run_cli("metrics")
        with open(os.path.join(self.cfg.metrics, "agino_store_server.prom"), encoding="utf-8") as f:
            text = f.read()
        expires = int(datetime.datetime(2026, 11, 16, 12, tzinfo=datetime.timezone.utc).timestamp())
        self.assertIn("agino_store_catalog_expires_timestamp_seconds %d\n" % expires, text)


class TestLazyModules(CliCase):
    def fake(self, name, **funcs):
        module = types.ModuleType("agino_store_server." + name)
        for key, value in funcs.items():
            setattr(module, key, value)
        patches = [mock.patch.dict(sys.modules, {module.__name__: module}),
                   mock.patch.object(agino_store_server, name, module, create=True)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_track(self):
        calls = []
        self.fake("trackers", run_track=lambda cfg, st, content, vendor=None: calls.append(vendor) or [
            {"vendor": "chrome", "message": "new 2.0-1"}])
        code, out, _ = self.run_cli("track", "--vendor", "chrome")
        self.assertEqual((code, calls), (0, ["chrome"]))
        self.assertIn("chrome: new 2.0-1", out)

    def test_track_problem(self):
        self.fake("trackers", run_track=lambda cfg, st, content, vendor=None: [{"vendor": "x", "error": "down"}])
        self.assertEqual(self.run_cli("track")[0], 1)

    def test_track_crash_is_reported(self):
        def boom(cfg, st, content, vendor=None):
            raise RuntimeError("kaput")
        self.fake("trackers", run_track=boom)
        code, _, err = self.run_cli("track")
        self.assertEqual(code, 1)
        self.assertIn("kaput", err)

    def test_check(self):
        self.fake("checks", run_check=lambda cfg, st, content, app=None: [{"app": "gimp", "problem": "gone"}])
        code, out, _ = self.run_cli("check", "--app", "gimp")
        self.assertEqual(code, 1)
        self.assertIn("gone", out)

    def test_intake(self):
        seen = []
        self.fake("trackers", intake=lambda cfg, st, content, vendor, path, url: seen.append((vendor, path, url)) or {
            "app": "chrome", "version": "2.0-1", "status": "pending"})
        code, out, _ = self.run_cli("intake", "chrome", "/tmp/x.deb", "--url", "https://example.org/")
        self.assertEqual((code, seen), (0, [("chrome", "/tmp/x.deb", "https://example.org/")]))
        self.assertIn("taken in chrome 2.0-1", out)


class TestReportsCommand(CliCase):
    def test_ingest_exit_code(self):
        self.add()
        H.write(os.path.join(self.cfg.reports, "bad.json"), b"{}")
        code, out, _ = self.run_cli("reports-ingest")
        self.assertEqual(code, 1)
        self.assertIn("rejected", out)


if __name__ == "__main__":
    unittest.main()
