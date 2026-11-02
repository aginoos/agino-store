# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "server"))
sys.path.insert(0, os.path.dirname(__file__))

from agino_store_sandbox import config, jobs, runner, shots_job  # noqa: E402
from agino_store_sandbox.util import SandboxError  # noqa: E402
from test_shots import GOOD  # noqa: E402


def mw_stem(app="app", package="app-pkg", version="1.2-1"):
    return jobs.milkyway_stem(app, package, version)


def write(tmp, stem, doc):
    if doc.get("shots") and doc.get("mode") == "shots":
        doc = dict(doc, demo=stem + ".demo.tar")
        with open(os.path.join(tmp, stem + ".demo.tar"), "wb") as f:
            f.write(b"tar")
    with open(os.path.join(tmp, stem + ".json"), "w") as f:
        json.dump(doc, f)
    return stem + ".json"


def mw_doc(**over):
    doc = {"schema": 1, "mode": "shots", "source": "milkyway", "app": "app", "package": "app-pkg",
           "version": "1.2-1", "sha256": None, "shots": GOOD}
    doc.update(over)
    return doc


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def deb_job(self, **over):
        deb = b"deb"
        sha = hashlib.sha256(deb).hexdigest()
        stem = "app_" + sha[:12]
        with open(os.path.join(self.tmp.name, stem + ".deb"), "wb") as f:
            f.write(deb)
        doc = {"schema": 1, "mode": "shots", "source": "deb", "app": "app", "version": "1.0", "sha256": sha,
               "size": 3, "package": "app", "launchable": None, "deb": stem + ".deb", "shots": GOOD}
        doc.update(over)
        return write(self.tmp.name, stem, doc)

    def test_mode_absent_is_check(self):
        deb = b"deb"
        sha = hashlib.sha256(deb).hexdigest()
        stem = "app_" + sha[:12]
        with open(os.path.join(self.tmp.name, stem + ".deb"), "wb") as f:
            f.write(deb)
        name = write(self.tmp.name, stem, {"schema": 1, "app": "app", "version": "1.0", "sha256": sha, "size": 3,
                                           "package": "app", "deb": stem + ".deb"})
        job = jobs.read_job(self.tmp.name, name)
        self.assertEqual((job.mode, job.source), ("check", "deb"))

    def test_deb_shots_job(self):
        job = jobs.read_job(self.tmp.name, self.deb_job())
        self.assertEqual((job.mode, job.source), ("shots", "deb"))
        self.assertTrue(job.deb_path.endswith(".deb"))
        self.assertEqual(len(job.shots["scenes"]), 1)

    def test_deb_shots_job_still_verifies_hash(self):
        name = self.deb_job()
        with open(os.path.join(self.tmp.name, name[:-5] + ".deb"), "wb") as f:
            f.write(b"DEB")
        with self.assertRaises(jobs.JobError):
            jobs.read_job(self.tmp.name, name)

    def test_milkyway_job(self):
        job = jobs.read_job(self.tmp.name, write(self.tmp.name, mw_stem(), mw_doc()))
        self.assertEqual((job.mode, job.source, job.sha256, job.deb_path, job.package),
                         ("shots", "milkyway", None, None, "app-pkg"))

    def test_milkyway_job_keeps_launchable(self):
        job = jobs.read_job(self.tmp.name, write(self.tmp.name, mw_stem(), mw_doc(launchable="calibre-gui.desktop")))
        self.assertEqual(job.launchable, "calibre-gui.desktop")
        with self.assertRaises(jobs.JobError):
            jobs.read_job(self.tmp.name, write(self.tmp.name, mw_stem(), mw_doc(launchable="../x.desktop")))

    def test_rejects_malformed_shots_jobs(self):
        cases = [
            mw_doc(package="Bad Pkg"), mw_doc(package="a"), mw_doc(package="-x"), mw_doc(version="x y"),
            mw_doc(version="; rm"), mw_doc(sha256="0" * 64), mw_doc(deb="x.deb"), mw_doc(size=5),
            mw_doc(shots=None), mw_doc(shots={"schema": 9}), mw_doc(mode="bogus"), mw_doc(source="ftp"),
            mw_doc(app="Bad App"),
        ]
        for i, doc in enumerate(cases):
            with self.assertRaises(jobs.JobError, msg=str(doc)):
                jobs.read_job(self.tmp.name, write(self.tmp.name, mw_stem(), doc))
        with self.assertRaises(jobs.JobError):  # name is not the hash of package and version
            jobs.read_job(self.tmp.name, write(self.tmp.name, "app_000000000000", mw_doc()))
        with self.assertRaises(jobs.JobError):  # check job must not carry a source
            jobs.read_job(self.tmp.name, write(self.tmp.name, mw_stem(), mw_doc(mode="check")))
        with self.assertRaises(jobs.JobError):  # deb shots job without shots
            jobs.read_job(self.tmp.name, self.deb_job(shots=None))
        with self.assertRaises(jobs.JobError):  # shots job without source
            jobs.read_job(self.tmp.name, write(self.tmp.name, mw_stem(), mw_doc(source=None)))


