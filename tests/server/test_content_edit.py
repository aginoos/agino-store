# SPDX-License-Identifier: GPL-3.0-or-later
import fcntl
import json
import os
import unittest
from unittest import mock

from agino_store_server import cli, content, content_edit, content_git
from agino_store_server import content_patch as P

from . import helpers as H
from .edit_helpers import NEW_TEXT, EditCase, git, slurp

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "app-yaml")


class TestEditHappyPath(EditCase):
    def test_text_edit_marks_the_description_confirmed(self):
        self.set_app(lambda d: dict(d, description_source="ai"))
        before = self.head()
        code, out, err = self.edit(NEW_TEXT)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("edited gimp: summary, description", out)
        data = self.app_data()
        self.assertEqual(data["summary"], NEW_TEXT["summary"])
        self.assertEqual(data["description"], NEW_TEXT["description"])
        self.assertEqual(data["description_source"], "confirmed")
        self.assertEqual(data["description_confirmed"], {"by": "alice", "at": "2026-11-02T12:00:00Z"})
        keys = list(data)
        self.assertEqual(keys[keys.index("description"):][:3],
                         ["description", "description_source", "description_confirmed"])
        self.assertEqual(self.subject(), "info(gimp): summary, description by alice")
        self.assertEqual(git(self.content, "log", "-1", "--format=%an <%ae>"), "agino-store <agino-store@localhost>")
        self.assertEqual(git(self.content, "rev-parse", "HEAD~1"), before)
        self.assertEqual(git(self.content, "status", "--porcelain"), "")
        self.assertFalse(os.path.exists(self.staged("patch-0001.json")))
        content.load(self.content)

    def test_summary_only_edit_keeps_an_ai_description_unconfirmed(self):
        self.set_app(lambda d: dict(d, description_source="ai"))
        self.assertEqual(self.edit({"summary": NEW_TEXT["summary"]})[0], 0)
        data = self.app_data()
        self.assertEqual(data["summary"], NEW_TEXT["summary"])
        self.assertEqual(data["description_source"], "ai")
        self.assertNotIn("description_confirmed", data)
        self.assertTrue(cli._ai_description(content.load(self.content), "gimp"))   # approve is still refused

    def test_description_edit_keeps_official_and_submitter_marks(self):
        description = {"description": NEW_TEXT["description"]}
        self.set_app(lambda d: dict(d, description_source="official"))
        self.assertEqual(self.edit(description)[0], 0)
        data = self.app_data()
        self.assertEqual(data["description"], NEW_TEXT["description"])
        self.assertEqual(data["description_source"], "official")
        self.assertNotIn("description_confirmed", data)
        self.assertEqual(self.edit(description, app="chrome", name="patch-0002.json")[0], 0)
        data = self.app_data("chrome")
        self.assertEqual(data["description"], NEW_TEXT["description"])
        self.assertNotIn("description_source", data)   # submitter is the default and stays so
        self.assertNotIn("description_confirmed", data)

    def test_description_edit_of_a_confirmed_text_replaces_the_reviewer(self):
        self.set_app(lambda d: dict(d, description_source="confirmed",
                                    description_confirmed={"by": "bob", "at": "2026-01-01T00:00:00Z"}))
        self.assertEqual(self.edit({"description": NEW_TEXT["description"]})[0], 0)
        self.assertEqual(self.app_data()["description_confirmed"]["by"], "alice")

    def test_other_keys_replace_the_whole_value(self):
        patch = {"name": {"zh_CN": "吉姆普", "en_US": "GIMP 3"}, "categories": ["web", "graphics"],
                 "license": "LicenseRef-proprietary", "proprietary": True, "homepage": "https://example.org/gimp",
                 "developer": {"zh_CN": "团队", "en_US": "Team"}}
        self.assertEqual(self.edit(patch)[0], 0)
        data = self.app_data()
        for key, value in patch.items():
            self.assertEqual(data[key], value)
        self.assertEqual(data["package"], {"kind": "milkyway", "name": "gimp"})
        self.assertEqual(data["screenshots"]["zh_CN"], [{"image": "screenshots/zh_CN/1.png"}])
        self.assertNotIn("description_source", data)   # name/developer/etc. are not a reviewed text
        self.assertEqual(self.subject(), "info(gimp): name, categories, license, proprietary, homepage, developer by alice")

    def test_categories_only_keeps_the_description_source(self):
        self.set_app(lambda d: dict(d, description_source="ai"))
        self.assertEqual(self.edit({"categories": ["web"]})[0], 0)
        data = self.app_data()
        self.assertEqual((data["categories"], data["description_source"]), (["web"], "ai"))
        self.assertNotIn("description_confirmed", data)

    def test_other_languages_survive_a_localized_replace(self):
        self.set_app(lambda d: dict(d, summary=dict(d["summary"], de_DE="Bildeditor")))
        self.assertEqual(self.edit({"summary": NEW_TEXT["summary"]})[0], 0)
        self.assertEqual(self.app_data()["summary"], dict(NEW_TEXT["summary"], de_DE="Bildeditor"))

    def test_unchanged_patch_is_a_no_op(self):
        before = self.head()
        code, out, _ = self.edit({"license": "GPL-3.0-or-later"})
        self.assertEqual(code, 0)
        self.assertIn("no changes", out)
        self.assertEqual(self.head(), before)
        self.assertFalse(os.path.exists(self.staged("patch-0001.json")))

    def test_edit_keeps_the_file_style(self):
        self.edit({"license": "MIT"})
        text = slurp(self.gimp).decode("utf-8")
        self.assertIn("categories:\n- graphics\n", text)
        self.assertIn("license: MIT\n", text)
        self.assertIn("  zh_CN:\n  - 段落一\n", text)


