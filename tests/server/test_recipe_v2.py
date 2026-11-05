# SPDX-License-Identifier: GPL-3.0-or-later
"""Recipe v2 (sources, files, test, evidence, recipe_version, derived_from) in apps/<id>/build.yaml."""

import copy
import os

from agino_store_server import buildcfg, recipe_v2
from agino_store_server import content as C

from . import build_helpers as B
from . import helpers as H
from .test_content_builds import BuiltCase

SHA = "a" * 64
GIT = {"name": "src", "kind": "git", "url": "https://github.com/example/tool.git", "ref": "v${VERSION}"}
TARBALL = {"name": "vendor", "kind": "tarball", "url": "https://github.com/example/tool/releases/download/v${VERSION}/tool_${VERSION}.tar.gz",
           "sha256": SHA}
BLOB = {"name": "blob", "kind": "file", "url": "https://codeberg.org/example/tool/app.AppImage", "sha256": SHA}


def v2(recipe, **changes):
    """The v1 test recipe turned into a v2 recipe (sources instead of source), with `changes` on top."""
    data = {k: v for k, v in copy.deepcopy(recipe).items() if k != "source"}
    data["sources"] = [copy.deepcopy(GIT)]
    data.update(copy.deepcopy(changes))
    return data


class V2Case(BuiltCase):
    def set_recipe(self, **changes):
        self.edit_recipe(lambda d: v2(d, **changes))

    def load_recipe(self):
        return C.load(self.root).recipe("tool")

    def assertBad(self, needle, **changes):
        self.set_recipe(**changes)
        with self.assertRaises(C.ContentError) as ctx:
            C.load(self.root)
        self.assertIn(needle, str(ctx.exception))


class TestValid(V2Case):
    def test_minimal_v2(self):
        self.set_recipe()
        recipe = self.load_recipe()
        self.assertTrue(recipe.is_v2)
        self.assertIsNone(recipe.source)
        self.assertEqual(recipe.sources[0]["name"], "src")

    def test_everything(self):
        self.set_recipe(
            sources=[GIT, TARBALL, BLOB], recipe_version=3, derived_from=2,
            files=[{"path": "agino/launcher.sh", "mode": "0755", "content": "#!/bin/sh\nexec tool \"$@\"\n"},
                   {"path": "tool.desktop", "content": "[Desktop Entry]\n"}],
            test=["tool --version", "timeout 20 tool >/dev/null 2>&1 || test $? -eq 124"],
            evidence=[{"claim": "license is GPL-3.0", "url": "https://github.com/example/tool/blob/main/COPYING"}])
        recipe = self.load_recipe()
        self.assertEqual([s["name"] for s in recipe.sources], ["src", "vendor", "blob"])
        self.assertEqual((recipe.recipe_version, recipe.derived_from), (3, 2))
        self.assertEqual([f["mode"] for f in recipe.files], ["0755", "0644"])
        self.assertEqual(len(recipe.test), 2)
        self.assertEqual(recipe.evidence[0]["claim"], "license is GPL-3.0")

    def test_requirements_are_kept_and_not_part_of_the_task(self):
        self.set_recipe(requirements="Leave out the Qt5 plugin.\nUse the --disable-docs option.")
        recipe = self.load_recipe()
        self.assertEqual(recipe.requirements, "Leave out the Qt5 plugin.\nUse the --disable-docs option.")
        self.assertNotIn("requirements", recipe.as_task("1.0", "v1.0", "0" * 40))
        self.edit_recipe(lambda d: {k: v for k, v in d.items() if k != "requirements"})
        self.assertEqual(self.load_recipe().requirements, "")

    def test_requirements_must_be_short_plain_text(self):
        self.assertBad("requirements", requirements="x" * 1001)
        self.assertBad("requirements", requirements="   ")
        self.assertBad("requirements", requirements="a\x07b")
        self.assertBad("requirements", requirements=["a list"])

    def test_v1_with_the_new_scalar_keys(self):
        self.edit_recipe(lambda d: dict(d, test=["tool --version"], recipe_version=1, evidence=[]))
        recipe = self.load_recipe()
        self.assertFalse(recipe.is_v2)
        self.assertEqual((recipe.test, recipe.recipe_version), (("tool --version",), 1))

    def test_unquoted_yaml_modes_are_accepted(self):
        # YAML reads an unquoted 0755 as the integer 493
        self.set_recipe(files=[{"path": "a.sh", "mode": 0o755, "content": "x\n"}])
        self.assertEqual(self.load_recipe().files[0]["mode"], "0755")

    def test_as_task_v1_is_unchanged(self):
        recipe = C.load(self.root).recipe("tool")
        self.assertEqual(sorted(recipe.as_task()), ["base", "build_depends", "limits", "package", "source", "steps"])

    def test_as_task_v2_replaces_placeholders_and_lists_only_known_keys(self):
        self.set_recipe(sources=[GIT, TARBALL], test=["tool --version"], recipe_version=2)
        task = self.load_recipe().as_task("1.2.3", "v1.2.3", "c" * 40)
        self.assertNotIn("source", task)
        self.assertEqual(task["sources"][0]["ref"], "v1.2.3")
        self.assertTrue(task["sources"][1]["url"].endswith("/v1.2.3/tool_1.2.3.tar.gz"))
        self.assertEqual(sorted(task), ["base", "build_depends", "limits", "package", "recipe_version", "sources",
                                        "steps", "test"])
        self.assertEqual(sorted(task["sources"][0]), ["kind", "name", "ref", "url"])
        self.assertEqual(sorted(task["sources"][1]), ["kind", "name", "sha256", "url"])

    def test_commit_placeholder_in_a_url(self):
        blob = dict(BLOB, url="https://github.com/example/tool/raw/${COMMIT}/app.bin")
        self.set_recipe(sources=[GIT, blob])
        task = self.load_recipe().as_task("1.0", "v1.0", "d" * 40)
        self.assertIn("d" * 40, task["sources"][1]["url"])

    def test_missing_value_for_a_placeholder(self):
        self.set_recipe(sources=[dict(GIT, ref="${COMMIT}")])
        with self.assertRaises(recipe_v2.ExpandError):
            self.load_recipe().as_task("1.0", "v1.0", None)

    def test_expansion_that_breaks_the_url_is_refused(self):
        self.set_recipe(sources=[GIT, TARBALL])
        with self.assertRaises(recipe_v2.ExpandError):
            self.load_recipe().as_task("1.0/../../x", "v1", "c" * 40)


