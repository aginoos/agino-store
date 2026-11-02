# SPDX-License-Identifier: GPL-3.0-or-later
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures  # noqa: E402
from agino_store_server import checks, trackers  # noqa: E402
from agino_store_server.state import State  # noqa: E402

DPKG = fixtures.have("dpkg-deb", "dpkg")
DOSE = fixtures.have("dpkg-deb", "dpkg", "dose-debcheck")


def entry(path, mode="-rw-r--r--", kind="-"):
    return {"path": path, "mode": mode, "type": kind, "owner": "root/root", "size": 1, "target": None}


class FlagTests(unittest.TestCase):
    def test_file_flags(self):
        files = [
            entry("usr/bin/su2", "-rwsr-xr-x"), entry("usr/bin/wall2", "-rwxr-sr-x"),
            entry("etc/apt/sources.list.d/x.list"), entry("etc/apt/", "drwxr-xr-x", "d"),
            entry("lib/systemd/system/x.service"), entry("etc/cron.d/x"),
            entry("etc/sudoers.d/x"), entry("etc/profile.d/x.sh"),
            entry("usr/lib/udev/rules.d/99-x.rules"), entry("usr/share/doc/x"),
        ]
        flags = checks.file_flags(files)
        for want in ("setuid usr/bin/su2", "setgid usr/bin/wall2", "writes /etc/apt: etc/apt/sources.list.d/x.list",
                     "systemd unit lib/systemd/system/x.service", "cron etc/cron.d/x",
                     "writes /etc/sudoers.d/x", "writes /etc/profile.d/x.sh",
                     "udev rule usr/lib/udev/rules.d/99-x.rules"):
            self.assertIn(want, flags)
        self.assertEqual(len(flags), 8)

    def test_script_flags(self):
        flags = checks.script_flags({"postinst": b"apt-key add k; chmod 777 /x", "prerm": b"true"})
        self.assertEqual(flags, ["postinst mentions apt-key", "postinst mentions chmod 777"])

    def test_fg_stanza_collapses_whitespace_and_needs_basics(self):
        text = checks.fg_stanza({"Package": "p", "Version": "1", "Architecture": "amd64",
                                 "Depends": "a,\nb\n\nPackage: evil", "Description": "dropped"})
        self.assertEqual(text, "Package: p\nVersion: 1\nArchitecture: amd64\nDepends: a, b Package: evil\n")
        with self.assertRaises(checks.CheckError):
            checks.fg_stanza({"Package": "p"})


@unittest.skipUnless(DPKG, "dpkg-deb not available")
class InspectTests(unittest.TestCase):
    def test_inspect_package_writes_inspect_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            deb = fixtures.build_deb(
                tmp, "demo", "1.0", scripts={"postinst": "#!/bin/sh\nuseradd demo\n"},
                files={"usr/bin/demo": b"x", "etc/cron.d/demo": b"* * * * * root true\n"},
                modes={"usr/bin/demo": 0o4755}, extra_control={"Installed-Size": "5"})
            inspect_dir = os.path.join(tmp, "x.inspect")
            inspected, installed = checks.inspect_package(deb, inspect_dir)
            again, _ = checks.inspect_package(deb, inspect_dir)      # re-running replaces control/
            self.assertEqual(inspected, again)
            self.assertEqual(installed, 5 * 1024)
            self.assertEqual(list(inspected["scripts"]), ["postinst"])
            self.assertIn("setuid usr/bin/demo", inspected["flags"])
            self.assertIn("cron etc/cron.d/demo", inspected["flags"])
            self.assertIn("postinst mentions useradd", inspected["flags"])
            self.assertTrue(os.path.isfile(os.path.join(inspect_dir, "control", "postinst")))
            with open(os.path.join(inspect_dir, "files.txt"), encoding="utf-8") as f:
                self.assertIn("usr/bin/demo", f.read())


class MilkywayBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = fixtures.TestServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.cfg = fixtures.make_cfg(self.tmp.name, self.server.base)
        self.st = State(self.cfg.state)
        self.mroot = os.path.join(self.tmp.name, "mw")
        self.publish([("libfoo", "1.0")])

    def publish(self, pkgs):
        stanzas = [{"Package": n, "Version": v, "Architecture": "amd64", "Size": "1000",
                    "Installed-Size": "10", "Description": "x"} for n, v in pkgs]
        fixtures.write_milkyway_repo(self.mroot, stanzas)
        self.server.mount(self.mroot, "/milkyway")

    def content(self, apps, vendors=None):
        return fixtures.FakeContent(os.path.join(self.tmp.name, "content"), vendors or {}, apps)