class TestPatchRejected(EditCase):
    def rejected(self, patch, needle, **kw):
        before_file, before_head = slurp(self.gimp), self.head()
        code, _, err = self.edit(patch, **kw)
        self.assertEqual(code, 1, err)
        self.assertIn(needle, err)
        self.assertEqual((slurp(self.gimp), self.head()), (before_file, before_head))
        self.assertTrue(os.path.exists(self.staged("patch-0001.json")))   # left in place on failure

    def test_unknown_keys(self):
        self.rejected({"description_source": "confirmed"}, "unknown key")
        self.rejected({"package": {"kind": "deb"}}, "unknown key")
        self.rejected({"icon": "x.png"}, "unknown key")

    def test_categories(self):
        self.rejected({"categories": ["nope"]}, "unknown category")
        self.rejected({"categories": ["web", "graphics", "web"]}, "1..2")
        self.rejected({"categories": ["web", "web"]}, "duplicate")
        self.rejected({"categories": []}, "1..2")
        self.rejected({"categories": "web"}, "1..2")

    def test_markup_and_control_characters(self):
        self.rejected({"summary": {"zh_CN": "x<script>alert(1)</script>", "en_US": "ok"}}, "markup")
        self.rejected({"description": {"zh_CN": ["ok"], "en_US": ["fine </b>"]}}, "markup")
        self.rejected({"name": {"zh_CN": "a", "en_US": "b\x07"}}, "control characters")
        self.rejected({"description": {"zh_CN": ["ok"], "en_US": ["two\nlines"]}}, "control characters")

    def test_comparison_signs_are_not_markup(self):
        self.assertEqual(self.edit({"summary": {"zh_CN": "1 < 2 且 3 > 2", "en_US": "x <3 y"}})[0], 0)

    def test_localized_shape_and_limits(self):
        self.rejected({"summary": {"zh_CN": "只有中文"}}, "exactly zh_CN and en_US")
        self.rejected({"summary": {"zh_CN": "a", "en_US": "b", "fr_FR": "c"}}, "exactly zh_CN and en_US")
        self.rejected({"summary": {"zh_CN": "a" * 301, "en_US": "b"}}, "at most 300")
        self.rejected({"name": {"zh_CN": "a" * 101, "en_US": "b"}}, "at most 100")
        self.rejected({"description": {"zh_CN": ["a" * 4001], "en_US": ["b"]}}, "at most 4000")
        self.rejected({"description": {"zh_CN": ["a"] * 51, "en_US": ["b"]}}, "1..50")
        self.rejected({"description": {"zh_CN": [], "en_US": ["b"]}}, "1..50")
        self.rejected({"summary": "plain string"}, "mapping")

    def test_scalar_fields(self):
        self.rejected({"proprietary": "yes"}, "true or false")
        self.rejected({"homepage": "http://example.org/"}, "https")
        self.rejected({"homepage": "https://user:pw@example.org/"}, "https")
        self.rejected({"license": "GPL; rm -rf /"}, "bad format")
        self.rejected({"license": 3}, "non-empty string")

    def test_bad_json(self):
        for raw, needle in ((b"not json", "not valid"), (b"[]", "non-empty JSON object"), (b"{}", "non-empty"),
                            (b'{"license": "MIT", "license": "GPL-2.0-only"}', "duplicate"),
                            (b'{"proprietary": NaN}', "not allowed"), (b'{"license": "\xff"}', "UTF-8"),
                            (b'"just a string"', "non-empty JSON object")):
            with self.subTest(raw):
                code, _, err = self.edit(None, raw=raw)
                self.assertEqual(code, 1)
                self.assertIn(needle, err)
        self.assertEqual(git(self.content, "status", "--porcelain"), "")

    def test_oversized_patch_is_not_read(self):
        big = json.dumps({"summary": {"zh_CN": "a", "en_US": "b"}, "padding": "x" * (64 << 10)}).encode()
        code, _, err = self.edit(None, raw=big)
        self.assertEqual(code, 1)
        self.assertIn("larger than 65536", err)

    def test_staging_file_rules(self):
        outside = os.path.join(os.path.dirname(self.staging), "outside.json")
        H.write(outside, b'{"license": "MIT"}')
        os.symlink(outside, self.staged("linked-0001.json"))
        code, _, err = self.run_cli("content-edit", "gimp", "--patch", "linked-0001.json", "--editor", "alice")
        self.assertEqual(code, 1)
        self.assertIn("symlink", err)
        os.mkdir(self.staged("adir-00001.json"))
        self.assertEqual(self.run_cli("content-edit", "gimp", "--patch", "adir-00001.json", "--editor", "a")[0], 1)
        self.assertEqual(self.run_cli("content-edit", "gimp", "--patch", "missing-0001.json", "--editor", "a")[0], 1)
        for bad in ("../outside.json", "sub/patch-0001.json", "short.json", "patch-0001.JSON", "patch-0001.png",
                    "patch 0001.json", "x" * 70 + ".json", ".hidden-0001.json"):
            with self.subTest(bad):
                self.assertEqual(self.run_cli("content-edit", "gimp", "--patch", bad, "--editor", "alice")[0], 2)
        self.assertTrue(os.path.exists(outside))
        self.assertEqual(slurp(outside), b'{"license": "MIT"}')

    def test_bad_arguments(self):
        self.stage("patch-0001.json", b'{"license": "MIT"}')
        for app, editor in (("nope", "alice"), ("../gimp", "alice"), ("gimp", "bad name"), ("gimp", "x" * 33),
                            ("gimp", "-n"), ("gimp", "a;b")):
            with self.subTest((app, editor)):
                self.assertEqual(self.run_cli("content-edit", app, "--patch", "patch-0001.json",
                                              "--editor", editor)[0], 2)
        self.assertEqual(self.confirm("nope")[0], 2)
        self.assertEqual(self.confirm("gimp", "bad name")[0], 2)
        self.assertEqual(self.head(), self.origin_head())