class TestSourceExclusivity(V2Case):
    def test_both(self):
        self.edit_recipe(lambda d: dict(d, sources=[GIT]))
        with self.assertRaises(C.ContentError) as ctx:
            C.load(self.root)
        self.assertIn("cannot both be set", str(ctx.exception))

    def test_neither(self):
        self.edit_recipe(lambda d: {k: v for k, v in d.items() if k != "source"})
        with self.assertRaises(C.ContentError) as ctx:
            C.load(self.root)
        self.assertIn("missing key 'source' (or 'sources')", str(ctx.exception))


class TestSources(V2Case):
    def test_bad_lists(self):
        for sources, needle in [
            ([], "must be a list of 1..8 items"),
            ([dict(GIT, name="s%d" % i) for i in range(9)], "must be a list of 1..8 items"),
            ("git", "must be a list"),
            ([GIT, GIT], "duplicate source name"),
            (["x"], "must be a mapping"),
        ]:
            with self.subTest(sources=sources):
                self.assertBad(needle, sources=sources)
                B.add_built_app(self.root)

    def test_eight_sources_are_fine(self):
        self.set_recipe(sources=[dict(GIT, name="s%d" % i) for i in range(8)])
        self.assertEqual(len(self.load_recipe().sources), 8)

    def test_bad_entries(self):
        for item, needle in [
            (dict(GIT, name="Src"), "bad format"),
            (dict(GIT, name="1src"), "bad format"),
            (dict(GIT, name="a" * 33), "at most 32"),
            (dict(GIT, kind="svn"), "must be one of git, tarball, file"),
            (dict(GIT, extra=1), "unknown key 'extra'"),
            ({k: v for k, v in GIT.items() if k != "url"}, "missing key 'url'"),
            ({k: v for k, v in GIT.items() if k != "ref"}, "needs a ref"),
            (dict(GIT, sha256=SHA), "not allowed for kind git"),
            ({k: v for k, v in TARBALL.items() if k != "sha256"}, "needs the sha256"),
            ({k: v for k, v in BLOB.items() if k != "sha256"}, "needs the sha256"),
            (dict(TARBALL, sha256="A" * 64), "bad format"),
            (dict(TARBALL, sha256="a" * 63), "bad format"),
            (dict(TARBALL, ref="v1"), "only allowed for kind git"),
        ]:
            with self.subTest(item=item):
                self.assertBad(needle, sources=[item])
                B.add_built_app(self.root)

    def test_bad_urls(self):
        for url, needle in [
            ("http://github.com/example/tool.git", "must be an https URL"),
            ("https://evil.example/example/tool.git", "allowed_hosts"),
            ("https://user:pw@github.com/example/tool.git", "without credentials"),
            ("https://github.com/example/tool.git?x=1", "without credentials, query or fragment"),
            ("https://github.com/example/tool.git#frag", "without credentials, query or fragment"),
            ("https://github.com:8443/example/tool.git", "without credentials"),
            ("https://github.com/tool.git", "2..6 segments"),
            ("https://github.com/a/../b.git", "2..6 segments"),
            ("https://${VERSION}.github.com/example/tool.git", "not allowed in the host part"),
            ("https://github.com${VERSION}/example/tool.git", "not allowed in the host part"),
            ("ftp://github.com/example/tool.git", "must be an https URL"),
            ("file:///etc/passwd", "must be an https URL"),
            ("https://github.com/example/tool.git\nX", "single line"),
            ("https://github.com/exa mple/tool.git", "2..6 segments"),
            ("https://github.com/example/${EVIL}.git", "2..6 segments"),
            ("https://github.com/" + "a/" * 8 + "b.git", "2..6 segments"),
            ("", "non-empty string"),
            ("x" * 500, "at most 400"),
        ]:
            with self.subTest(url=url):
                self.assertBad(needle, sources=[dict(GIT, url=url)])
                B.add_built_app(self.root)

    def test_bad_download_urls(self):
        for url, needle in [
            ("http://github.com/a/app.tar.gz", "must be an https URL"),
            ("https://evil.example/app.tar.gz", "allowed_hosts"),
            ("https://github.com/app.tar.gz?token=1", "without credentials, query or fragment"),
            ("https://github.com/", "1..12 segments"),
            ("https://github.com", "1..12 segments"),
            ("https://github.com/a//b.tar.gz", "1..12 segments"),
            ("https://github.com/a/../b.tar.gz", "1..12 segments"),
            ("https://github.com/a/b%20c.tar.gz", "1..12 segments"),
            ("https://github.com/" + "a/" * 13 + "b.tar.gz", "1..12 segments"),
            ("https://github.com/a/${NOPE}.tar.gz", "1..12 segments"),
        ]:
            with self.subTest(url=url):
                self.assertBad(needle, sources=[dict(TARBALL, url=url)])
                B.add_built_app(self.root)

    def test_bad_refs(self):
        for ref, needle in [
            ("", "non-empty string"), ("-x", "must be a tag name"), ("a..b", "must be a tag name"),
            ("a b", "must be a tag name"), ("a//b", "must be a tag name"), ("v1/", "must be a tag name"),
            ("v1.lock", "must be a tag name"), ("v1;rm", "must be a tag name"), ("$(id)", "must be a tag name"),
            ("v${OTHER}", "must be a tag name"), ("a" * 201, "at most 200"),
        ]:
            with self.subTest(ref=ref):
                self.assertBad(needle, sources=[dict(GIT, ref=ref)])
                B.add_built_app(self.root)

    def test_good_refs(self):
        for ref in ("v1.2.3", "release/1.2", "${TAG}", "${COMMIT}", "v${VERSION}", "a" * 40):
            with self.subTest(ref=ref):
                self.set_recipe(sources=[dict(GIT, ref=ref)])
                self.assertEqual(self.load_recipe().sources[0]["ref"], ref)

    def test_local_http_only_with_allow_insecure(self):
        self.set_recipe(sources=[dict(BLOB, url="http://github.com/a/app.bin")])
        with self.assertRaises(C.ContentError):
            C.load(self.root)
        policy = buildcfg.Policy(buildcfg.DEFAULT_UPSTREAM_HOSTS, buildcfg.DEFAULT_LIMITS)
        self.assertTrue(C.load(self.root, allow_insecure=True, policy=policy).recipe("tool"))

    def test_extra_host_through_the_policy(self):
        blob = dict(BLOB, url="https://downloads.vendor.example/pub/app.bin")
        self.set_recipe(sources=[GIT, blob])
        with self.assertRaises(C.ContentError):
            C.load(self.root)
        policy = buildcfg.Policy(buildcfg.DEFAULT_UPSTREAM_HOSTS + ("downloads.vendor.example",),
                                 buildcfg.DEFAULT_LIMITS)
        self.assertEqual(len(C.load(self.root, policy=policy).recipe("tool").sources), 2)


