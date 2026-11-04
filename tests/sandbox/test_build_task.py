# SPDX-License-Identifier: GPL-3.0-or-later
import copy
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_helpers import TASK_ID, ai_task_doc, builder_cfg, task_doc, write_task  # noqa: E402

from agino_store_sandbox import build_guest as G  # noqa: E402
from agino_store_sandbox import build_task as T  # noqa: E402
from agino_store_sandbox import config  # noqa: E402
from agino_store_sandbox.util import SandboxError  # noqa: E402

CEILING = config.BuildLimits()


def parse(doc, task_id=TASK_ID, ceiling=CEILING):
    return T.parse_task(task_id, json.dumps(doc).encode(), ceiling)


def with_recipe(**changes):
    doc = task_doc()
    doc["recipe"].update(changes)
    return doc


class ParseTests(unittest.TestCase):
    def test_valid_task(self):
        task = parse(task_doc())
        self.assertEqual((task.id, task.app, task.version), (TASK_ID, "demo", "1.0+agino1"))
        self.assertEqual(task.build_depends, ("build-essential", "libfoo-dev"))
        self.assertEqual(len(task.steps), 3)
        self.assertEqual(task.package.name, "demo-agino")
        self.assertEqual(task.package.depends, ("libfoo1 (>= 1.2)",))
        self.assertEqual(task.limits, CEILING)
        self.assertEqual(task.deb_name, "demo-agino_1.0+agino1_amd64.deb")

    def test_informational_keys_are_accepted(self):
        doc = task_doc(id=TASK_ID, upstream_version="1.0", tag="v1.0", commit="a" * 40, attempt=1, by="admin",
                       created="2026-11-04T00:00:00Z")
        self.assertEqual(parse(doc).app, "demo")

    def test_bad_task_id(self):
        for bad in ("", "0123456789ABCDEF", "0123456789abcde", "0123456789abcdef0", "../../etc/passwd", "g" * 16):
            with self.assertRaises(T.TaskError, msg=bad):
                parse(task_doc(), task_id=bad)

    def test_id_must_match_the_directory(self):
        with self.assertRaises(T.TaskError):
            parse(task_doc(id="f" * 16))

    def test_not_json_or_not_an_object(self):
        for raw in (b"", b"{", b"[]", b'"x"', b"\xff\xfe", b"null", b"[" * 100000):
            with self.assertRaises(T.TaskError, msg=raw[:10]):
                T.parse_task(TASK_ID, raw, CEILING)

    def test_too_large(self):
        with self.assertRaises(T.TaskError):
            T.parse_task(TASK_ID, b" " * (T.MAX_TASK_BYTES + 1), CEILING)

    def test_unknown_and_missing_keys(self):
        with self.assertRaises(T.TaskError):
            parse(task_doc(extra=1))
        for key in ("app", "version", "source", "recipe"):
            doc = task_doc()
            del doc[key]
            with self.assertRaises(T.TaskError, msg=key):
                parse(doc)
        for key in ("build_depends", "steps", "package"):
            doc = task_doc()
            del doc["recipe"][key]
            with self.assertRaises(T.TaskError, msg=key):
                parse(doc)
        with self.assertRaises(T.TaskError):
            parse(with_recipe(scripts=["x"]))

    def test_app_and_version(self):
        for key, bad in (("app", "Demo"), ("app", "-x"), ("app", ""), ("app", 5), ("app", "a" * 65),
                         ("version", "1.0"[:0]), ("version", "v1.0"), ("version", "1:1.0"), ("version", "1.0 x"),
                         ("version", "1.0;id"), ("version", "1" * 65), ("version", 1)):
            with self.assertRaises(T.TaskError, msg="%s=%r" % (key, bad)):
                parse(task_doc(**{key: bad}))

    def test_source(self):
        good = task_doc()["source"]
        for bad in ({"sha256": "A" * 64, "size": 5}, {"sha256": "a" * 63, "size": 5}, {"sha256": "a" * 64, "size": 0},
                    {"sha256": "a" * 64, "size": True}, {"sha256": "a" * 64, "size": "5"},
                    {"sha256": "a" * 64, "size": 5, "file": "../source.tar"}, {"size": 5}, {"sha256": "a" * 64},
                    {"sha256": "a" * 64, "size": 5, "url": "http://x"}, "x"):
            with self.assertRaises(T.TaskError, msg=bad):
                parse(task_doc(source=bad))
        self.assertEqual(parse(task_doc(source=dict(good, file="source.tar"))).source_size, good["size"])

    def test_build_depends_names(self):
        for bad in ("Foo", "-x", "a b", "a;rm", "a$(id)", "a|b", "x", "a:amd64", "a (>= 1)", "", "a\nb", 7, "é" * 3):
            with self.assertRaises(T.TaskError, msg=repr(bad)):
                parse(with_recipe(build_depends=["ok", bad]))
        with self.assertRaises(T.TaskError):
            parse(with_recipe(build_depends="build-essential"))
        with self.assertRaises(T.TaskError):
            parse(with_recipe(build_depends=["pkg%d" % i for i in range(T.MAX_DEPENDS + 1)]))
        self.assertEqual(parse(with_recipe(build_depends=[])).build_depends, ())
        self.assertEqual(parse(with_recipe(build_depends=["g++", "libstdc++-14-dev", "python3.13"])).build_depends[0],
                         "g++")

    def test_steps(self):
        for bad in ([], "make", [5], [""], ["x" * (T.MAX_STEP_CHARS + 1)], ["a\x00b"], ["a\x1bb"],
                    ["make"] * (T.MAX_STEPS + 1)):
            with self.assertRaises(T.TaskError, msg=repr(bad)[:40]):
                parse(with_recipe(steps=bad))
        ok = parse(with_recipe(steps=["x" * T.MAX_STEP_CHARS, "a\tb\nc"] + ["make"] * (T.MAX_STEPS - 2)))
        self.assertEqual(len(ok.steps), T.MAX_STEPS)

    def test_package(self):
        base = task_doc()["recipe"]["package"]
        for key, bad in (("name", "Demo"), ("name", "a"), ("name", "x;y"), ("section", "Games"), ("section", "a/b"),
                         ("maintainer", "nobody"), ("maintainer", "a <b@c>\nX-Evil: 1"), ("maintainer", "<a@b.c>"),
                         ("depends", ["a;b"]), ("depends", "libfoo1"), ("depends", ["a (>> 1\n)"]),
                         ("depends", ["a | b"])):
            pkg = dict(base, **{key: bad})
            with self.assertRaises(T.TaskError, msg="%s=%r" % (key, bad)):
                parse(with_recipe(package=pkg))
        with self.assertRaises(T.TaskError):
            parse(with_recipe(package=dict(base, scripts={"postinst": "x"})))
        with self.assertRaises(T.TaskError):
            parse(with_recipe(package=dict(base, control="Package: x")))
        no_depends = {k: v for k, v in base.items() if k != "depends"}
        self.assertEqual(parse(with_recipe(package=no_depends)).package.depends, ())

    def test_base_and_recipe_source(self):
        with self.assertRaises(T.TaskError):
            parse(with_recipe(base="../x"))
        with self.assertRaises(T.TaskError):
            parse(with_recipe(source={"kind": "git", "url": "http://evil"}))
        self.assertEqual(parse(with_recipe(source={"kind": "git", "checksum": "required"})).base, "debian-trixie")

    def test_limits_can_only_be_lowered(self):
        doc = task_doc(limits={"cpu_minutes": 30, "memory_gib": 4, "disk_gib": 10})
        self.assertEqual(parse(doc).limits, config.BuildLimits(30, 4, 10))
        recipe_limits = with_recipe(limits={"memory_gib": 2})
        self.assertEqual(parse(recipe_limits).limits, config.BuildLimits(90, 2, 20))
        for bad in ({"cpu_minutes": 91}, {"memory_gib": 9}, {"disk_gib": 21}, {"cpu_minutes": 0},
                    {"cpu_minutes": True}, {"disk_gib": "5"}, {"cpu_minutes": 1.5}, {"cores": 4}):
            with self.assertRaises(T.TaskError, msg=bad):
                parse(task_doc(limits=bad))

    def test_original_document_is_not_mutated(self):
        doc = task_doc()
        before = copy.deepcopy(doc)
        parse(doc)
        self.assertEqual(doc, before)


