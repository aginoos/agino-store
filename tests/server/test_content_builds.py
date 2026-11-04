# SPDX-License-Identifier: GPL-3.0-or-later
import os
import unittest

from agino_store_server import buildcfg
from agino_store_server import content as C

from . import build_helpers as B
from . import helpers as H
from .test_content import ContentCase


class BuiltCase(ContentCase):
    def setUp(self):
        super().setUp()
        B.add_built_app(self.root)

    def edit_recipe(self, change):
        H.edit_yaml(self.path("apps", "tool", "build.yaml"), change)

    def edit_upstream(self, change):
        H.edit_yaml(self.path("upstreams", "tool.yaml"), change)


class TestValidBuilt(BuiltCase):
    def test_loads(self):
        content = C.load(self.root)
        up, recipe = content.upstream("tool"), content.recipe("tool")
        self.assertEqual((up.repo, up.track_type, up.min_version, up.auto_task),
                         ("https://github.com/example/tool.git", "tag", "1.0.0", False))
        self.assertEqual(up.build, "apps/tool/build.yaml")
        self.assertEqual(recipe.package["name"], "tool-agino")
        self.assertEqual(recipe.limits, buildcfg.Limits(cpu_minutes=60, memory_gib=4, disk_gib=10))
        self.assertEqual(len(recipe.steps), 3)
        app = content.app("tool")
        self.assertEqual((app.kind, app.source, app.vendor, app.package), ("deb", "personal", None, "tool-agino"))

    def test_auto_task_defaults_to_false_and_limits_to_the_ceiling(self):
        self.edit_upstream(lambda d: {k: v for k, v in d.items() if k != "auto_task"})
        self.edit_recipe(lambda d: {k: v for k, v in d.items() if k != "limits"})
        content = C.load(self.root)
        self.assertFalse(content.upstream("tool").auto_task)
        self.assertEqual(content.recipe("tool").limits, buildcfg.DEFAULT_LIMITS)

    def test_recipe_without_upstream_is_fine(self):
        os.unlink(self.path("upstreams", "tool.yaml"))
        self.assertIsNotNone(C.load(self.root).recipe("tool"))
        self.assertIsNone(C.load(self.root).upstream("tool"))

    def test_other_apps_are_unchanged(self):
        content = C.load(self.root)
        self.assertEqual([a.id for a in content.apps], ["chrome", "gimp", "tool"])
        self.assertEqual(content.app("chrome").vendor, "chrome")

    def test_recipe_does_not_change_the_catalog_digest(self):
        before = C.content_digest(self.root)
        self.edit_recipe(lambda d: dict(d, steps=["make"]))
        self.assertEqual(before, C.content_digest(self.root))

    def test_policy_hosts_and_limits(self):
        policy = buildcfg.Policy(("example.org",), buildcfg.Limits(30, 2, 5))
        with self.assertRaises(C.ContentError) as ctx:
            C.load(self.root, policy=policy)
        text = "\n".join(ctx.exception.errors)
        self.assertIn("host is not in [upstream] allowed_hosts", text)
        self.assertIn("limits.cpu_minutes", text)
        self.assertIn("limits.memory_gib", text)
        self.assertIn("limits.disk_gib", text)

    def test_local_repo_only_with_allow_insecure(self):
        self.edit_upstream(lambda d: dict(d, repo="file:///tmp/some/repo"))
        self.assertRejected("must be an https URL")
        self.assertEqual(C.load(self.root, allow_insecure=True).upstream("tool").repo, "file:///tmp/some/repo")


