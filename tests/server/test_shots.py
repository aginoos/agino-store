# SPDX-License-Identifier: GPL-3.0-or-later
import copy
import io
import json
import os
import tarfile
import tempfile
import unittest

from agino_store_server import content as C
from agino_store_server import sandboxqueue
from agino_store_server.state import State

from . import helpers as H
from .test_content import ContentCase
from .test_sandboxqueue import DEB

SCENE = {"id": "editor", "caption": H.text("编辑器", "Editor"), "demo": ["code-project"],
         "args": ["~/Demo/code-project"], "steps": [{"wait": 3}, {"key": "ctrl+shift+p"}, {"type": "hi"},
                                                    {"click": [200, 150, "right"]}, {"wait_window": "Prefs"}]}
SHOTS = {"schema": 1, "window": {"width": 1400, "height": 900}, "scenes": [SCENE]}


def add_shots(root, data=None, demo=True, app="gimp"):
    H.write_yaml(os.path.join(root, "apps", app, "shots.yaml"), copy.deepcopy(data or SHOTS))
    if demo:
        H.write(os.path.join(root, "demo", "code-project", "src", "main.py"), b"print(1)\n")
        H.write(os.path.join(root, "demo", "code-project", "README.txt"), b"hello\n")


class TestShotsValidation(ContentCase):
    def shots(self, **change):
        data = copy.deepcopy(SHOTS)
        data.update(change)
        add_shots(self.root, data)

    def scene(self, **change):
        self.shots(scenes=[dict(SCENE, **change)])

    def test_valid(self):
        add_shots(self.root, dict(SHOTS, promo=[]))
        gimp = C.load(self.root).app("gimp")
        scene = gimp.shots["scenes"][0]
        self.assertEqual(gimp.shots["window"], {"width": 1400, "height": 900})
        self.assertEqual(scene["window"], {"width": 1400, "height": 900})   # default filled in
        self.assertEqual(scene["capture"], "window")
        self.assertEqual(scene["demo"], ["code-project"])
        self.assertIsNone(C.load(self.root).app("chrome").shots)

    def test_defaults(self):
        add_shots(self.root, {"schema": 1, "scenes": [{"id": "a", "caption": H.text("甲", "A")}]}, demo=False)
        shots = C.load(self.root).app("gimp").shots
        self.assertEqual(shots["window"], {"width": 1600, "height": 1000})
        self.assertEqual(shots["scenes"][0]["steps"], [])

    def test_promo(self):
        H.write(self.path("apps", "gimp", "promo", "zh_CN", "1.png"), H.png(10, 10))
        promo = {"image": "promo/zh_CN/1.png", "lang": "zh_CN", "source": "https://x.example/", "license": "ok"}
        self.shots(promo=[promo])
        self.assertEqual(C.load(self.root).app("gimp").promo[0]["image"], "promo/zh_CN/1.png")
        for bad, needle in ((dict(promo, image="promo/missing.png"), "promo[0].image"),
                            (dict(promo, lang="fr"), "zh_CN or en_US"),
                            (dict(promo, image="../../apps/chrome/icon.png"), "inside the app directory"),
                            (dict(promo, image="icon.svg"), "file type"),
                            (dict(promo, extra=1), "unknown key")):
            self.shots(promo=[bad])
            self.assertRejected(needle)
        H.write(self.path("apps", "gimp", "promo", "fake.png"), b"GIF89a....")
        self.shots(promo=[dict(promo, image="promo/fake.png")])
        self.assertRejected("not a PNG")
        H.write(self.path("apps", "gimp", "promo", "big.png"), b"\x89PNG\r\n\x1a\n" + b"0" * ((10 << 20) + 1))
        self.shots(promo=[dict(promo, image="promo/big.png")])
        self.assertRejected("10 MiB")

    def test_rejections(self):
        cases = [
            (dict(schema=2), "schema"), (dict(extra=1), "unknown key"), (dict(scenes=[]), "1..6 items"),
            (dict(scenes=[dict(SCENE, id="s%d" % i) for i in range(7)]), "1..6 items"),
            (dict(window={"width": 700, "height": 900}), "window.width"),
            (dict(window={"width": 1400, "height": 1051}), "window.height"),
            (dict(window={"width": 1400}), "missing key"),
            (dict(scenes=[SCENE, SCENE]), "duplicate scene id"),
        ]
        for change, needle in cases:
            self.shots(**change)
            self.assertRejected(needle)

    def test_scene_rejections(self):
        cases = [
            (dict(id="Bad"), "id"), (dict(id="a" * 32), "id"),
            (dict(caption={"zh_CN": "x"}), "missing en_US"),
            (dict(caption=H.text("x" * 81, "y")), "caption"), (dict(caption=H.text("a\nb", "y")), "single line"),
            (dict(demo=["nope"]), "unknown demo tree"), (dict(demo=["code-project", "code-project"]), "duplicate"),
            (dict(args=["x"] * 9), "0..8"), (dict(args=["x" * 301]), "args[0]"), (dict(args=["a\nb"]), "newlines"),
            (dict(window={"width": 1400, "height": 100}), "window.height"),
            (dict(capture="screen"), "capture"), (dict(extra=1), "unknown key"),
            (dict(steps=[{"wait": 1}] * 31), "0..30"),
            (dict(steps=[{"wait": 0}]), "wait"), (dict(steps=[{"wait": 21}]), "wait"),
            (dict(steps=[{"wait": True}]), "wait"),
            (dict(steps=[{"key": "ctrl shift"}]), "key"), (dict(steps=[{"key": "k" * 41}]), "key"),
            (dict(steps=[{"type": "a\nb"}]), "type"), (dict(steps=[{"type": "x" * 201}]), "type"),
            (dict(steps=[{"click": [1]}]), "click"), (dict(steps=[{"click": [-1, 5]}]), "click"),
            (dict(steps=[{"click": [1, 2, "double"]}]), "button"), (dict(steps=[{"click": [2000, 5]}]), "click"),
            (dict(steps=[{"wait_window": "x" * 81}]), "wait_window"),
            (dict(steps=[{"wait": 1, "key": "a"}]), "exactly one"), (dict(steps=[{"sleep": 1}]), "exactly one"),
            (dict(steps=["wait"]), "exactly one"),
        ]
        for change, needle in cases:
            self.scene(**change)
            self.assertRejected(needle)

    def test_unknown_key_and_bad_yaml(self):
        H.write(self.path("apps", "gimp", "shots.yaml"), b"schema: [")
        self.assertRejected("shots.yaml")

    def test_not_valid_without_scene_demo_dir(self):
        add_shots(self.root, demo=False)
        self.assertRejected("unknown demo tree")