class FakeRes:
    def __init__(self, code=0, text="", err=b""):
        self.exitcode, self.text, self.err, self.timed_out = code, text, err, False

    @property
    def ok(self):
        return self.exitcode == 0


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.from_dict({"queue": self.tmp.name + "/q", "state": self.tmp.name + "/s",
                                     "reports": self.tmp.name + "/r"})
        os.mkdir(self.cfg.queue)
        self.name = write(self.cfg.queue, mw_stem(), mw_doc())

    def run_it(self, details=None, install_error=None):
        with mock.patch.object(shots_job, "Vm") as vm, mock.patch.object(shots_job, "Guest") as guest, \
                mock.patch.object(shots_job, "install", side_effect=install_error) as inst, \
                mock.patch.object(shots_job, "ShotsGuest"), mock.patch.object(shots_job, "Phase") as phase, \
                mock.patch.object(runner.report, "write") as rwrite, mock.patch.object(runner.report, "build") as rbuild:
            guest.return_value.find_desktop.return_value = "/usr/share/applications/app.desktop"
            phase.return_value.run.return_value = details or {"images": 2, "problems": 0, "sha256": "a" * 64}
            state = runner.run_job_file(self.cfg, self.name, 0, False)
            self.assertFalse(rwrite.called or rbuild.called)
            guest.return_value.apt.assert_not_called()  # no purge (install is mocked out)
            return state, phase, inst, vm

    def test_shots_run_writes_no_report_and_no_purge(self):
        state, phase, inst, vm = self.run_it()
        self.assertTrue(phase.return_value.run.called)
        self.assertTrue(inst.called)
        self.assertTrue(vm.return_value.stop.called)
        self.assertEqual((state["status"], state["attempts"]), ("done", 1))
        self.assertNotIn("report", state)
        key = jobs.state_key(jobs.stem_of(self.name), "shots")
        self.assertEqual(jobs.read_state(self.cfg.state, key)["status"], "done")
        self.assertEqual(jobs.pending(self.cfg.queue, self.cfg.state), [])

    def test_done_check_state_does_not_hide_a_shots_job_of_the_same_name(self):
        jobs.write_state(self.cfg.state, jobs.stem_of(self.name), {"status": "done", "result": "pass"})
        self.assertEqual(jobs.pending(self.cfg.queue, self.cfg.state), [self.name])

    def test_install_failure_is_error_with_tail_and_retries(self):
        err = shots_job.InstallError("apt-get install failed (exit 100)", 100, "E: Unable to locate package")
        state, phase, _, _ = self.run_it(install_error=err)
        self.assertEqual((state["status"], state["apt_exit"], state["attempts"]), ("error", 100, 1))
        self.assertIn("Unable to locate", state["apt_tail"])
        self.assertFalse(phase.return_value.run.called)
        self.assertEqual(jobs.pending(self.cfg.queue, self.cfg.state), [self.name])
        jobs.write_state(self.cfg.state, jobs.state_key(jobs.stem_of(self.name), "shots"), dict(state, attempts=3))
        self.assertEqual(jobs.pending(self.cfg.queue, self.cfg.state), [])

    def test_shots_off_is_invalid(self):
        self.cfg = config.from_dict({"queue": self.cfg.queue, "state": self.cfg.state, "shots": "off"})
        with mock.patch.object(shots_job, "Vm") as vm:
            state = runner.run_job_file(self.cfg, self.name, 0, False)
        self.assertEqual(state["status"], "invalid")
        self.assertFalse(vm.called)


