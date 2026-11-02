# SPDX-License-Identifier: GPL-3.0-or-later
import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures  # noqa: E402
from agino_store_server import trackers  # noqa: E402
from agino_store_server.state import State  # noqa: E402

DPKG = fixtures.have("dpkg-deb", "dpkg")
GPG = fixtures.have("gpg", "gpgv", "dpkg-deb", "dpkg")


def vendor_def(track, fetch_mode="auto", package="demo"):
    return {"package": package, "arch": "amd64", "fetch": fetch_mode,
            "allowed_hosts": ["127.0.0.1"], "track": track}


class TrackerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = fixtures.TestServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.cfg = fixtures.make_cfg(self.tmp.name, self.server.base)
        self.st = State(self.cfg.state)
        self.root = os.path.join(self.tmp.name, "content")

    def content(self, vendors, apps=None):
        apps = apps if apps is not None else {vid: fixtures.deb_app(vid) for vid in vendors}
        return fixtures.FakeContent(self.root, vendors, apps)

    def serve_deb(self, path, version, package="demo", etag=None, **kw):
        deb = fixtures.build_deb(self.tmp.name, package, version, **kw)
        with open(deb, "rb") as f:
            self.server.routes[path] = fixtures.Route(body=f.read(), etag=etag)
        return deb

    def track(self, content, vendor=None):
        return trackers.run_track(self.cfg, self.st, content, vendor)

    def levels(self, problems):
        return sorted(p["level"] for p in problems)