class RunCheckTests(MilkywayBase):
    def test_updates_milkyway_json_and_reports_missing(self):
        content = self.content({"m1": fixtures.milkyway_app("libfoo"), "m2": fixtures.milkyway_app("ghost")})
        problems = checks.run_check(self.cfg, self.st, content)
        self.assertEqual([(p["app"], p["level"]) for p in problems], [("m2", "error")])
        data = self.st.milkyway()
        self.assertEqual(list(data), ["libfoo"])
        self.assertEqual((data["libfoo"]["version"], data["libfoo"]["size"], data["libfoo"]["installed_size"]),
                         ("1.0", 1000, 10240))

    def test_first_seen_kept_until_version_changes(self):
        content = self.content({"m1": fixtures.milkyway_app("libfoo")})
        self.st.save_milkyway({"libfoo": {"version": "1.0", "first_seen": "2000-01-01T00:00:00Z",
                                          "size": 1, "installed_size": 1}})
        checks.run_check(self.cfg, self.st, content)
        self.assertEqual(self.st.milkyway()["libfoo"]["first_seen"], "2000-01-01T00:00:00Z")
        self.st.save_milkyway({"libfoo": {"version": "0.9", "first_seen": "2000-01-01T00:00:00Z",
                                          "size": 1, "installed_size": 1}})
        checks.run_check(self.cfg, self.st, content)
        self.assertNotEqual(self.st.milkyway()["libfoo"]["first_seen"], "2000-01-01T00:00:00Z")

    def test_packages_cached_by_release_hash(self):
        content = self.content({"m1": fixtures.milkyway_app("libfoo")})
        path = "/milkyway/dists/milkyway/main/binary-amd64/Packages.gz"
        checks.run_check(self.cfg, self.st, content)
        checks.run_check(self.cfg, self.st, content)
        self.assertEqual(self.server.count("GET", path), 1)
        del self.server.routes["/milkyway/dists/milkyway/Release"]       # fetch failure: use the cache
        self.assertEqual(checks.run_check(self.cfg, self.st, content), [])
        self.publish([("libfoo", "2.0")])
        checks.run_check(self.cfg, self.st, content)
        self.assertEqual(self.st.milkyway()["libfoo"]["version"], "2.0")
        self.assertEqual(len(os.listdir(self.st.cache_dir("milkyway-cache"))), 2)

    def test_unreachable_milkyway_without_cache_is_a_problem(self):
        del self.server.routes["/milkyway/dists/milkyway/Release"]
        problems = checks.run_check(self.cfg, self.st, self.content({"m1": fixtures.milkyway_app("libfoo")}))
        self.assertEqual(len(problems), 1)

    def test_unknown_app(self):
        problems = checks.run_check(self.cfg, self.st, self.content({}), app="nope")
        self.assertEqual(len(problems), 1)


@unittest.skipUnless(DOSE, "dose-debcheck or dpkg-deb not available")
class DependencyTests(MilkywayBase):
    def intake(self, depends, version="1.0"):
        vendor = {"package": "demo", "arch": "amd64", "fetch": "auto", "allowed_hosts": ["127.0.0.1"],
                  "track": {"type": "head", "url": self.server.base + "/x"}}
        content = self.content({"v1": fixtures.deb_app("v1")}, {"v1": vendor})
        deb = fixtures.build_deb(self.tmp.name, "demo", version, depends=depends)
        return content, trackers.intake(self.cfg, self.st, content, "v1", deb, "u")

    def test_installable_candidate(self):
        _, cand = self.intake("libfoo (>= 1.0)")
        deps = cand["checks"]["deps"]
        self.assertTrue(deps["ok"], deps["detail"])
        self.assertEqual(len(deps["milkyway"]), 64)

    def test_uninstallable_candidate_has_explanation(self):
        _, cand = self.intake("libmissing")
        self.assertFalse(cand["checks"]["deps"]["ok"])
        self.assertIn("libmissing", cand["checks"]["deps"]["detail"])

    def test_run_check_rechecks_pending_candidates(self):
        content, cand = self.intake("libfoo")
        self.assertTrue(cand["checks"]["deps"]["ok"])
        self.publish([("libbar", "1.0")])                                # libfoo disappears
        problems = checks.run_check(self.cfg, self.st, content)
        self.assertEqual([p["app"] for p in problems], ["v1"])
        (after,) = self.st.candidates(app="v1")
        self.assertFalse(after["checks"]["deps"]["ok"])


if __name__ == "__main__":
    unittest.main()