class TestPlaceholders(V2Case):
    def test_only_in_sources(self):
        for key, value in [
            ("steps", ["make VERSION=${VERSION}"]),
            ("test", ["tool --version | grep ${VERSION}"]),
            ("build_depends", ["pkg-${TAG}"]),
            ("evidence", [{"claim": "version ${VERSION}", "url": "https://github.com/a/b"}]),
            ("files", [{"path": "a.sh", "content": "echo ${COMMIT}\n"}]),
            ("package", dict(B.RECIPE["package"], section="${TAG}")),
        ]:
            with self.subTest(key=key):
                self.assertBad("only allowed in sources[].url and sources[].ref", **{key: value})
                B.add_built_app(self.root)

    def test_environment_variables_are_fine_everywhere(self):
        self.set_recipe(steps=["make VERSION=$AGINO_VERSION TAG=${AGINO_TAG}"], test=["test -n \"$AGINO_COMMIT\""],
                        files=[{"path": "a.sh", "content": "echo $AGINO_VERSION\n"}])
        self.assertTrue(self.load_recipe())

    def test_placeholder_in_a_source_name_is_refused(self):
        self.assertBad("bad format", sources=[dict(GIT, name="a${TAG}")])


class TestFiles(V2Case):
    def good(self, **kw):
        return dict({"path": "agino/launcher.sh", "mode": "0755", "content": "#!/bin/sh\n"}, **kw)

    def test_bad_files(self):
        for item, needle in [
            (self.good(path="/etc/passwd"), "relative path"),
            (self.good(path="../x"), "relative path"),
            (self.good(path="a/../b"), "relative path"),
            (self.good(path="a//b"), "relative path"),
            (self.good(path="./a"), "relative path"),
            (self.good(path=".hidden"), "relative path"),
            (self.good(path="a b"), "relative path"),
            (self.good(path="a\\b"), "relative path"),
            (self.good(path="a/" * 9 + "b"), "at most 8 segments"),
            (self.good(path="src/new.c"), "must not lie inside a source directory"),
            (self.good(mode="0777"), 'must be "0644" or "0755"'),
            (self.good(mode="4755"), 'must be "0644" or "0755"'),
            (self.good(mode=755), 'must be "0644" or "0755"'),
            (self.good(mode=True), 'must be "0644" or "0755"'),
            (self.good(content=""), "non-empty text"),
            (self.good(content="x\x00y"), "without control characters"),
            (self.good(content="x\x1by"), "without control characters"),
            (self.good(content="é" * 40000), "at most 64 KiB"),
            (self.good(content=5), "non-empty text"),
            (self.good(extra=1), "unknown key 'extra'"),
            ({"path": "a.sh"}, "missing key 'content'"),
            ("a.sh", "must be a mapping"),
        ]:
            with self.subTest(item=item):
                self.assertBad(needle, files=[item])
                B.add_built_app(self.root)

    def test_duplicates_and_directory_clashes(self):
        self.assertBad("duplicate file path", files=[self.good(), self.good()])
        B.add_built_app(self.root)
        self.assertBad("used as a directory", files=[self.good(path="a"), self.good(path="a/b")])

    def test_too_many_files(self):
        self.assertBad("must be a list of 0..20 items", files=[self.good(path="f%d" % i) for i in range(21)])

    def test_twenty_big_files_are_fine(self):
        big = "x" * (64 << 10)
        self.set_recipe(files=[self.good(path="f%d" % i, content=big) for i in range(20)])
        self.assertEqual(len(self.load_recipe().files), 20)

    def test_pkgroot_is_reserved(self):
        self.assertBad("must not lie inside a source directory", files=[self.good(path="pkgroot/usr/bin/x")])
        B.add_built_app(self.root)
        self.assertBad("'pkgroot' is reserved", sources=[dict(GIT, name="pkgroot")])

    def test_twenty_small_files_are_fine(self):
        self.set_recipe(files=[self.good(path="f%d" % i) for i in range(20)])
        self.assertEqual(len(self.load_recipe().files), 20)

    def test_files_need_sources(self):
        self.edit_recipe(lambda d: dict(d, files=[self.good()]))
        with self.assertRaises(C.ContentError) as ctx:
            C.load(self.root)
        self.assertIn("only allowed together with sources", str(ctx.exception))