@unittest.skipUnless(DPKG, "dpkg-deb not available")
class HttpTrackerTests(TrackerBase):
    def test_json_creates_pending_candidate(self):
        self.serve_deb("/files/demo_2.1_amd64.deb", "2.1", scripts={"postinst": "#!/bin/sh\napt-key add x\n"})
        self.server.routes["/latest.js"] = fixtures.Route(
            body=b'cb({"version": "2.1", "dl": {"deb": "/files/demo_2.1_amd64.deb"}});')
        track = {"type": "json", "url": self.server.base + "/latest.js", "strip_js": True,
                 "version_path": "version", "url_path": "dl.deb"}
        content = self.content({"v1": vendor_def(track)})
        problems = self.track(content)
        self.assertEqual(self.levels(problems), ["info"])
        (cand,) = self.st.candidates(app="v1")
        self.assertEqual((cand["status"], cand["version"]), ("pending", "2.1"))
        self.assertTrue(os.path.isfile(os.path.join(self.cfg.downloads, cand["file"])))
        self.assertEqual(cand["origin"]["type"], "https")
        self.assertIn("postinst", cand["checks"]["inspect"]["scripts"])
        self.assertIn("postinst mentions apt-key", cand["checks"]["inspect"]["flags"])
        self.assertIsNone(cand["checks"]["diff"]["against"])
        self.assertEqual(cand["checks"]["sandbox"], {"status": "missing"})
        self.assertIn("deps", cand["checks"])
        self.assertTrue(os.path.isdir(self.st.inspect_dir(cand)))
        memo = self.st.vendor_memo("v1")
        self.assertEqual((memo["seen_version"], memo["last_error"]), ("2.1", None))
        # second run: nothing new
        self.assertEqual(self.track(content), [])
        self.assertEqual(len(self.st.candidates(app="v1")), 1)

    def test_not_newer_than_approved_is_ignored(self):
        self.serve_deb("/files/demo_2.1_amd64.deb", "2.1")
        self.server.routes["/latest.json"] = fixtures.Route(
            body=b'{"version": "2.1", "url": "/files/demo_2.1_amd64.deb"}')
        track = {"type": "json", "url": self.server.base + "/latest.json",
                 "version_path": "version", "url_path": "url"}
        self.st.set_approved("v1", {"version": "2.1", "sha256": "0" * 64}, {"event": "approve"})
        self.assertEqual(self.track(self.content({"v1": vendor_def(track)})), [])
        self.assertEqual(self.st.candidates(app="v1"), [])
        self.assertEqual(self.server.count("GET", "/files/demo_2.1_amd64.deb"), 0)

    def test_head_downloads_only_when_validators_change(self):
        self.serve_deb("/dl/demo.deb", "2.0", etag='"a"')
        track = {"type": "head", "url": self.server.base + "/dl/demo.deb"}
        content = self.content({"v1": vendor_def(track)})
        self.assertEqual(self.levels(self.track(content)), ["info"])
        self.assertEqual(self.st.candidates(app="v1")[0]["version"], "2.0")
        self.assertEqual(self.track(content), [])
        self.assertEqual(self.server.count("GET", "/dl/demo.deb"), 1)
        self.serve_deb("/dl/demo.deb", "2.1", etag='"b"')
        self.assertEqual(self.levels(self.track(content)), ["info"])
        self.assertEqual(sorted(c["version"] for c in self.st.candidates(app="v1")), ["2.0", "2.1"])

    def test_head_wrong_package_is_an_error(self):
        self.serve_deb("/dl/demo.deb", "2.0", package="other")
        track = {"type": "head", "url": self.server.base + "/dl/demo.deb"}
        problems = self.track(self.content({"v1": vendor_def(track)}))
        self.assertEqual(self.levels(problems), ["error"])
        self.assertIn("Package", problems[0]["message"])
        self.assertEqual(self.st.candidates(app="v1"), [])
        self.assertIn("Package", self.st.vendor_memo("v1")["last_error"])

    def test_page_picks_highest_version(self):
        self.serve_deb("/files/demo_2.5_amd64.deb", "2.5")
        html = (b'<a href="/files/demo_2.0_amd64.deb">old</a> <a href="/files/demo_2.5_amd64.deb">new</a>')
        self.server.routes["/downloads"] = fixtures.Route(body=html)
        track = {"type": "page", "url": self.server.base + "/downloads",
                 "link_regex": r'href="(?P<url>/files/demo_(?P<version>[0-9.]+)_amd64\.deb)"'}
        self.assertEqual(self.levels(self.track(self.content({"v1": vendor_def(track)}))), ["info"])
        self.assertEqual(self.st.candidates(app="v1")[0]["version"], "2.5")

    def test_redirect_version_from_final_url(self):
        self.serve_deb("/files/demo_3.1_amd64.deb", "3.1")
        self.server.routes["/latest"] = fixtures.Route(redirect=self.server.base + "/files/demo_3.1_amd64.deb")
        track = {"type": "redirect", "url": self.server.base + "/latest",
                 "version_regex": r"demo_(?P<version>[0-9.]+)_"}
        self.assertEqual(self.levels(self.track(self.content({"v1": vendor_def(track)}))), ["info"])
        self.assertEqual(self.st.candidates(app="v1")[0]["version"], "3.1")

    def test_page_regex_backtracking_times_out_as_vendor_error(self):
        self.server.routes["/downloads"] = fixtures.Route(body=b"a" * 40 + b"!")
        track = {"type": "page", "url": self.server.base + "/downloads", "link_regex": r"(?P<url>(a+)+$)"}
        old = trackers.REGEX_TIMEOUT
        trackers.REGEX_TIMEOUT = 2
        self.addCleanup(setattr, trackers, "REGEX_TIMEOUT", old)
        problems = self.track(self.content({"v1": vendor_def(track)}))
        self.assertEqual(self.levels(problems), ["error"])
        self.assertIn("did not finish", problems[0]["message"])

    def test_regex_matches_unit(self):
        got = trackers.regex_matches(r"(?P<url>/f_(?P<version>[0-9.]+))", "x /f_1.0 /f_2.0", "finditer")
        self.assertEqual([m["version"] for m in got["matches"]], ["1.0", "2.0"])
        self.assertTrue(got["has_version"])
        with self.assertRaises(trackers.TrackError):
            trackers.regex_matches("(", "x", "search")

    def test_regex_input_is_capped_at_1_mib(self):
        text = "x" * trackers.MAX_REGEX_TEXT + "/f_9.9"
        got = trackers.regex_matches(r"/f_(?P<version>[0-9.]+)", text, "search")
        self.assertEqual(got["matches"], [])

    def test_page_version_must_start_with_a_digit(self):
        self.server.routes["/downloads"] = fixtures.Route(body=b'<a href="/files/demo_-9_amd64.deb">x</a>')
        track = {"type": "page", "url": self.server.base + "/downloads",
                 "link_regex": r'href="(?P<url>/files/demo_(?P<version>[-0-9.]+)_amd64\.deb)"'}
        self.assertEqual(self.levels(self.track(self.content({"v1": vendor_def(track)}))), ["error"])

    def test_same_version_with_other_size_alerts(self):
        self.serve_deb("/files/demo_2.1_amd64.deb", "2.1")
        self.server.routes["/latest.json"] = fixtures.Route(
            body=b'{"version": "2.1", "url": "/files/demo_2.1_amd64.deb"}')
        track = {"type": "json", "url": self.server.base + "/latest.json",
                 "version_path": "version", "url_path": "url"}
        content = self.content({"v1": vendor_def(track)})
        self.assertEqual(self.levels(self.track(content)), ["info"])
        self.assertEqual(self.track(content), [])                       # same size: quiet
        self.serve_deb("/files/demo_2.1_amd64.deb", "2.1", files={"usr/share/demo/x": b"y" * 5000})
        problems = self.track(content)
        self.assertEqual(self.levels(problems), ["alert"])
        self.assertIn("same version, different file", problems[0]["message"])
        self.assertEqual(len(self.st.candidates(app="v1")), 1)

    def test_messages_have_no_control_characters(self):
        problem = trackers._problem("alert", "v1", "a", "bad \x1b[31mtext\nline\tok")
        self.assertEqual(problem["message"], "bad [31mtextline\tok")

    def test_one_failing_vendor_does_not_stop_the_others(self):
        self.serve_deb("/dl/demo.deb", "2.0")
        good = {"type": "head", "url": self.server.base + "/dl/demo.deb"}
        bad = {"type": "head", "url": self.server.base + "/nothing"}
        content = self.content({"a-bad": vendor_def(bad), "b-good": vendor_def(good)})
        problems = self.track(content)
        self.assertEqual(self.levels(problems), ["error", "info"])
        self.assertEqual(len(self.st.candidates(app="b-good")), 1)
        self.assertIsNotNone(self.st.vendor_memo("a-bad")["last_error"])
        self.assertIsNone(self.st.vendor_memo("b-good")["last_error"])

    def test_vendor_without_exactly_one_app_is_an_error(self):
        track = {"type": "head", "url": self.server.base + "/dl/demo.deb"}
        content = self.content({"v1": vendor_def(track)}, apps={})
        self.assertEqual(self.levels(self.track(content)), ["error"])

    def test_unknown_vendor(self):
        self.assertEqual(self.levels(self.track(self.content({}), vendor="nope")), ["error"])