class InstallTests(unittest.TestCase):
    def guest(self, results):
        g = mock.Mock()
        g.apt.side_effect = results
        g.is_installed.return_value = True
        return g

    def test_milkyway_installs_by_name(self):
        job = jobs.Job("s", "app", "1", None, 0, "app-pkg", None, [], [], None, mode="shots", source="milkyway")
        g = self.guest([FakeRes(), FakeRes()])
        shots_job.install(g, job)
        self.assertEqual([c.args[0] for c in g.apt.call_args_list], [["update"], ["install", "-y", "app-pkg"]])
        self.assertFalse(g.copy_deb.called)

    def test_deb_uses_copy_and_profile(self):
        job = jobs.Job("s", "app", "1", "0" * 64, 3, "app", None, [], [], "/q/x.deb", mode="shots")
        g = self.guest([FakeRes(100), FakeRes()])  # failed update is tolerated for debs, as in checks
        g.apply_profile.return_value = None
        shots_job.install(g, job)
        self.assertTrue(g.copy_deb.called and g.apply_profile.called)
        self.assertEqual(g.apt.call_args_list[1].args[0], ["install", "-y", "/var/tmp/sbx/x.deb"])

    def test_failure_tail_is_capped(self):
        job = jobs.Job("s", "app", "1", None, 0, "app-pkg", None, [], [], None, mode="shots", source="milkyway")
        g = self.guest([FakeRes(), FakeRes(100, "x" * 5000, b"y" * 5000)])
        with self.assertRaises(shots_job.InstallError) as cm:
            shots_job.install(g, job)
        self.assertEqual(cm.exception.exitcode, 100)
        self.assertLessEqual(len(cm.exception.tail), 900)


class ResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.from_dict({"queue": self.tmp.name + "/q", "state": self.tmp.name + "/s",
                                     "shots_out": self.tmp.name + "/o"})
        self.out = os.path.join(self.cfg.shots_out, "gimp_3c256705b88b")
        os.makedirs(os.path.join(self.out, "zh_CN"))
        with open(os.path.join(self.out, "shots.json"), "w") as f:
            f.write("{}")

    def test_keeps_previous_output_and_drops_state(self):
        jobs.write_state(self.cfg.state, "gimp_3c256705b88b.shots", {"status": "done"})
        jobs.write_state(self.cfg.state, "gimp_3c256705b88b", {"status": "done"})  # a check job's state stays
        os.makedirs(self.out + ".prev")
        msg = shots_job.reset(self.cfg, "gimp_3c256705b88b.json")
        self.assertIn("state removed", msg)
        self.assertFalse(os.path.exists(self.out))
        self.assertTrue(os.path.exists(os.path.join(self.out + ".prev", "shots.json")))
        self.assertIsNone(jobs.read_state(self.cfg.state, "gimp_3c256705b88b.shots"))
        self.assertIsNotNone(jobs.read_state(self.cfg.state, "gimp_3c256705b88b"))
        self.assertIn("nothing to reset", shots_job.reset(self.cfg, "gimp_3c256705b88b"))

    def test_rejects_bad_names(self):
        for name in ("../x_3c256705b88b", "gimp", "gimp_3c256705b88b/..", ""):
            with self.assertRaises(SandboxError):
                shots_job.reset(self.cfg, name)
        self.assertTrue(os.path.exists(self.out))


if __name__ == "__main__":
    unittest.main()