class TestDemoTrees(ContentCase):
    def test_symlink_rejected(self):
        add_shots(self.root)
        os.symlink("/etc/passwd", self.path("demo", "code-project", "link"))
        self.assertRejected("demo/code-project")

    def test_symlinked_tree_and_bad_name(self):
        add_shots(self.root)
        os.symlink(self.path("demo", "code-project"), self.path("demo", "alias"))
        self.assertRejected("demo/alias")
        os.unlink(self.path("demo", "alias"))
        os.makedirs(self.path("demo", "Bad_Name"))
        self.assertRejected("demo/Bad_Name")

    def test_fifo_rejected(self):
        add_shots(self.root)
        os.mkfifo(self.path("demo", "code-project", "pipe"))
        self.assertRejected("regular files")

    def test_sizes(self):
        add_shots(self.root)
        H.write(self.path("demo", "code-project", "big.bin"), b"0" * ((5 << 20) + 1))
        self.assertRejected("larger than 5 MiB")
        os.unlink(self.path("demo", "code-project", "big.bin"))
        for i in range(5):
            H.write(self.path("demo", "code-project", "f%d.bin" % i), b"0" * (5 << 20))
        self.assertRejected("larger than 20 MiB")

    def test_loose_file_in_demo(self):
        H.write(self.path("demo", "stray.txt"), b"x")
        self.assertRejected("demo/stray.txt")

    def test_unused_tree_is_fine(self):
        H.write(self.path("demo", "spare", "a.txt"), b"x")
        C.load(self.root)


class TestDigest(ContentCase):
    def test_shots_promo_and_demo_do_not_change_digest(self):
        before = C.content_digest(self.root)
        add_shots(self.root)
        H.write(self.path("apps", "gimp", "promo", "1.png"), H.png(4, 4))
        self.assertEqual(before, C.content_digest(self.root))
        H.write(self.path("apps", "gimp", "icon.svg"), b"<svg/>")
        self.assertNotEqual(before, C.content_digest(self.root))


