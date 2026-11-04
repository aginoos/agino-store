# SPDX-License-Identifier: GPL-3.0-or-later
import copy
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_helpers import TASK_ID, builder_cfg, task_doc, write_task  # noqa: E402

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

    def test_load_reads_the_builder_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sandbox.conf")
            with open(path, "w", encoding="utf-8") as f:
                f.write("[sandbox]\nslots = 2\n[builder]\nmax_concurrent = 3  # three\nlimits = cpu_minutes=10\n")
            cfg = config.load(path)
        self.assertEqual((cfg.slots, cfg.builder.max_concurrent, cfg.builder.limits.cpu_minutes), (2, 3, 10))


if __name__ == "__main__":
    unittest.main()