class TestValidation(EditCase):
    def test_invalid_resulting_repository_restores_the_file(self):
        original, before = slurp(self.gimp), self.head()
        with mock.patch.object(P, "validate"):   # let a bad value through to the whole-repository check
            code, _, err = self.edit({"license": "bad/license!"})
        self.assertEqual(code, 1)
        self.assertIn("undone", err)
        self.assertIn("license", err)
        self.assertEqual((slurp(self.gimp), self.head()), (original, before))
        self.assertEqual(git(self.content, "status", "--porcelain"), "")
        self.assertTrue(os.path.exists(self.staged("patch-0001.json")))

    def test_already_invalid_repository_is_not_touched(self):
        os.unlink(os.path.join(self.content, "apps", "chrome", "icon.png"))
        self.commit_all("break")
        before = slurp(self.gimp)
        code, _, err = self.edit({"license": "MIT"})
        self.assertEqual(code, 1)
        self.assertIn("store-content is invalid", err)
        self.assertEqual(slurp(self.gimp), before)

    def test_uncommitted_change_in_the_app_file_is_refused(self):
        with open(self.gimp, "a", encoding="utf-8") as f:
            f.write("# hand edit\n")
        code, _, err = self.edit({"license": "MIT"})
        self.assertEqual(code, 1)
        self.assertIn("uncommitted", err)
        self.assertIn("# hand edit", slurp(self.gimp).decode())

    def test_not_a_git_checkout(self):
        import shutil
        shutil.rmtree(os.path.join(self.content, ".git"))
        code, _, err = self.edit({"license": "MIT"})
        self.assertEqual(code, 1)
        self.assertIn("content checkout", err)

    def test_lock_busy_exits_3(self):
        before = self.head()
        with self.st.lock():
            self.assertEqual(self.edit(NEW_TEXT)[0], cli.EXIT_LOCKED)
            self.assertEqual(self.confirm()[0], cli.EXIT_LOCKED)
        self.assertEqual(self.head(), before)
        self.assertTrue(os.path.exists(self.staged("patch-0001.json")))
        fd = os.open(os.path.join(self.cfg.state, ".lock"), os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertEqual(self.edit(NEW_TEXT)[0], cli.EXIT_LOCKED)


class TestConfirm(EditCase):
    def test_confirm_an_ai_description(self):
        self.set_app(lambda d: dict(d, description_source="ai"))
        code, out, err = self.confirm(editor="bob")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("confirmed", out)
        data = self.app_data()
        self.assertEqual(data["description_source"], "confirmed")
        self.assertEqual(data["description_confirmed"], {"by": "bob", "at": "2026-11-02T12:00:00Z"})
        self.assertEqual(self.subject(), "info(gimp): confirm description by bob")
        self.assertEqual(self.origin_head(), self.head())
        self.assertEqual(content.load(self.content).app("gimp").description_confirmed["by"], "bob")

    def test_already_confirmed_is_a_no_op(self):
        self.confirm()
        head = self.head()
        code, out, _ = self.confirm(editor="carol")
        self.assertEqual(code, 0)
        self.assertIn("already confirmed", out)
        self.assertEqual(self.head(), head)
        self.assertEqual(self.app_data()["description_confirmed"]["by"], "alice")

    def test_confirm_a_submitter_description(self):
        self.assertEqual(self.confirm("chrome")[0], 0)
        self.assertEqual(self.app_data("chrome")["description_source"], "confirmed")


class TestRoundTrip(unittest.TestCase):
    def test_dump_reproduces_the_store_content_files(self):
        import yaml
        for name in ("chrome", "audacity", "code"):
            with self.subTest(name):
                raw = slurp(os.path.join(DATA, name + ".yaml")).decode("utf-8")
                self.assertEqual(content_edit.dump(yaml.safe_load(raw)), raw)

    def test_flow_lists_are_the_only_known_difference(self):
        import yaml
        raw = slurp(os.path.join(DATA, "thunderbird.yaml")).decode("utf-8")
        out = content_edit.dump(yaml.safe_load(raw))
        self.assertEqual(yaml.safe_load(out), yaml.safe_load(raw))
        self.assertEqual(out.replace("zh_CN:\n  - thunderbird-l10n-zh-cn", "zh_CN: [thunderbird-l10n-zh-cn]"), raw)

    def test_patch_apply_does_not_modify_its_input(self):
        data = {"summary": {"zh_CN": "a", "en_US": "b"}, "description": {"zh_CN": ["x"], "en_US": ["y"]}}
        new, changed = P.apply(data, {"summary": {"zh_CN": "c", "en_US": "b"}})
        self.assertEqual((changed, data["summary"]["zh_CN"], new["summary"]["zh_CN"]), (["summary"], "a", "c"))
        marked = P.mark_confirmed(new, "bob", "2026-01-01T00:00:00Z")
        self.assertEqual(list(marked), ["summary", "description", "description_source", "description_confirmed"])
        self.assertNotIn("description_source", new)


class TestGitEnvironment(unittest.TestCase):
    def test_environment_is_reduced(self):
        with mock.patch.dict(os.environ, {"PATH": "/p", "HOME": "/h", "SECRET": "x", "GIT_DIR": "/elsewhere"}):
            self.assertEqual(content_git._env(""), {"PATH": "/p", "HOME": "/h", "GIT_TERMINAL_PROMPT": "0"})
            self.assertEqual(content_git._env("ssh -i k")["GIT_SSH_COMMAND"], "ssh -i k")

    def test_commands_have_a_timeout_and_no_shell(self):
        with mock.patch.object(content_git.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            content_git.run("/x", ["status"], "ssh")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["git", "-C", "/x"])
        self.assertEqual(run.call_args.kwargs["timeout"], 60)
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs["env"]["GIT_SSH_COMMAND"], "ssh")


if __name__ == "__main__":
    unittest.main()