class TestInvalidUpstream(BuiltCase):
    def check(self, change, needle):
        self.edit_upstream(change)
        self.assertRejected(needle)

    def test_unknown_key(self):
        self.check(lambda d: dict(d, extra=1), "unknown key 'extra'")

    def test_missing_keys(self):
        for key in ("app", "repo", "track", "build"):
            with self.subTest(key=key):
                self.edit_upstream(lambda d: {k: v for k, v in d.items() if k != key})
                self.assertRejected("missing key %r" % key)
                B.add_built_app(self.root)

    def test_app_must_match_file_name(self):
        self.check(lambda d: dict(d, app="gimp"), "upstreams/tool.yaml.app: must be 'tool'")

    def test_repo_forms(self):
        for repo, needle in [
            ("http://github.com/example/tool.git", "must be an https URL"),
            ("ftp://github.com/example/tool.git", "must be an https URL"),
            ("git://github.com/example/tool.git", "must be an https URL"),
            ("ssh://git@github.com/example/tool.git", "must be an https URL"),
            ("file:///etc", "must be an https URL"),
            ("https://user:pw@github.com/example/tool.git", "without credentials"),
            ("https://github.com/example/tool.git?x=1", "without credentials"),
            ("https://github.com/example/tool.git#frag", "without credentials"),
            ("https://github.com:8443/example/tool.git", "without credentials"),
            ("https://evil.example.com/example/tool.git", "allowed_hosts"),
            ("https://github.com.evil.example/example/tool.git", "allowed_hosts"),
            ("https://127.0.0.1/example/tool.git", "allowed_hosts"),
            ("https://github.com/tool", "2..6 segments"),
            ("https://github.com/a/../b", "2..6 segments"),
            ("https://github.com/a/b c", "2..6 segments"),
            ("https://github.com/a/b%2e", "2..6 segments"),
            ("-https://github.com/a/b", "must be an https URL"),
            ("", "non-empty string"),
            (7, "non-empty string"),
        ]:
            with self.subTest(repo=repo):
                self.edit_upstream(lambda d: dict(d, repo=repo))
                self.assertRejected(needle)

    def test_track(self):
        for track, needle in [
            ({"type": "branch", "match": "^v(.*)$"}, "must be one of tag, release"),
            ({"type": "tag"}, "missing key 'match'"),
            ({"type": "tag", "match": "^v(.*)$", "x": 1}, "unknown key 'x'"),
            ({"type": "tag", "match": "^v.*$"}, "needs 1..6 capture groups"),
            ({"type": "tag", "match": "^v(("}, "invalid regular expression"),
            ({"type": "tag", "match": "^(a+)+$"}, "nested quantifiers"),
            ({"type": "tag", "match": "^(.*)*$"}, "nested quantifiers"),
            ({"type": "tag", "match": "(" + "a" * 250 + ")"}, "at most 200"),
            ({"type": "tag", "match": "^v(.*)$", "min_version": 3.0}, "quoted string"),
            ({"type": "tag", "match": "^v(.*)$", "min_version": "abc"}, "bad format"),
            ({"type": "tag", "match": "^v(.*)$", "min_version": "1.0 && x"}, "bad format"),
            ("tag", "must be a mapping"),
        ]:
            with self.subTest(track=track):
                self.edit_upstream(lambda d: dict(d, track=track))
                self.assertRejected(needle)

    def test_build_path_must_be_the_recipe_of_the_app(self):
        self.check(lambda d: dict(d, build="apps/gimp/build.yaml"), "must be 'apps/tool/build.yaml'")
        self.check(lambda d: dict(d, build="../x.yaml"), "must be 'apps/tool/build.yaml'")

    def test_auto_task_must_be_boolean(self):
        self.check(lambda d: dict(d, auto_task="yes please"), "must be true or false")

    def test_needs_a_valid_recipe(self):
        os.unlink(self.path("apps", "tool", "build.yaml"))
        self.assertRejected("apps/tool/build.yaml is missing or invalid")

    def test_unknown_app_and_stray_files(self):
        H.write_yaml(self.path("upstreams", "nope.yaml"), B.UPSTREAM)
        H.write(self.path("upstreams", "README.md"), b"x")
        text = self.errors()
        self.assertIn("upstreams/nope.yaml: must be <app-id>.yaml of an existing app", text)
        self.assertIn("upstreams/README.md", text)