class TestQueueShots(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = self._tmp.name
        self.queue = os.path.join(base, "queue")
        self.cfg = H.make_config(base, sandbox_queue=self.queue)
        H.make_content(self.cfg.content)
        add_shots(self.cfg.content, app="chrome")
        self.st = State(self.cfg.state)
        record = H.candidate(version="2.0-1", sha="a" * 64, status="pending",
                             checks={"deps": {"ok": True}, "sandbox": {"status": "missing"}})
        H.write(os.path.join(self.cfg.downloads, record["file"]), DEB)
        self.record = self.st.add_candidate(record)

    def run_queue(self):
        return sandboxqueue.run(self.cfg, self.st, C.load(self.cfg.content))

    def names(self):
        return sorted(os.listdir(self.queue))

    def tar_path(self):
        return os.path.join(self.queue, "chrome_aaaaaaaaaaaa.demo.tar")

    def doc(self):
        with open(os.path.join(self.queue, "chrome_aaaaaaaaaaaa.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_json_carries_shots_and_demo(self):
        self.run_queue()
        self.assertEqual(self.names(), ["chrome_aaaaaaaaaaaa.deb", "chrome_aaaaaaaaaaaa.demo.tar",
                                        "chrome_aaaaaaaaaaaa.json"])
        doc = self.doc()
        self.assertEqual(doc["demo"], "chrome_aaaaaaaaaaaa.demo.tar")
        self.assertEqual(doc["demo_size"], os.path.getsize(self.tar_path()))
        self.assertEqual(doc["shots"]["scenes"][0]["id"], "editor")
        self.assertEqual(doc["shots"]["scenes"][0]["steps"][3], {"click": [200, 150, "right"]})
        self.assertEqual(doc["shots"]["window"], {"width": 1400, "height": 900})

    def test_tar_is_deterministic_and_plain(self):
        self.run_queue()
        with open(self.tar_path(), "rb") as f:
            first = f.read()
        self.assertEqual(first, __import__("agino_store_server.demo", fromlist=["x"]).build_tar(
            self.cfg.content, ["code-project"]))
        with tarfile.open(fileobj=io.BytesIO(first)) as tar:
            members = tar.getmembers()
        self.assertEqual([m.name for m in members],
                         ["code-project", "code-project/README.txt", "code-project/src",
                          "code-project/src/main.py"])
        for m in members:
            self.assertEqual((m.uid, m.gid, m.uname, m.gname, m.mtime), (0, 0, "", "", 0))
            self.assertEqual(m.mode, 0o755 if m.isdir() else 0o644)
            self.assertTrue(m.isdir() or m.isreg())

    def test_idempotent_and_content_change(self):
        self.run_queue()
        inode = os.stat(self.tar_path()).st_ino
        self.assertEqual(self.run_queue(), [])
        self.assertEqual(os.stat(self.tar_path()).st_ino, inode)
        H.write(os.path.join(self.cfg.content, "demo", "code-project", "README.txt"), b"changed\n")
        messages = self.run_queue()
        self.assertTrue(messages and messages[0].startswith("updated chrome"))
        self.assertNotEqual(os.stat(self.tar_path()).st_ino, inode)
        self.assertEqual(self.doc()["demo_size"], os.path.getsize(self.tar_path()))

    def test_no_demo_scene_has_no_tar(self):
        data = copy.deepcopy(SHOTS)
        data["scenes"][0]["demo"] = []
        add_shots(self.cfg.content, data, demo=False, app="chrome")
        self.run_queue()
        self.assertEqual(self.names(), ["chrome_aaaaaaaaaaaa.deb", "chrome_aaaaaaaaaaaa.json"])
        doc = self.doc()
        self.assertIn("shots", doc)
        self.assertNotIn("demo", doc)

    def test_tar_dropped_when_demo_no_longer_used(self):
        self.run_queue()
        os.unlink(os.path.join(self.cfg.content, "apps", "chrome", "shots.yaml"))
        self.run_queue()
        self.assertEqual(self.names(), ["chrome_aaaaaaaaaaaa.deb", "chrome_aaaaaaaaaaaa.json"])
        self.assertNotIn("shots", self.doc())

    def test_stale_demo_tar_removed(self):
        self.run_queue()
        stray = os.path.join(self.queue, "other_bbbbbbbbbbbb.demo.tar")
        H.write(stray, b"x")
        H.write(os.path.join(self.queue, "notes.demo.tar"), b"x")   # not an exact queue name: kept
        messages = self.run_queue()
        self.assertFalse(os.path.exists(stray))
        self.assertTrue(os.path.exists(os.path.join(self.queue, "notes.demo.tar")))
        self.assertIn("removed other_bbbbbbbbbbbb.demo.tar", messages)
        self.st.update_candidate(self.record, {"status": "approved"})
        messages = self.run_queue()
        self.assertEqual(self.names(), ["notes.demo.tar"])
        self.assertIn("removed chrome_aaaaaaaaaaaa.demo.tar", messages)

    def test_symlink_tar_not_followed(self):
        os.makedirs(self.queue)
        victim = os.path.join(self._tmp.name, "victim")
        H.write(victim, b"keep")
        os.symlink(victim, self.tar_path())
        self.run_queue()
        with open(victim, "rb") as f:
            self.assertEqual(f.read(), b"keep")
        self.assertFalse(os.path.islink(self.tar_path()))