@unittest.skipUnless(DPKG, "dpkg-deb not available")
class IntakeTests(TrackerBase):
    def json_vendor(self, version="2.1"):
        self.server.routes["/latest.json"] = fixtures.Route(
            body=('{"version": "%s", "url": "/x.deb"}' % version).encode())
        track = {"type": "json", "url": self.server.base + "/latest.json",
                 "version_path": "version", "url_path": "url"}
        return self.content({"v1": vendor_def(track, "manual")})

    def test_manual_awaits_upload_then_intake_completes_it(self):
        content = self.json_vendor()
        self.assertEqual(self.levels(self.track(content)), ["info"])
        (waiting,) = self.st.candidates(app="v1", status="awaiting-upload")
        self.assertEqual(waiting["version"], "2.1")
        self.assertEqual(self.track(content), [])           # still just one
        deb = fixtures.build_deb(self.tmp.name, "demo", "2.1", files={"usr/bin/demo": b"x"},
                                 modes={"usr/bin/demo": 0o6755})
        cand = trackers.intake(self.cfg, self.st, content, "v1", deb, "https://127.0.0.1/download")
        self.assertEqual((cand["status"], cand["origin"]["type"]), ("pending", "manual"))
        self.assertEqual(cand["origin"]["url"], "https://127.0.0.1/download")
        self.assertIn("setuid usr/bin/demo", cand["checks"]["inspect"]["flags"])
        self.assertIn("setgid usr/bin/demo", cand["checks"]["inspect"]["flags"])
        self.assertEqual(self.st.candidates(app="v1", status="awaiting-upload"), [])
        self.assertEqual(len(self.st.candidates(app="v1")), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.cfg.downloads, cand["file"])))

    def test_intake_rejects_wrong_package_arch_and_version(self):
        content = self.json_vendor()
        self.track(content)
        for kwargs in ({"package": "other", "version": "2.1"}, {"package": "demo", "version": "2.0"},
                       {"package": "demo", "version": "2.1", "arch": "arm64"}):
            deb = fixtures.build_deb(self.tmp.name, kwargs["package"], kwargs["version"],
                                     arch=kwargs.get("arch", "amd64"))
            with self.assertRaises(trackers.TrackError, msg=str(kwargs)):
                trackers.intake(self.cfg, self.st, content, "v1", deb, None)

    def test_intake_without_awaiting_must_be_higher_than_approved(self):
        content = self.json_vendor()
        self.st.set_approved("v1", {"version": "2.0", "sha256": "0" * 64}, {"event": "approve"})
        low = fixtures.build_deb(self.tmp.name, "demo", "1.9")
        with self.assertRaises(trackers.TrackError):
            trackers.intake(self.cfg, self.st, content, "v1", low, None)
        high = fixtures.build_deb(self.tmp.name, "demo", "2.5")
        cand = trackers.intake(self.cfg, self.st, content, "v1", high, None)
        self.assertEqual(cand["version"], "2.5")

    def test_intake_same_file_twice_returns_existing(self):
        content = self.json_vendor()
        deb = fixtures.build_deb(self.tmp.name, "demo", "2.5")
        first = trackers.intake(self.cfg, self.st, content, "v1", deb, None)
        second = trackers.intake(self.cfg, self.st, content, "v1", deb, None)
        self.assertEqual(first["_path"], second["_path"])

    def test_intake_url_must_be_https_on_an_allowed_host(self):
        content = self.json_vendor()
        deb = fixtures.build_deb(self.tmp.name, "demo", "2.5")
        for url in ("http://127.0.0.1/x", "https://evil.example/x", "file:///etc/passwd",
                    "https://user:pw@127.0.0.1/x", "javascript:alert(1)"):
            with self.assertRaises(trackers.TrackError, msg=url):
                trackers.intake(self.cfg, self.st, content, "v1", deb, url)
        self.assertEqual(self.st.candidates(app="v1"), [])
        self.assertEqual(trackers.intake(self.cfg, self.st, content, "v1", deb, None)["version"], "2.5")

    def test_manual_head_is_refused(self):
        track = {"type": "head", "url": self.server.base + "/x"}
        content = self.content({"v1": vendor_def(track, "manual")})
        self.assertEqual(self.levels(self.track(content)), ["error"])


