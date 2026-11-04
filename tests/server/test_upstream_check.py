# SPDX-License-Identifier: GPL-3.0-or-later
import os
import unittest
from unittest import mock

from agino_store_server import upstreams

from . import build_helpers as B
from . import helpers as H
from .build_case import GIT, BuildCase


def edit_upstream(case, change):
    H.edit_yaml(os.path.join(case.cfg.content, "upstreams", "tool.yaml"), change)


@unittest.skipUnless(GIT, "git not available")
class TestUpstreamCheck(BuildCase):
    def tag(self, name, data=b"x", move=False):
        """Commit something new and tag it (or move an existing tag onto it)."""
        commit = B.commit_more(self.repo, "f-" + name, data)
        B.git(self.repo, "tag", *(["-f"] if move else []), name)
        return commit

    def check(self, *extra):
        return self.run_cli("upstream-check", *extra)

    def test_first_check_records_the_baseline(self):
        code, out, err = self.check()
        self.assertEqual((code, out.strip(), err), (0, "tool: latest 1.0.0, 1 new", ""))
        memo = self.memo()
        self.assertEqual(memo["seen"]["1.0.0"]["tag"], "v1.0.0")
        self.assertEqual(memo["seen"]["1.0.0"]["commit"], self.commit)
        self.assertEqual((memo["latest_version"], memo["last_error"], memo["tag_moved"]), ("1.0.0", None, []))
        self.assertTrue(memo["last_check"] and memo["last_ok"])

    def test_only_matching_tags_at_or_above_min_version_are_seen(self):
        for name in ("v2.0.0-rc1", "foo", "v0.9.0", "xv1.5.0", "v1.5", "v1.5.0.1"):
            self.tag(name)
        self.tag("v1.5.0")
        self.check()
        self.assertEqual(sorted(self.memo()["seen"]), ["1.0.0", "1.5.0"])
        self.assertEqual(self.memo()["latest_version"], "1.5.0")

    def test_new_versions_are_found(self):
        self.check()
        self.tag("v1.1.0")
        self.tag("v1.10.0")
        self.tag("v1.9.0")
        code, out, _ = self.check()
        self.assertEqual((code, out.strip()), (0, "tool: latest 1.10.0, 3 new"))
        self.assertEqual(sorted(self.memo()["seen"]), ["1.0.0", "1.1.0", "1.10.0", "1.9.0"])

    def test_annotated_tags_pin_the_commit(self):
        B.commit_more(self.repo, "a", b"1")
        B.git(self.repo, "tag", "-a", "-m", "release", "v1.2.0")
        self.check()
        self.assertEqual(self.memo()["seen"]["1.2.0"]["commit"], B.git(self.repo, "rev-parse", "HEAD"))

    def test_seen_is_capped(self):
        for i in range(1, 5):
            self.tag("v1.%d.0" % i)
        with mock.patch.object(upstreams, "MAX_SEEN", 2):
            self.check()
        self.assertEqual(sorted(self.memo()["seen"]), ["1.3.0", "1.4.0"])

    def test_moved_tag_is_recorded_and_fails(self):
        self.check()
        moved_to = self.tag("v1.0.0", move=True)
        code, out, _ = self.check()
        self.assertEqual(code, 1)
        self.assertIn("TAG MOVED 1.0.0 (v1.0.0)", out)
        memo = self.memo()
        self.assertEqual(memo["seen"]["1.0.0"]["commit"], self.commit)   # the pin stays
        self.assertEqual(len(memo["tag_moved"]), 1)
        self.assertEqual({k: memo["tag_moved"][0][k] for k in ("version", "tag", "old_commit", "new_commit")},
                         {"version": "1.0.0", "tag": "v1.0.0", "old_commit": self.commit, "new_commit": moved_to})
        self.assertEqual(self.check()[0], 0)    # told once; the record stays
        self.assertEqual(len(self.memo()["tag_moved"]), 1)

    def test_a_moved_tag_cannot_be_built(self):
        self.check()
        self.tag("v1.0.0", move=True)
        self.check()
        code, _, err = self.run_cli("build-request", "tool", "--version", "1.0.0", "--by", "alice")
        self.assertEqual(code, 1)
        self.assertIn("moved", err)

    def test_unreachable_repository_is_an_error_and_keeps_the_memo(self):
        self.check()
        before = self.memo()
        edit_upstream(self, lambda d: dict(d, repo="file://" + os.path.join(self._tmp.name, "gone")))
        code, out, _ = self.check()
        self.assertEqual(code, 1)
        self.assertIn("tool: error:", out)
        memo = self.memo()
        self.assertTrue(memo["last_error"])
        self.assertEqual((memo["seen"], memo["last_ok"], memo["latest_version"]),
                         (before["seen"], before["last_ok"], before["latest_version"]))

    def test_release_tracking_is_not_implemented(self):
        edit_upstream(self, lambda d: dict(d, track=dict(d["track"], type="release")))
        code, out, _ = self.check()
        self.assertEqual(code, 1)
        self.assertIn("not implemented", out)

    def test_app_selection(self):
        self.assertEqual(self.check("--app", "tool")[0], 0)
        self.assertEqual(self.check("--app", "gimp")[0], 2)        # no upstream file
        self.assertEqual(self.check("--app", "Bad Id")[0], 2)

    def test_invalid_content_is_reported(self):
        H.write(os.path.join(self.cfg.content, "upstreams", "tool.yaml"), "app: [", "w")
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("store-content is invalid", err)

    def test_the_state_lock_is_taken(self):
        with self.st.lock():
            self.assertEqual(self.check()[0], 3)


