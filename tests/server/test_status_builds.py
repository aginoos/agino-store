# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import subprocess
import unittest
from unittest import mock

from agino_store_server import status, status_sandbox

from . import helpers as H
from .build_case import BuildCase
from .test_status import snapshot

ORIGIN = {"type": "built", "source_url": "https://github.com/example/tool.git", "source_ref": "v1.0.0",
          "source_commit": "c" * 40, "build_job": "0123456789abcdef"}


class StatusBuildsCase(BuildCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(status_sandbox.subprocess, "run", side_effect=self.fake_systemctl)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def fake_systemctl(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout="LoadState=loaded\nActiveState=active\n", stderr="")

    def doc(self, *sections):
        argv = ["status", "--json"]
        for s in sections:
            argv += ["--section", s]
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def add_task(self, build_id=None, **fields):
        record = {"id": build_id or self.st.new_build_id(), "app": "tool", "version": "1.0.0+agino1",
                  "upstream_version": "1.0.0", "tag": "v1.0.0", "commit": "c" * 40, "attempt": 1, "status": "queued",
                  "by": "alice", "created": "2026-11-04T10:00:00Z", "started": None, "finished": None, "error": "",
                  "artifact": None, "candidate": None, "recipe": {"steps": ["secret-ish recipe text"]},
                  "log": "must never appear"}
        record.update(fields)
        self.st.save_build(record)
        return record["id"]


class TestUpstreamsSection(StatusBuildsCase):
    def test_configured_upstream_without_a_check_yet(self):
        entry = self.doc("upstreams")["sections"]["upstreams"][0]
        self.assertEqual(entry["app"], "tool")
        self.assertEqual((entry["repo"], entry["auto_task"], entry["in_content"]), ("file://" + self.repo, False, True))
        for key in ("latest_version", "last_check", "last_ok", "last_error"):
            self.assertIsNone(entry[key], key)
        self.assertEqual((entry["tag_moved"], entry["seen_count"], entry["versions"]), (0, 0, []))

    def test_memo_fields(self):
        moved = {"version": "1.0.0", "tag": "v1.0.0", "old_commit": "a" * 40, "new_commit": "b" * 40,
                 "at": "2026-11-04T11:00:00Z"}
        seen = {v: {"tag": "v" + v, "commit": "a" * 40, "found": "x"} for v in ("1.0.0", "1.10.0", "1.9.0")}
        self.st.save_upstream_memo("tool", {"last_check": "2026-11-04T12:00:00Z", "last_ok": "2026-11-04T11:00:00Z",
                                            "last_error": "boom", "latest_version": "1.10.0", "seen": seen,
                                            "tag_moved": [moved]})
        entry = self.doc("upstreams")["sections"]["upstreams"][0]
        self.assertEqual((entry["latest_version"], entry["last_error"], entry["last_ok"]),
                         ("1.10.0", "boom", "2026-11-04T11:00:00Z"))
        self.assertEqual((entry["tag_moved"], entry["tag_moves"], entry["seen_count"]), (1, [moved], 3))
        self.assertEqual(entry["versions"], ["1.10.0", "1.9.0", "1.0.0"])

    def test_auto_task_comes_from_the_content(self):
        H.edit_yaml(os.path.join(self.cfg.content, "upstreams", "tool.yaml"), lambda d: dict(d, auto_task=True))
        self.assertTrue(self.doc("upstreams")["sections"]["upstreams"][0]["auto_task"])

    def test_state_without_content_is_still_listed(self):
        os.unlink(os.path.join(self.cfg.content, "upstreams", "tool.yaml"))
        os.unlink(os.path.join(self.cfg.content, "apps", "tool", "build.yaml"))
        self.st.save_upstream_memo("tool", {"latest_version": "1.0.0", "seen": {}})
        entry = self.doc("upstreams")["sections"]["upstreams"][0]
        self.assertEqual((entry["in_content"], entry["repo"], entry["latest_version"]), (False, None, "1.0.0"))

    def test_damaged_memo_is_an_error_not_a_crash(self):
        H.write(os.path.join(self.cfg.state, "upstreams", "tool.json"), b"{nope")
        doc = self.doc("upstreams")
        self.assertEqual(len(doc["sections"]["upstreams"]), 1)
        self.assertTrue([e for e in doc["errors"] if e["section"] == "upstreams"])


class TestBuildsSection(StatusBuildsCase):
    def test_fields_and_order(self):
        old = self.add_task(created="2026-11-04T09:00:00Z", status="done", candidate="1.0.0+agino1_aaaaaaaaaaaa",
                        artifact={"file": "x.deb", "sha256": "a" * 64, "size": 3}, finished="2026-11-04T09:30:00Z")
        new = self.add_task(created="2026-11-04T10:00:00Z", status="building", started="2026-11-04T10:01:00Z")
        builds = self.doc("builds")["sections"]["builds"]
        self.assertEqual([b["id"] for b in builds], [new, old])
        self.assertEqual(set(builds[0]), {"id", "app", "version", "upstream_version", "tag", "commit", "attempt",
                                          "status", "by", "created", "started", "finished", "error", "artifact",
                                          "candidate"})
        self.assertEqual(builds[1]["artifact"]["file"], "x.deb")
        text = json.dumps(builds)
        self.assertNotIn("must never appear", text)
        self.assertNotIn("secret-ish", text)

    def test_only_the_last_fifty(self):
        for i in range(55):
            self.add_task(created="2026-11-04T10:%02d:%02dZ" % (i // 60, i % 60))
        builds = self.doc("builds")["sections"]["builds"]
        self.assertEqual(len(builds), 50)
        self.assertEqual(builds[0]["created"], "2026-11-04T10:00:54Z")

    def test_sanitised(self):
        self.add_task(error="bad\x1b[31mred\x07")
        out = self.run_cli("status", "--json", "--section", "builds")[1]
        self.assertIn("bad?[31mred?", out)
        self.assertTrue(out.isascii())

    def test_damaged_record_is_reported(self):
        self.add_task()
        H.write(os.path.join(self.cfg.state, "builds", "0123456789abcdef.json"), b"{nope")
        doc = self.doc("builds")
        self.assertEqual(len(doc["sections"]["builds"]), 1)
        self.assertTrue([e for e in doc["errors"] if e["section"] == "builds"])

    def test_empty(self):
        self.assertEqual(self.doc("builds", "builder")["sections"], {"builds": [], "builder": {"running": None, "queued": 0}})


class TestBuilderSection(StatusBuildsCase):
    def test_running_and_queued(self):
        for status_name in ("queued", "fetching", "ready", "done", "failed", "cancelled", "collecting"):
            self.add_task(status=status_name)
        running = self.add_task(status="building")
        builder = self.doc("builder")["sections"]["builder"]
        self.assertEqual(builder, {"running": running, "queued": 3})


class TestCandidatesAndSchema(StatusBuildsCase):
    def test_built_origin_is_visible(self):
        self.add(app="tool", version="1.0.0+agino1", char="b", origin=dict(ORIGIN))
        cand = self.doc("candidates")["sections"]["candidates"][0]
        self.assertEqual(cand["origin"], ORIGIN)
        self.assertEqual(cand["source_class"], "personal")

    def test_schema_version_and_default_sections(self):
        doc = self.doc()
        self.assertEqual(doc["schema"], 3)
        for name in ("upstreams", "builds", "builder", "intake"):
            self.assertIn(name, doc["sections"])
        self.assertEqual(status.SCHEMA, 3)

    def test_read_only_and_lock_free(self):
        self.add_task(status="building")
        self.st.save_upstream_memo("tool", {"seen": {}})
        with self.st.lock():
            pass   # creates state/.lock
        base = os.path.dirname(self.conf)
        before = snapshot(base)
        with self.st.lock():                 # status must not need the lock
            self.doc()
        self.assertEqual(snapshot(base), before)

    def test_the_size_guard_can_shorten_builds(self):
        for i in range(40):
            self.add_task(created="2026-11-04T10:%02d:00Z" % i, error="x" * 200)
        with mock.patch.object(status, "MAX_BYTES", 4000):
            out = self.run_cli("status", "--json", "--section", "builds")[1]
        doc = json.loads(out)
        self.assertEqual(doc["truncated"], ["builds"])
        self.assertEqual(doc["sections"]["builds"][0]["created"], "2026-11-04T10:39:00Z")


class TestMetrics(StatusBuildsCase):
    def metrics(self):
        self.assertEqual(self.run_cli("metrics")[0], 0)
        with open(os.path.join(self.cfg.metrics, "agino_store_server.prom"), encoding="utf-8") as f:
            return f.read()

    def test_nothing_without_upstreams_or_builds(self):
        text = self.metrics()
        self.assertNotIn("agino_store_upstream", text)
        self.assertNotIn("agino_store_builds", text)

    def test_upstream_and_build_lines(self):
        self.st.save_upstream_memo("tool", {"last_ok": "2026-11-04T08:00:00Z", "last_error": "boom",
                                            "tag_moved": [{"version": "1.0.0"}]})
        self.add_task(status="failed")
        self.add_task(status="failed")
        self.add_task(status="queued")
        text = self.metrics()
        for line in ('agino_store_upstream_last_ok_timestamp_seconds{app="tool"} 1793779200',
                     'agino_store_upstream_last_error{app="tool"} 1', 'agino_store_upstream_tag_moved{app="tool"} 1',
                     'agino_store_builds{status="failed"} 2', 'agino_store_builds{status="queued"} 1',
                     'agino_store_builds{status="building"} 0'):
            self.assertIn(line + "\n", text)


if __name__ == "__main__":
    unittest.main()