@unittest.skipUnless(GPG, "gpg, gpgv or dpkg-deb missing")
class AptTrackerTests(TrackerBase):
    @classmethod
    def setUpClass(cls):
        cls.keydir = tempfile.TemporaryDirectory()
        cls.key = fixtures.gen_key(cls.keydir.name)
        cls.other = fixtures.gen_key(cls.keydir.name, "Other")

    @classmethod
    def tearDownClass(cls):
        fixtures.stop_agent(cls.key)
        fixtures.stop_agent(cls.other)
        cls.keydir.cleanup()

    def publish(self, versions, date=fixtures.RELEASE_DATE, **kw):
        repo = os.path.join(self.tmp.name, "repo")
        debs = [fixtures.build_deb(self.tmp.name, "demo", v, **kw) for v in versions]
        fixtures.write_apt_repo(repo, self.key, debs, date=date)
        self.server.mount(repo, "/apt")

    def apt_vendor(self, fetch_mode="auto", fingerprint=None):
        rel = fixtures.install_key(self.root, self.key)
        track = {"type": "apt", "url": self.server.base + "/apt", "suite": "stable", "component": "main",
                 "keys": [rel], "fingerprints": [fingerprint or self.key.fingerprint]}
        return vendor_def(track, fetch_mode)

    def test_new_candidate_records_origin_and_is_idempotent(self):
        self.publish(["1.0", "1.1"])
        content = self.content({"v1": self.apt_vendor()})
        self.assertEqual(self.levels(self.track(content)), ["info"])
        (cand,) = self.st.candidates(app="v1")
        self.assertEqual(cand["version"], "1.1")
        self.assertEqual(cand["origin"]["type"], "apt")
        self.assertEqual(cand["origin"]["signed_by"], self.key.fingerprint)
        self.assertEqual(len(cand["origin"]["inrelease_sha256"]), 64)
        memo = self.st.vendor_memo("v1")
        self.assertEqual((memo["inrelease_date"], memo["seen_version"]), (fixtures.RELEASE_ISO, "1.1"))
        gets = self.server.count("GET", "/apt/pool/demo_1.1_amd64.deb")
        self.assertEqual(self.track(content), [])
        self.assertEqual(self.server.count("GET", "/apt/pool/demo_1.1_amd64.deb"), gets)

    def test_same_version_different_sha256_alerts_without_new_candidate(self):
        self.publish(["1.0"])
        content = self.content({"v1": self.apt_vendor()})
        self.track(content)
        self.publish(["1.0"], date=fixtures.release_date(datetime.timedelta(minutes=30)),
                     files={"usr/share/demo/README": b"changed\n"})
        problems = self.track(content)
        self.assertEqual(self.levels(problems), ["alert"])
        self.assertEqual(len(self.st.candidates(app="v1")), 1)
        self.assertIn("different sha256", self.st.vendor_memo("v1")["last_error"])

    def test_rollback_is_an_error_and_recorded(self):
        self.publish(["1.0"])
        self.st.save_vendor_memo("v1", {"inrelease_date": "2030-01-01T00:00:00Z"})
        problems = self.track(self.content({"v1": self.apt_vendor()}))
        self.assertEqual(self.levels(problems), ["error"])
        self.assertEqual(self.st.candidates(app="v1"), [])
        self.assertEqual(self.st.vendor_memo("v1")["inrelease_date"], "2030-01-01T00:00:00Z")

    def test_wrong_fingerprint_is_an_error(self):
        self.publish(["1.0"])
        content = self.content({"v1": self.apt_vendor(fingerprint=self.other.fingerprint)})
        self.assertEqual(self.levels(self.track(content)), ["error"])
        self.assertEqual(self.st.candidates(app="v1"), [])

    def test_manual_apt_waits_for_upload(self):
        self.publish(["1.0"])
        content = self.content({"v1": self.apt_vendor("manual")})
        self.assertEqual(self.levels(self.track(content)), ["info"])
        (cand,) = self.st.candidates(app="v1", status="awaiting-upload")
        self.assertEqual(cand["origin"]["type"], "apt")
        self.assertEqual(self.server.count("GET", "/apt/pool/demo_1.0_amd64.deb"), 0)

    def test_diff_against_approved_candidate(self):
        self.publish(["1.0"], scripts={"postinst": "#!/bin/sh\ntrue\n"})
        content = self.content({"v1": self.apt_vendor()})
        self.track(content)
        (first,) = self.st.candidates(app="v1")
        self.assertIsNone(first["checks"]["diff"]["against"])
        self.st.set_approved("v1", {"version": "1.0", "sha256": first["sha256"]}, {"event": "approve"})
        self.publish(["1.1"], date=fixtures.release_date(datetime.timedelta(minutes=30)),
                     scripts={"postinst": "#!/bin/sh\nsystemctl enable demo\n"})
        self.assertEqual(self.levels(self.track(content)), ["info"])
        second = self.st.find_candidate("v1", "1.1")
        diff = second["checks"]["diff"]
        self.assertEqual((diff["against"], diff["scripts_changed"]), ("1.0", ["postinst"]))
        self.assertIn("postinst mentions systemctl enable", diff["flags_added"])
        with open(os.path.join(self.st.inspect_dir(second), "scripts.diff"), encoding="utf-8") as f:
            self.assertIn("+systemctl enable demo", f.read())


if __name__ == "__main__":
    unittest.main()