@unittest.skipUnless(GIT, "git not available")
class TestAutoTask(BuildCase):
    def setUp(self):
        super().setUp()
        edit_upstream(self, lambda d: dict(d, auto_task=True))

    def tag(self, name):
        B.commit_more(self.repo, "f-" + name, b"x")
        B.git(self.repo, "tag", name)

    def test_first_check_only_records_the_baseline(self):
        self.assertEqual(self.run_cli("upstream-check")[0], 0)
        self.assertEqual(self.st.builds(), [])

    def test_a_new_version_creates_a_task_by_auto(self):
        self.run_cli("upstream-check")
        self.tag("v1.1.0")
        code, out, _ = self.run_cli("upstream-check")
        self.assertEqual(code, 0)
        tasks = self.st.builds()
        self.assertEqual(len(tasks), 1)
        self.assertEqual((tasks[0]["by"], tasks[0]["upstream_version"], tasks[0]["version"], tasks[0]["status"]),
                         ("auto", "1.1.0", "1.1.0+agino1", "queued"))
        self.assertIn("build task %s requested" % tasks[0]["id"], out)
        self.run_cli("upstream-check")
        self.assertEqual(len(self.st.builds()), 1)

    def test_only_the_newest_of_several_new_versions_is_requested(self):
        self.run_cli("upstream-check")
        for name in ("v1.1.0", "v1.3.0", "v1.2.0"):
            self.tag(name)
        self.run_cli("upstream-check")
        self.assertEqual([t["upstream_version"] for t in self.st.builds()], ["1.3.0"])

    def test_an_old_tag_appearing_late_is_not_built(self):
        self.run_cli("upstream-check")
        self.tag("v1.0.1")
        self.run_cli("upstream-check")
        self.set_status(self.st.builds()[0]["id"], status="done")
        edit_upstream(self, lambda d: dict(d, track=dict(d["track"], min_version="0.5.0")))
        self.tag("v0.9.0")
        self.run_cli("upstream-check")
        self.assertEqual(len(self.st.builds()), 1)

    def test_an_unfinished_task_blocks_a_second_one_quietly(self):
        self.run_cli("upstream-check")
        self.tag("v1.1.0")
        self.run_cli("upstream-check")
        out = ""
        with mock.patch.object(upstreams, "_auto_candidate", return_value="1.1.0"):
            code, out, _ = self.run_cli("upstream-check")
        self.assertEqual(code, 0)
        self.assertIn("no build task", out)
        self.assertEqual(len(self.st.builds()), 1)

    def test_no_task_when_auto_task_is_off(self):
        edit_upstream(self, lambda d: dict(d, auto_task=False))
        self.run_cli("upstream-check")
        self.tag("v1.1.0")
        self.run_cli("upstream-check")
        self.assertEqual(self.st.builds(), [])


class TestPickVersions(unittest.TestCase):
    """The pure parts, without git."""

    def test_prefers_the_recorded_tag_for_a_version(self):
        up = type("Up", (), {"match": r"^v?(\d+)\.(\d+)$", "min_version": None})
        tags = {"1.2": "a" * 40, "v1.2": "b" * 40}
        self.assertEqual(upstreams.pick_versions(up, tags, {}), {"1.2": ("1.2", "a" * 40)})
        self.assertEqual(upstreams.pick_versions(up, tags, {"1.2": {"tag": "v1.2"}}), {"1.2": ("v1.2", "b" * 40)})

    def test_merge_marks_moves_once(self):
        now = "2026-11-04T10:00:00Z"
        memo = {"seen": {"1.0.0": {"tag": "v1.0.0", "commit": "a" * 40, "found": "x"}}}
        found = {"1.0.0": ("v1.0.0", "b" * 40), "1.1.0": ("v1.1.0", "c" * 40)}
        seen, moved, new, moves = upstreams.merge(memo, found, now)
        self.assertEqual((sorted(seen), new, len(moved), len(moves)), (["1.0.0", "1.1.0"], ["1.1.0"], 1, 1))
        self.assertEqual(seen["1.0.0"]["commit"], "a" * 40)
        again = upstreams.merge(dict(memo, tag_moved=moved), found, now)
        self.assertEqual(again[3], [])

    def test_a_different_tag_for_a_seen_version_is_ignored(self):
        memo = {"seen": {"1.0.0": {"tag": "v1.0.0", "commit": "a" * 40, "found": "x"}}}
        seen, moved, new, moves = upstreams.merge(memo, {"1.0.0": ("1.0.0", "b" * 40)}, "t")
        self.assertEqual((moved, new, moves, seen["1.0.0"]["tag"]), ([], [], [], "v1.0.0"))


if __name__ == "__main__":
    unittest.main()