SHA = "a" * 64


def v2_doc(**recipe):
    """A build task whose recipe uses `sources` instead of `source`."""
    doc = task_doc()
    doc["recipe"].update({"sources": [
        {"name": "src", "kind": "git", "url": "https://github.com/x/y.git", "ref": "v1.0"},
        {"name": "vendor", "kind": "tarball", "url": "https://dl.example.com/app_1.0.tar.gz", "sha256": SHA},
        {"name": "blob", "kind": "file", "url": "https://dl.example.com/app.AppImage", "sha256": SHA}]})
    doc["recipe"].update(recipe)
    return doc


class RecipeV2Tests(unittest.TestCase):
    def test_a_full_v2_recipe(self):
        doc = v2_doc(recipe_version=3, derived_from=2,
                     files=[{"path": "agino/launcher.sh", "mode": "0755", "content": "#!/bin/sh\nexec app \"$@\"\n"},
                            {"path": "agino/app.desktop", "mode": "0644", "content": "[Desktop Entry]\n"}],
                     test=["app --version", "timeout 20 app >/dev/null 2>&1 || test $? -eq 124"],
                     evidence=[{"claim": "license is GPL-3.0", "url": "https://example.com/LICENSE"}])
        doc.update(upstream_version="1.0", tag="v1.0", commit="b" * 40)
        task = parse(doc)
        self.assertEqual([s.name for s in task.sources], ["src", "vendor", "blob"])
        self.assertEqual((task.sources[0].kind, task.sources[0].ref, task.sources[0].sha256), ("git", "v1.0", ""))
        self.assertEqual((task.sources[1].sha256, task.sources[1].ref), (SHA, ""))
        self.assertEqual((task.recipe_version, task.derived_from), (3, 2))
        self.assertEqual([(f.path, f.mode) for f in task.files], [("agino/launcher.sh", "0755"),
                                                                ("agino/app.desktop", "0644")])
        self.assertEqual(len(task.tests), 2)
        self.assertEqual(task.evidence, (("license is GPL-3.0", "https://example.com/LICENSE"),))
        self.assertEqual(task.agino_env, ["AGINO_VERSION=1.0", "AGINO_TAG=v1.0", "AGINO_COMMIT=" + "b" * 40])
        self.assertEqual(task.mode, "build")

    def test_v1_recipe_has_no_v2_parts(self):
        task = parse(task_doc())
        self.assertEqual((task.sources, task.files, task.tests, task.evidence), ((), (), (), ()))
        self.assertEqual((task.recipe_version, task.derived_from), (None, None))

    def test_agino_env_defaults(self):
        task = parse(task_doc())
        self.assertEqual(task.agino_env, ["AGINO_VERSION=1.0", "AGINO_TAG=", "AGINO_COMMIT="])
        self.assertEqual(parse(task_doc(version="2.5.1~rc1+agino12")).upstream_version, "2.5.1~rc1")
        self.assertEqual(parse(task_doc(version="2.5")).upstream_version, "2.5")

    def test_malformed_informational_values_are_dropped(self):
        task = parse(task_doc(upstream_version="x y", tag="v1; id", commit="XYZ"))
        self.assertEqual(task.agino_env, ["AGINO_VERSION=1.0", "AGINO_TAG=", "AGINO_COMMIT="])

    def test_source_and_sources_exclude_each_other(self):
        with self.assertRaises(T.TaskError):
            parse(v2_doc(source={"kind": "git", "checksum": "required"}))

    def test_unknown_recipe_keys_are_refused(self):
        with self.assertRaises(T.TaskError):
            parse(v2_doc(scripts=["x"]))
        with self.assertRaises(T.TaskError):
            parse(v2_doc(tests=["x"]))

    def test_versions(self):
        for key in ("recipe_version", "derived_from"):
            for bad in (0, -1, True, "3", 1.5, None):
                with self.assertRaises(T.TaskError, msg="%s=%r" % (key, bad)):
                    parse(v2_doc(**{key: bad}))

    def test_sources_rules(self):
        good = {"name": "src", "kind": "git", "url": "https://github.com/x/y.git"}
        tar = {"name": "t", "kind": "tarball", "url": "https://dl.example.com/a.tgz", "sha256": SHA}
        bad_lists = [
            [], "x", [good] * 9, [good, dict(good)],                         # empty, not a list, too many, duplicate
            [dict(good, kind="svn")], [dict(good, url="http://github.com/x")],  # kind, scheme
            [dict(good, url="https://a b/")], [dict(good, url="https://x/\n")], [dict(good, url="file:///etc")],
            [dict(good, name="Src")], [dict(good, name="../x")], [dict(good, name="pkgroot")], [dict(good, name="")],
            [dict(good, sha256=SHA)], [dict(good, ref="a b")], [dict(good, ref="-x")],  # git: no sha256, odd ref
            [dict(tar, sha256=None)], [{k: v for k, v in tar.items() if k != "sha256"}],
            [dict(tar, sha256="A" * 64)], [dict(tar, sha256="a" * 63)], [dict(tar, ref="v1")],
            [dict(good, extra=1)], [{"kind": "git", "url": "https://x/y"}], [5], ["src"],
            [dict(good, url="https://x/" + "a" * 3000)],
        ]
        for bad in bad_lists:
            with self.assertRaises(T.TaskError, msg=repr(bad)[:60]):
                doc = task_doc()
                doc["recipe"]["sources"] = bad
                parse(doc)
        doc = task_doc()
        doc["recipe"]["sources"] = [good, tar, dict(tar, name="f", kind="file")]
        self.assertEqual(len(parse(doc).sources), 3)
        doc["recipe"]["sources"] = [dict(good, url="https://dl.example.com/app_${VERSION}.tgz", ref="v${VERSION}")]
        self.assertEqual(parse(doc).sources[0].ref, "v${VERSION}")

    def test_files_rules(self):
        ok = {"path": "agino/run.sh", "mode": "0755", "content": "echo hi\n"}
        for bad in ([5], ["x"], [dict(ok, extra=1)], [{"path": "a", "mode": "0644"}],
                    [dict(ok, path="/etc/passwd")], [dict(ok, path="../x")], [dict(ok, path="a/../b")],
                    [dict(ok, path="a//b")], [dict(ok, path="a/")], [dict(ok, path="")], [dict(ok, path=".")],
                    [dict(ok, path="a/./b")], [dict(ok, path="-x")], [dict(ok, path="a b")],
                    [dict(ok, path="a;b")], [dict(ok, path="a\nb")], [dict(ok, path="a" * 201)],
                    [dict(ok, path="/".join("d%d" % i for i in range(T.MAX_FILE_DEPTH + 1)))],
                    [dict(ok, mode="0777")], [dict(ok, mode="755")], [dict(ok, mode=755)], [dict(ok, mode="4755")],
                    [dict(ok, content=5)], [dict(ok, content="a\x00b")], [dict(ok, content="a\x1bb")],
                    [dict(ok, content="x" * (T.MAX_FILE_BYTES + 1))],
                    [dict(ok, content="é" * (T.MAX_FILE_BYTES // 2 + 1))],
                    [ok, ok], [dict(ok, path="a"), dict(ok, path="a/b")],
                    [dict(ok, path="p%d" % i) for i in range(T.MAX_FILES + 1)],
                    [dict(ok, path="src/x")], [dict(ok, path="vendor")], [dict(ok, path="pkgroot/x")]):
            with self.assertRaises(T.TaskError, msg=repr(bad)[:70]):
                parse(v2_doc(files=bad))
        edge = [dict(ok, path="x" * 100 + "/" + "y" * 99, content="x" * T.MAX_FILE_BYTES)]
        self.assertEqual(len(parse(v2_doc(files=edge)).files), 1)
        self.assertEqual(parse(v2_doc(files=[dict(ok, content="a\tb\nc\n")])).files[0].content, "a\tb\nc\n")
        self.assertEqual(parse(v2_doc(files=[dict(ok, path=".config/app.conf"), dict(ok, path="a.b/c_d+e@f")]))
                         .files[0].path, ".config/app.conf")

    def test_files_may_use_any_top_directory_with_a_v1_source(self):
        doc = with_recipe(files=[{"path": "src/x.sh", "mode": "0644", "content": ""}])
        self.assertEqual(parse(doc).files[0].path, "src/x.sh")

    def test_test_commands(self):
        for bad in ("app --version", [5], [""], ["a\nb"], ["a\rb"], ["a\x00b"], ["a\x1bb"],
                    ["x" * (T.MAX_TEST_CHARS + 1)], ["true"] * (T.MAX_TESTS + 1)):
            with self.assertRaises(T.TaskError, msg=repr(bad)[:50]):
                parse(with_recipe(test=bad))
        self.assertEqual(parse(with_recipe(test=[])).tests, ())
        self.assertEqual(len(parse(with_recipe(test=["x" * T.MAX_TEST_CHARS, "a\tb"] + ["true"] * 8)).tests), 10)

    def test_evidence(self):
        ok = {"claim": "license is GPL-3.0", "url": "https://example.com/COPYING"}
        for bad in ("x", [5], [dict(ok, extra=1)], [{"claim": "x"}], [dict(ok, claim="")], [dict(ok, claim="x" * 201)],
                    [dict(ok, claim="a\nb")], [dict(ok, url="http://example.com")], [dict(ok, url="javascript:x")],
                    [ok] * (T.MAX_EVIDENCE + 1)):
            with self.assertRaises(T.TaskError, msg=repr(bad)[:50]):
                parse(with_recipe(evidence=bad))
        self.assertEqual(len(parse(with_recipe(evidence=[ok] * T.MAX_EVIDENCE)).evidence), T.MAX_EVIDENCE)

    def test_a_task_with_twenty_big_files_fits(self):
        files = [{"path": "f%d" % i, "mode": "0644", "content": '"\n' * (T.MAX_FILE_BYTES // 2)} for i in range(20)]
        raw = json.dumps(v2_doc(files=files)).encode()
        self.assertGreater(len(raw), 1 << 20)
        self.assertEqual(len(T.parse_task(TASK_ID, raw, CEILING).files), 20)

    def test_the_ai_key_belongs_to_ai_tasks(self):
        with self.assertRaises(T.TaskError):
            parse(task_doc(ai={"agent": "claude", "kind": "import"}))
        with self.assertRaises(T.TaskError):
            parse(task_doc(mode="shots"))
        self.assertEqual(parse(task_doc(mode="build")).mode, "build")


class AiTaskTests(unittest.TestCase):
    def parse_ai(self, doc, ai_ceiling=None):
        return T.parse_task(TASK_ID, json.dumps(doc).encode(), CEILING, ai_ceiling)

    def test_minimal_ai_task(self):
        task = self.parse_ai({"mode": "ai", "ai": {"agent": "codex", "kind": "repair"}})
        self.assertEqual((task.mode, task.ai), ("ai", T.AiSpec("codex", "repair", 120)))
        self.assertEqual(task.limits, config.BuildLimits(cpu_minutes=120, memory_gib=16, disk_gib=40))
        self.assertEqual((task.app, task.version, task.steps, task.package), ("", "", (), None))

    def test_minutes_and_limits_can_only_be_lowered(self):
        task = self.parse_ai(ai_task_doc(limits={"memory_gib": 8, "disk_gib": 30}))
        self.assertEqual((task.ai.minutes, task.limits), (30, config.BuildLimits(30, 8, 30)))
        ceiling = config.AiLimits(cpu=4, memory_gib=4, disk_gib=10, minutes=20)
        for doc in (ai_task_doc(), ai_task_doc(limits={"memory_gib": 8}), ai_task_doc(limits={"disk_gib": 11}),
                    ai_task_doc(limits={"memory_gib": 0}), ai_task_doc(limits={"cores": 2})):
            with self.assertRaises(T.TaskError, msg=doc):
                self.parse_ai(doc, ceiling)
        self.assertEqual(self.parse_ai({"mode": "ai", "ai": {"agent": "claude", "kind": "import"}},
                                       ceiling).ai.minutes, 20)

    def test_ai_object_rules(self):
        base = {"agent": "claude", "kind": "import", "minutes": 30}
        for bad in (None, "claude", [], {}, {"agent": "claude"}, {"kind": "import"}, dict(base, agent="gpt"),
                    dict(base, kind="build"), dict(base, minutes=0), dict(base, minutes=121), dict(base, minutes=True),
                    dict(base, minutes="30"), dict(base, minutes=1.5), dict(base, extra=1)):
            with self.assertRaises(T.TaskError, msg=repr(bad)):
                self.parse_ai({"mode": "ai", "ai": bad})
        with self.assertRaises(T.TaskError):
            self.parse_ai({"mode": "ai"})

    def test_optional_identity_keys(self):
        task = self.parse_ai(ai_task_doc(app="demo", version="1.0+agino1"))
        self.assertEqual((task.app, task.version), ("demo", "1.0+agino1"))
        for key, bad in (("app", "Demo"), ("app", 5), ("version", "v1"), ("version", 1)):
            with self.assertRaises(T.TaskError, msg=key):
                self.parse_ai(ai_task_doc(**{key: bad}))
        with self.assertRaises(T.TaskError):
            self.parse_ai(ai_task_doc(extra=1))

    def test_source_and_recipe_are_accepted_but_unused(self):
        task = self.parse_ai(ai_task_doc(source=task_doc()["source"], recipe={"base": "debian-trixie"}))
        self.assertEqual((task.steps, task.source_size), ((), 0))
        with self.assertRaises(T.TaskError):
            self.parse_ai(ai_task_doc(recipe={"scripts": []}))
        with self.assertRaises(T.TaskError):
            self.parse_ai(ai_task_doc(source="x"))

    def test_id_must_match_the_directory(self):
        with self.assertRaises(T.TaskError):
            self.parse_ai(ai_task_doc(id="f" * 16))

    def test_read_task_file_uses_the_ai_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = builder_cfg(tmp, ai_limits="minutes=10")
            write_task(cfg, doc=ai_task_doc(), source=None)
            with self.assertRaises(T.TaskError):  # minutes: 30 is above the configured 10
                T.read_task_file(cfg.builder.queue, TASK_ID, cfg.builder.limits, cfg.builder.ai_limits)
            write_task(cfg, doc={"mode": "ai", "ai": {"agent": "claude", "kind": "import"}}, source=None)
            task = T.read_task_file(cfg.builder.queue, TASK_ID, cfg.builder.limits, cfg.builder.ai_limits)
            self.assertEqual(task.ai.minutes, 10)


class ReadFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = builder_cfg(self.tmp.name)

    def test_reads_a_task(self):
        write_task(self.cfg)
        self.assertEqual(T.read_task_file(self.cfg.builder.queue, TASK_ID, CEILING).app, "demo")

    def test_missing_symlink_and_non_regular_files_are_refused(self):
        with self.assertRaises(T.TaskError):
            T.read_task_file(self.cfg.builder.queue, TASK_ID, CEILING)
        base = write_task(self.cfg)
        os.unlink(os.path.join(base, "task.json"))
        os.symlink("/etc/passwd", os.path.join(base, "task.json"))
        with self.assertRaises(T.TaskError):
            T.read_task_file(self.cfg.builder.queue, TASK_ID, CEILING)
        os.unlink(os.path.join(base, "task.json"))
        os.mkdir(os.path.join(base, "task.json"))
        with self.assertRaises(SandboxError):
            T.read_task_file(self.cfg.builder.queue, TASK_ID, CEILING)

    def test_bad_directory_name_is_refused_before_any_path_is_built(self):
        with self.assertRaises(T.TaskError):
            T.read_task_file(self.cfg.builder.queue, "../x", CEILING)


class ControlTests(unittest.TestCase):
    def test_control_comes_from_recipe_fields(self):
        text = G.render_control(parse(task_doc()))
        self.assertEqual(text.splitlines(), [
            "Package: demo-agino", "Version: 1.0+agino1", "Architecture: amd64",
            "Maintainer: agino app store <store@aginoos.com>", "Section: utils", "Priority: optional",
            "Depends: libfoo1 (>= 1.2)", "Description: demo built from source by the agino app store"])

    def test_no_depends_line_without_depends(self):
        pkg = {k: v for k, v in task_doc()["recipe"]["package"].items() if k != "depends"}
        self.assertNotIn("Depends", G.render_control(parse(with_recipe(package=pkg))))


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        b = config.from_dict({}).builder
        self.assertEqual((b.queue, b.out, b.max_concurrent), ("/srv/store/build-queue", "/srv/store/build-out", 1))
        self.assertEqual(b.limits, config.BuildLimits(90, 8, 20))
        self.assertEqual((b.max_source_mib, b.max_artifact_mib, b.max_log_mib), (512, 1024, 8))
        self.assertTrue(b.apt_source.startswith("http://51.38.63.36:8080/ "))

    def test_overrides(self):
        b = config.builder_from_dict({"max_concurrent": "2", "limits": "memory_gib=4", "queue": "/q", "out": "/o",
                                      "apt_source": "[trusted=yes] https://apt.example/ milkyway main contrib"})
        self.assertEqual((b.max_concurrent, b.limits, b.queue), (2, config.BuildLimits(90, 4, 20), "/q"))

    def test_rejects_bad_values(self):
        for bad in ({"max_concurrent": "0"}, {"max_concurrent": "9"}, {"max_concurrent": "x"}, {"nope": "1"},
                    {"limits": "cpu_minutes=0"}, {"limits": "cpu_minutes=5 cpu_minutes=6"}, {"limits": "bogus=1"},
                    {"limits": "disk_gib=9999"}, {"queue": "relative"}, {"out": "/a,b"},
                    {"apt_source": "ftp://x/ y main"}, {"apt_source": "http://x/"},
                    {"apt_source": "http://x/ milkyway main; rm -rf /"}, {"apt_source": "http://x/\nmilkyway main"},
                    {"max_log_mib": "0"}):
            with self.assertRaises(SandboxError, msg=bad):
                config.builder_from_dict(bad)

    def test_ai_defaults(self):
        b = config.from_dict({}).builder
        self.assertEqual((b.ai_slot, b.ai_max_concurrent, b.ai_slots), (1, 1, (1,)))
        self.assertEqual(b.ai_limits, config.AiLimits(cpu=8, memory_gib=16, disk_gib=40, minutes=120))
        self.assertIn("HTTPS_PROXY=http://10.231.0.1:3128", b.ai_env)
        self.assertIn("ANTHROPIC_BASE_URL=http://10.231.0.1:8490/anthropic", b.ai_claude_env)
        self.assertIn("ANTHROPIC_API_KEY=agino-placeholder", b.ai_claude_env)
        self.assertIn("CODEX_HOME=/home/builder/.codex", b.ai_codex_env)
        self.assertIn('base_url = "http://10.231.0.1:8490/openai/v1"', b.ai_codex_toml)
        self.assertTrue(b.ai_claude_cmd.startswith("claude "))
        self.assertTrue(b.ai_codex_cmd.startswith("codex exec "))
        self.assertEqual(config.BuilderConfig().ai_slots, (1,))

    def test_ai_slot_follows_the_regular_slots_by_default(self):
        self.assertEqual(config.builder_from_dict({"max_concurrent": "3"}).ai_slots, (3,))
        b = config.builder_from_dict({"max_concurrent": "2", "ai_slot": "5", "ai_max_concurrent": "2"})
        self.assertEqual(b.ai_slots, (5, 6))

    def test_ai_overrides(self):
        b = config.builder_from_dict({"ai_limits": "minutes=60 cpu=2", "ai_claude_cmd": "claude -p x",
                                      "ai_env": "A=1 B=2", "ai_claude_env": "K=v", "ai_codex_env": "",
                                      "ai_codex_toml": "model = 'x'"})
        self.assertEqual(b.ai_limits, config.AiLimits(cpu=2, memory_gib=16, disk_gib=40, minutes=60))
        self.assertEqual((b.ai_claude_cmd, b.ai_env, b.ai_claude_env, b.ai_codex_env),
                         ("claude -p x", ("A=1", "B=2"), ("K=v",), ()))
        self.assertEqual(b.ai_codex_toml, "model = 'x'\n")

    def test_ai_rejects_bad_values(self):
        for bad in ({"ai_slot": "x"}, {"ai_slot": "16"}, {"ai_slot": "-1"}, {"ai_max_concurrent": "0"},
                    {"ai_max_concurrent": "5"}, {"ai_max_concurrent": "x"},
                    {"max_concurrent": "2", "ai_slot": "1"}, {"max_concurrent": "1", "ai_slot": "0"},
                    {"ai_slot": "15", "ai_max_concurrent": "2"},
                    {"ai_limits": "cpu=0"}, {"ai_limits": "minutes=99999"}, {"ai_limits": "bogus=1"},
                    {"ai_limits": "cpu=1 cpu=2"}, {"ai_limits": "cpu"},
                    {"ai_env": "A"}, {"ai_env": "A=1 A=2"}, {"ai_env": "A=b c;d"}, {"ai_env": "1A=x"},
                    {"ai_claude_env": "A=$(id)"}, {"ai_codex_env": 'A="x"'}, {"ai_env": "A=`id`"},
                    {"ai_claude_cmd": ""}, {"ai_codex_cmd": "x\ny"}, {"ai_claude_cmd": "x" * 1001},
                    {"ai_codex_toml": "a\x00b"}, {"ai_codex_toml": "x" * 5000}):
            with self.assertRaises(SandboxError, msg=bad):
                config.builder_from_dict(bad)

    def test_load_reads_the_builder_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sandbox.conf")
            with open(path, "w", encoding="utf-8") as f:
                f.write("[sandbox]\nslots = 2\n[builder]\nmax_concurrent = 3  # three\nlimits = cpu_minutes=10\n")
            cfg = config.load(path)
        self.assertEqual((cfg.slots, cfg.builder.max_concurrent, cfg.builder.limits.cpu_minutes), (2, 3, 10))


if __name__ == "__main__":
    unittest.main()