class TestTestEvidenceAndVersions(V2Case):
    def test_bad_tests(self):
        for tests, needle in [
            (["a\nb"], "single line"), (["a\x7fb"], "control characters"), ([""], "non-empty string"), (["x" * 513], "at most 512"),
            (["x\x00"], "control characters"), ([5], "non-empty string"), (["t"] * 11, "0..10 items"),
            ("tool --version", "must be a list"),
        ]:
            with self.subTest(tests=tests):
                self.assertBad(needle, test=tests)
                B.add_built_app(self.root)

    def test_ten_tests_and_none_are_fine(self):
        self.set_recipe(test=["t%d" % i for i in range(10)])
        self.assertEqual(len(self.load_recipe().test), 10)
        self.set_recipe(test=[])
        self.assertEqual(self.load_recipe().test, ())

    def test_bad_evidence(self):
        ok = {"claim": "c", "url": "https://github.com/a/b"}
        for evidence, needle in [
            ([dict(ok, claim="")], "non-empty string"), ([dict(ok, claim="x" * 201)], "at most 200"),
            ([dict(ok, claim="<b>x</b>")], "must not contain markup"), ([dict(ok, claim="a\nb")], "single line"), ([dict(ok, claim="a\tb")], "single line"),
            ([dict(ok, url="https://a.example/it's")], "plain https URL"), ([dict(ok, url="https://a.example/x`y")], "plain https URL"),
            ([dict(ok, url="https://a_b.example/x")], "plain https URL"),
            ([dict(ok, claim="a‮b")], "invisible"), ([dict(ok, url="http://a.example/b")], "https URL"),
            ([dict(ok, url="javascript:alert(1)")], "https URL"), ([dict(ok, url="https://u:p@a.example/")], "credentials"),
            ([dict(ok, extra=1)], "unknown key 'extra'"), ([{"claim": "c"}], "missing key 'url'"),
            ([ok] * 21, "0..20 items"), (["x"], "must be a mapping"),
        ]:
            with self.subTest(evidence=evidence):
                self.assertBad(needle, evidence=evidence)
                B.add_built_app(self.root)

    def test_evidence_urls_are_not_limited_to_the_allow_list(self):
        self.set_recipe(evidence=[{"claim": "page", "url": "https://vendor.example.com/license"}])
        self.assertEqual(len(self.load_recipe().evidence), 1)

    def test_versions(self):
        for changes, needle in [
            ({"recipe_version": 0}, "between 1"), ({"recipe_version": -1}, "between 1"),
            ({"recipe_version": "3"}, "must be an integer"), ({"recipe_version": True}, "must be an integer"),
            ({"recipe_version": 1.5}, "must be an integer"), ({"recipe_version": 10 ** 9}, "between 1"),
            ({"recipe_version": 2, "derived_from": 2}, "lower than recipe_version"),
            ({"recipe_version": 2, "derived_from": 3}, "lower than recipe_version"),
            ({"derived_from": 1}, "needs recipe_version"),
            ({"recipe_version": 2, "derived_from": 0}, "between 1"),
        ]:
            with self.subTest(changes=changes):
                self.assertBad(needle, **changes)
                B.add_built_app(self.root)

    def test_unknown_key_is_still_rejected(self):
        self.assertBad("unknown key 'extra'", extra=1)

    def test_the_digest_ignores_v2_recipes(self):
        before = C.content_digest(self.root)
        self.set_recipe(test=["tool --version"])
        self.assertEqual(C.content_digest(self.root), before)


class TestHelpers(V2Case):
    def test_substitute(self):
        self.assertEqual(recipe_v2.substitute("v${VERSION}-${TAG}", {"VERSION": "1", "TAG": "t"}), "v1-t")
        with self.assertRaises(recipe_v2.ExpandError):
            recipe_v2.substitute("${COMMIT}", {"COMMIT": None})
        self.assertFalse(recipe_v2.has_placeholder("$VERSION"))
        self.assertTrue(recipe_v2.has_placeholder("a${TAG}b"))

    def test_load_without_a_policy_uses_defaults(self):
        self.set_recipe()
        self.assertTrue(os.path.exists(self.path("apps", "tool", "build.yaml")))
        self.assertTrue(C.load(self.root).recipe("tool").is_v2)
