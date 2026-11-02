# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import json
import os
import tempfile
import unittest

from agino_store_server import cli, config, content, sandboxqueue
from agino_store_server.state import State

from . import helpers as H
from .test_cli import CliCase
from .test_sandboxqueue import DEB
from .test_shots import add_shots


def milkyway_name(version):
    return "gimp_" + hashlib.sha256(("milkyway:gimp=%s" % version).encode()).hexdigest()[:12]


class TestShotsRequestCli(CliCase):
    def test_request_and_cancel(self):
        add_shots(self.cfg.content, app="gimp")
        code, out, _ = self.run_cli("shots-request", "gimp")
        self.assertEqual(code, 0)
        self.assertIn("requested shots for gimp", out)
        request, = self.st.shots_requests()
        self.assertEqual((request["app"], request["by"]), ("gimp", "alice"))
        self.assertRegex(request["requested"], r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
        self.assertIn("already requested", self.run_cli("shots-request", "gimp")[1])
        self.assertIn("cancelled", self.run_cli("shots-request", "--cancel", "gimp")[1])
        self.assertEqual(self.st.shots_requests(), [])
        self.assertIn("no shots request", self.run_cli("shots-request", "--cancel", "gimp")[1])

    def test_unknown_app_and_no_shots(self):
        self.assertEqual(self.run_cli("shots-request", "nosuch")[0], cli.EXIT_USAGE)
        code, _, err = self.run_cli("shots-request", "gimp")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("no valid shots.yaml", err)
        self.assertEqual(self.st.shots_requests(), [])

    def test_shots_out_config(self):
        self.assertEqual(config.load(self.conf).shots_out, config.DEFAULT_SHOTS_OUT)
        with open(self.conf, encoding="utf-8") as f:
            text = f.read()
        with open(self.conf, "w", encoding="utf-8") as f:
            f.write(text.replace("[paths]\n", "[paths]\nshots_out = /x/y\n", 1))
        self.assertEqual(config.load(self.conf).shots_out, "/x/y")


class TestShotsJobs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = self._tmp.name
        self.queue = os.path.join(base, "queue")
        self.out = os.path.join(base, "shots-out")
        self.cfg = H.make_config(base, sandbox_queue=self.queue, shots_out=self.out)
        H.make_content(self.cfg.content)
        add_shots(self.cfg.content, app="chrome")
        add_shots(self.cfg.content, app="gimp")
        self.st = State(self.cfg.state)

    def request(self, app):
        self.st.add_shots_request(app, {"app": app, "requested": "2026-11-03T00:00:00Z", "by": "alice"})

    def candidate(self, version="2.0-1", sha="a" * 64, status="pending", sandbox="pass"):
        record = H.candidate(version=version, sha=sha, status=status,
                             checks={"deps": {"ok": True}, "sandbox": {"status": sandbox}})
        H.write(os.path.join(self.cfg.downloads, record["file"]), DEB)
        return self.st.add_candidate(record)

    def run_queue(self):
        return sandboxqueue.run(self.cfg, self.st, content.load(self.cfg.content))

    def names(self):
        return sorted(os.listdir(self.queue)) if os.path.isdir(self.queue) else []

    def doc(self, name):
        with open(os.path.join(self.queue, name + ".json"), encoding="utf-8") as f:
            return json.load(f)

    def finish(self, name, link=False):
        directory = os.path.join(self.out, name)
        H.write(os.path.join(directory, "shots.json"), b"{}")
        return directory

    def test_vendor_job_uses_newest_candidate(self):
        self.candidate(version="1.0-1", sha="b" * 64, status="approved")
        self.candidate(version="2.0-1", sha="a" * 64)
        self.candidate(version="3.0-1", sha="c" * 64, status="rejected")
        self.request("chrome")
        messages = self.run_queue()
        self.assertEqual(self.names(), ["chrome_aaaaaaaaaaaa.deb", "chrome_aaaaaaaaaaaa.demo.tar",
                                        "chrome_aaaaaaaaaaaa.json"])
        self.assertEqual(messages, ["queued chrome 2.0-1 (chrome_aaaaaaaaaaaa)"])
        doc = self.doc("chrome_aaaaaaaaaaaa")
        self.assertEqual((doc["mode"], doc["source"], doc["version"], doc["sha256"], doc["deb"]),
                         ("shots", "deb", "2.0-1", "a" * 64, "chrome_aaaaaaaaaaaa.deb"))
        self.assertEqual(doc["shots"]["scenes"][0]["id"], "editor")
        self.assertIn("install_profile", doc)
        self.assertEqual(self.run_queue(), [])

    def test_milkyway_job(self):
        self.st.save_milkyway({"gimp": {"version": "3.0.4-1", "first_seen": "x", "size": 1, "installed_size": 1}})
        self.request("gimp")
        self.run_queue()
        name = milkyway_name("3.0.4-1")
        self.assertEqual(self.names(), [name + ".demo.tar", name + ".json"])
        doc = self.doc(name)
        self.assertEqual((doc["mode"], doc["source"], doc["package"], doc["version"], doc["sha256"]),
                         ("shots", "milkyway", "gimp", "3.0.4-1", None))
        self.assertNotIn("deb", doc)
        self.assertEqual(doc["demo"], name + ".demo.tar")
        self.assertEqual(self.run_queue(), [])

    def test_milkyway_package_missing_is_skipped(self):
        self.request("gimp")
        messages = self.run_queue()
        self.assertEqual(messages, ["skipped shots gimp: package gimp is not in milkyway"])
        self.assertEqual(self.names(), [])
        self.assertEqual(len(self.st.shots_requests()), 1)

    def test_vendor_without_candidate_is_skipped(self):
        self.request("chrome")
        self.assertTrue(self.run_queue()[0].startswith("skipped shots chrome"))

    def test_closed_by_shots_json(self):
        self.candidate()
        self.request("chrome")
        self.run_queue()
        self.finish("chrome_aaaaaaaaaaaa")
        messages = self.run_queue()
        self.assertIn("closed shots request chrome (chrome_aaaaaaaaaaaa)", messages)
        self.assertEqual(self.st.shots_requests(), [])
        self.assertEqual(self.names(), [])   # stale entries removed

    def test_milkyway_closed(self):
        self.st.save_milkyway({"gimp": {"version": "3.0.4-1"}})
        self.request("gimp")
        self.run_queue()
        self.finish(milkyway_name("3.0.4-1"))
        self.run_queue()
        self.assertEqual((self.st.shots_requests(), self.names()), ([], []))

    def test_missing_shots_out_closes_nothing(self):
        self.candidate()
        self.request("chrome")
        self.run_queue()
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(len(self.st.shots_requests()), 1)
        self.assertIn("chrome_aaaaaaaaaaaa.json", self.names())

    def test_symlinked_entries_ignored(self):
        self.candidate()
        self.request("chrome")
        real = os.path.join(self._tmp.name, "elsewhere")
        H.write(os.path.join(real, "shots.json"), b"{}")
        os.makedirs(self.out)
        os.symlink(real, os.path.join(self.out, "chrome_aaaaaaaaaaaa"))
        self.run_queue()
        self.assertEqual(len(self.st.shots_requests()), 1)
        os.unlink(os.path.join(self.out, "chrome_aaaaaaaaaaaa"))
        os.makedirs(os.path.join(self.out, "chrome_aaaaaaaaaaaa"))
        os.symlink(os.path.join(real, "shots.json"), os.path.join(self.out, "chrome_aaaaaaaaaaaa", "shots.json"))
        self.run_queue()
        self.assertEqual(len(self.st.shots_requests()), 1)

    def test_check_job_wins_the_name(self):
        self.candidate(sandbox="missing")
        self.request("chrome")
        messages = self.run_queue()
        self.assertEqual(len(messages), 1)
        self.assertNotIn("mode", self.doc("chrome_aaaaaaaaaaaa"))
        self.assertIn("shots", self.doc("chrome_aaaaaaaaaaaa"))
        self.assertEqual(len(self.st.shots_requests()), 1)   # stays open
        self.finish("chrome_aaaaaaaaaaaa")                   # the check job's shots close it
        self.run_queue()
        self.assertEqual(self.st.shots_requests(), [])
        self.assertIn("chrome_aaaaaaaaaaaa.json", self.names())   # the check job itself stays

    def test_check_job_done_then_shots_job_replaces_it(self):
        self.candidate(sandbox="missing")
        self.request("chrome")
        self.run_queue()
        self.st.update_candidate(self.st.candidates(app="chrome")[0], {"checks": {"sandbox": {"status": "pass"}}})
        self.run_queue()
        self.assertEqual(self.doc("chrome_aaaaaaaaaaaa")["mode"], "shots")


if __name__ == "__main__":
    unittest.main()