class TestInvalidRecipe(BuiltCase):
    def check(self, change, needle):
        self.edit_recipe(change)
        self.assertRejected(needle)

    def test_unknown_and_missing_keys(self):
        self.check(lambda d: dict(d, extra=1), "unknown key 'extra'")
        for key in ("base", "source", "build_depends", "steps", "package"):
            with self.subTest(key=key):
                self.edit_recipe(lambda d: {k: v for k, v in d.items() if k != key})
                self.assertRejected("missing key %r" % key)
                B.add_built_app(self.root)

    def test_base(self):
        self.check(lambda d: dict(d, base="ubuntu-noble"), "must be one of debian-trixie")

    def test_source(self):
        for source, needle in [
            ({"kind": "svn"}, "must be one of git, tarball"),
            ({"kind": "git", "checksum": "abc"}, "only 'checksum: required'"),
            ({"kind": "git", "url": "https://github.com/a/b.tar.gz"}, "takes no url"),
            ({"kind": "git", "extra": 1}, "unknown key 'extra'"),
            ({"kind": "tarball"}, "needs the sha256"),
            ({"kind": "tarball", "checksum": "a" * 64}, "needs the https url"),
            ({"kind": "tarball", "checksum": "a" * 63, "url": "https://github.com/a/b.tar.gz"}, "bad format"),
            ({"kind": "tarball", "checksum": "A" * 64, "url": "https://github.com/a/b.tar.gz"}, "bad format"),
            ({"kind": "tarball", "checksum": "a" * 64, "url": "http://github.com/a/b.tar.gz"}, "must be an https URL"),
            ({"kind": "tarball", "checksum": "a" * 64, "url": "https://evil.example/a/b.tar.gz"}, "allowed_hosts"),
            ("git", "must be a mapping"),
        ]:
            with self.subTest(source=source):
                self.edit_recipe(lambda d: dict(d, source=source))
                self.assertRejected(needle)

    def test_valid_tarball(self):
        source = {"kind": "tarball", "checksum": "a" * 64, "url": "https://github.com/a/b/archive/v1.tar.gz"}
        self.edit_recipe(lambda d: dict(d, source=source))
        self.assertEqual(C.load(self.root).recipe("tool").source["url"], source["url"])

    def test_build_depends(self):
        for depends, needle in [
            (["Bad_Name"], "bad format"), (["a"], "bad format"), (["-x"], "bad format"),
            (["meson", "meson"], "duplicate package"), (["meson; rm -rf /"], "bad format"),
            (["x" * 129], "at most 128 characters"), (["ok-pkg"] * 101, "0..100 items"), ("meson", "list of 0..100"),
            ([1], "non-empty string"),
        ]:
            with self.subTest(depends=depends):
                self.edit_recipe(lambda d: dict(d, build_depends=depends))
                self.assertRejected(needle)

    def test_empty_build_depends_is_fine(self):
        self.edit_recipe(lambda d: dict(d, build_depends=[]))
        self.assertEqual(C.load(self.root).recipe("tool").build_depends, ())

    def test_steps(self):
        for steps, needle in [
            ([], "1..40 items"), (["x"] * 41, "1..40 items"), (["x" * 513], "at most 512"),
            (["a\nb"], "single line"), (["a\x07b"], "control characters"), ([""], "non-empty string"),
            ([None], "non-empty string"), ("make", "list of 1..40"), ([["make"]], "non-empty string"),
        ]:
            with self.subTest(steps=steps):
                self.edit_recipe(lambda d: dict(d, steps=steps))
                self.assertRejected(needle)

    def test_steps_at_the_limits_are_fine(self):
        self.edit_recipe(lambda d: dict(d, steps=["x" * 512] * 40))
        self.assertEqual(len(C.load(self.root).recipe("tool").steps), 40)

    def test_package(self):
        base = B.RECIPE["package"]
        for package, needle in [
            (dict(base, name="Bad"), "package.name: bad format"),
            (dict(base, name="x" * 129), "at most 128"),
            (dict(base, section="Graphics!"), "section: bad format"),
            (dict(base, maintainer="no address"), "maintainer: bad format"),
            (dict(base, maintainer="a <b@c>\n"), "maintainer"),
            (dict(base, depends=["Bad Dep"]), "bad format"),
            (dict(base, depends="libc6"), "list of 0..50"),
            (dict(base, extra=1), "unknown key 'extra'"),
            ({k: v for k, v in base.items() if k != "section"}, "missing key 'section'"),
        ]:
            with self.subTest(package=package):
                self.edit_recipe(lambda d: dict(d, package=package))
                self.assertRejected(needle)

    def test_package_name_must_match_the_app(self):
        self.edit_recipe(lambda d: dict(d, package=dict(d["package"], name="other-name")))
        self.assertRejected("must equal the recipe package 'other-name'")

    def test_limits(self):
        for limits, needle in [
            ({"cpu_minutes": 91}, "limits.cpu_minutes"), ({"memory_gib": 9}, "limits.memory_gib"),
            ({"disk_gib": 21}, "limits.disk_gib"), ({"cpu_minutes": 0}, "limits.cpu_minutes"),
            ({"cpu_minutes": -1}, "limits.cpu_minutes"), ({"cpu_minutes": "60"}, "limits.cpu_minutes"),
            ({"cpu_minutes": True}, "limits.cpu_minutes"), ({"cpu_minutes": 1.5}, "limits.cpu_minutes"),
            ({"gpu": 1}, "unknown key 'gpu'"), (90, "must be a mapping"),
        ]:
            with self.subTest(limits=limits):
                self.edit_recipe(lambda d: dict(d, limits=limits))
                self.assertRejected(needle)

    def test_limits_at_the_ceiling_are_fine(self):
        self.edit_recipe(lambda d: dict(d, limits={"cpu_minutes": 90, "memory_gib": 8, "disk_gib": 20}))
        self.assertEqual(C.load(self.root).recipe("tool").limits, buildcfg.DEFAULT_LIMITS)

    def test_recipe_must_be_a_regular_file(self):
        os.unlink(self.path("apps", "tool", "build.yaml"))
        os.symlink(self.path("apps", "gimp", "icon.svg"), self.path("apps", "tool", "build.yaml"))
        self.assertRejected("must be a regular file")


class TestAppRules(BuiltCase):
    def edit_tool(self, change):
        H.edit_yaml(self.path("apps", "tool", "app.yaml"), change)

    def test_source_must_be_personal(self):
        self.edit_tool(lambda d: dict(d, source="official"))
        self.assertRejected("needs kind deb and source personal")

    def test_maintainer_is_required(self):
        self.edit_tool(lambda d: {k: v for k, v in d.items() if k != "maintainer"})
        self.assertRejected("maintainer: required for source personal")

    def test_no_vendor_for_built_apps(self):
        self.edit_tool(lambda d: dict(d, package={"kind": "deb", "name": "tool-agino", "vendor": "chrome"}))
        self.assertRejected("must not be set for an app built from source")

    def test_kind_must_be_deb(self):
        self.edit_tool(lambda d: dict(d, source="system", package={"kind": "milkyway", "name": "tool-agino"}))
        self.assertRejected("needs kind deb and source personal")

    def test_deb_app_without_recipe_still_needs_a_vendor(self):
        os.unlink(self.path("apps", "tool", "build.yaml"))
        os.unlink(self.path("upstreams", "tool.yaml"))
        self.assertRejected("unknown vendor None")


if __name__ == "__main__":
    unittest.main()
