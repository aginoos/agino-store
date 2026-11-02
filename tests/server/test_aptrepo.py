# SPDX-License-Identifier: GPL-3.0-or-later
import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures  # noqa: E402
from agino_store_server import aptrepo, fetch  # noqa: E402

TOOLS = fixtures.have("gpg", "gpgv", "dpkg-deb", "dpkg")


@unittest.skipUnless(TOOLS, "gpg, gpgv or dpkg-deb missing")
class AptRepoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keydir = tempfile.TemporaryDirectory()
        cls.key = fixtures.gen_key(cls.keydir.name)
        cls.other = fixtures.gen_key(cls.keydir.name, "Other Vendor")

    @classmethod
    def tearDownClass(cls):
        fixtures.stop_agent(cls.key)
        fixtures.stop_agent(cls.other)
        cls.keydir.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "repo")
        self.content_root = os.path.join(self.tmp.name, "content")
        self.key_rel = fixtures.install_key(self.content_root, self.key)
        debs = [fixtures.build_deb(self.tmp.name, "demo", v) for v in ("1.9", "1.10", "1.2")]
        debs.append(fixtures.build_deb(self.tmp.name, "other", "9.0"))
        debs.append(fixtures.build_deb(self.tmp.name, "demo", "5.0", arch="arm64"))
        fixtures.write_apt_repo(self.repo, self.key, debs)
        self.server = fixtures.TestServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.server.mount(self.repo, "/apt")
        self.cfg = fixtures.make_cfg(self.tmp.name, self.server.base)
        self.fetcher = fetch.Fetcher(self.cfg, ["127.0.0.1"])

    def vendor(self, **track):
        base = {"type": "apt", "url": self.server.base + "/apt", "suite": "stable", "component": "main",
                "keys": [self.key_rel], "fingerprints": [self.key.fingerprint]}
        base.update(track)
        return {"package": "demo", "arch": "amd64", "fetch": "auto", "allowed_hosts": ["127.0.0.1"],
                "track": base}

    def find(self, vendor=None, memo=None):
        return aptrepo.find_latest(self.cfg, self.fetcher, vendor or self.vendor(),
                                   self.content_root, memo or {})

    def test_picks_highest_version_with_dpkg_order(self):
        res = self.find()
        self.assertEqual(res.version, "1.10")
        self.assertEqual(res.signed_by, self.key.fingerprint)
        self.assertEqual(res.inrelease_date, fixtures.RELEASE_ISO)
        self.assertTrue(res.deb_url.endswith("/apt/pool/demo_1.10_amd64.deb"))

    def test_download_deb_verifies_hash(self):
        res = self.find()
        dl = aptrepo.download_deb(self.cfg, self.fetcher, res)
        self.assertEqual(dl.sha256, res.sha256)
        self.assertTrue(os.path.isfile(dl.path))

    def test_wrong_fingerprint_rejected(self):
        with self.assertRaises(aptrepo.AptError):
            self.find(self.vendor(fingerprints=[self.other.fingerprint]))

    def test_key_not_in_keyring_rejected(self):
        other_rel = fixtures.install_key(self.content_root, self.other, "other.asc")
        with self.assertRaises(aptrepo.AptError):
            self.find(self.vendor(keys=[other_rel]))

    def test_key_path_traversal_rejected(self):
        with self.assertRaises(aptrepo.AptError):
            self.find(self.vendor(keys=["../../" + os.path.basename(self.key.asc)]))

    def test_tampered_inrelease_rejected(self):
        path = "/apt/dists/stable/InRelease"
        body = self.server.routes[path].body.replace(b"Origin: Test", b"Origin: Evil")
        self.server.routes[path] = fixtures.Route(body=body)
        with self.assertRaises(aptrepo.AptError):
            self.find()

    def test_unsigned_release_text_not_trusted(self):
        path = "/apt/dists/stable/InRelease"
        self.server.routes[path] = fixtures.Route(body=("Date: %s\nSHA256:\n" % fixtures.RELEASE_DATE).encode())
        with self.assertRaises(aptrepo.AptError):
            self.find()

    def test_rollback_rejected(self):
        later = (datetime.datetime.fromisoformat(fixtures.RELEASE_ISO) + datetime.timedelta(hours=1))
        with self.assertRaises(aptrepo.AptError):
            self.find(memo={"inrelease_date": later.strftime("%Y-%m-%dT%H:%M:%SZ")})
        self.assertEqual(self.find(memo={"inrelease_date": fixtures.RELEASE_ISO}).version, "1.10")

    def test_equal_date_with_other_content_rejected(self):
        res = self.find()
        memo = {"inrelease_date": res.inrelease_date, "inrelease_sha256": res.inrelease_sha256}
        self.assertEqual(self.find(memo=memo).version, "1.10")
        with self.assertRaises(aptrepo.AptError):
            self.find(memo=dict(memo, inrelease_sha256="0" * 64))

    def test_wrong_suite_rejected(self):
        debs = [fixtures.build_deb(self.tmp.name, "demo", "1.0")]
        fixtures.write_apt_repo(self.repo, self.key, debs, suite="testing")
        self.server.mount(self.repo, "/apt")
        # a validly signed InRelease of another suite served where "stable" is requested
        self.server.routes["/apt/dists/stable/InRelease"] = self.server.routes["/apt/dists/testing/InRelease"]
        with self.assertRaises(aptrepo.AptError):
            self.find()

    def test_codename_match_accepted(self):
        debs = [fixtures.build_deb(self.tmp.name, "demo", "3.0")]
        fixtures.write_apt_repo(self.repo, self.key, debs, suite="stable-updates", codename="stable")
        self.server.mount(self.repo, "/apt")
        self.server.routes["/apt/dists/stable/InRelease"] = self.server.routes["/apt/dists/stable-updates/InRelease"]
        for suffix in ("Packages", "Packages.gz"):
            self.server.routes["/apt/dists/stable/main/binary-amd64/" + suffix] = \
                self.server.routes["/apt/dists/stable-updates/main/binary-amd64/" + suffix]
        self.assertEqual(self.find().version, "3.0")

    def test_expired_and_stale_release_rejected(self):
        for kw in ({"release_extra": ["Valid-Until: " + fixtures.release_date(datetime.timedelta(hours=1))]},
                   {"date": fixtures.release_date(datetime.timedelta(days=31))}):
            self.write_signed(**kw)
            with self.assertRaises(aptrepo.AptError, msg=str(kw)):
                self.find()
        self.write_signed(release_extra=["Valid-Until: " + fixtures.release_date(-datetime.timedelta(days=5))])
        self.assertEqual(self.find().version, "1.0")

    def write_signed(self, **kw):
        debs = [fixtures.build_deb(self.tmp.name, "demo", "1.0")]
        fixtures.write_apt_repo(self.repo, self.key, debs, **kw)
        self.server.mount(self.repo, "/apt")

    def test_check_release_unit(self):
        rel = aptrepo.Release(datetime.datetime.now(datetime.UTC), {}, "stable", "trixie", None)
        aptrepo.check_release(rel, "stable")
        aptrepo.check_release(rel, "trixie")
        with self.assertRaises(aptrepo.AptError):
            aptrepo.check_release(rel, "other")

    def test_packages_hash_mismatch_rejected(self):
        for suffix in (".gz", ""):
            path = "/apt/dists/stable/main/binary-amd64/Packages" + suffix
            self.server.routes[path] = fixtures.Route(body=b"Package: demo\nVersion: 99\n")
        with self.assertRaises(aptrepo.AptError):
            self.find()

    def test_deb_hash_mismatch_rejected(self):
        res = self.find()
        self.server.routes["/apt/pool/demo_1.10_amd64.deb"] = fixtures.Route(body=b"evil")
        with self.assertRaises(fetch.FetchError):
            aptrepo.download_deb(self.cfg, self.fetcher, res)

    def test_package_missing(self):
        vendor = dict(self.vendor(), package="nothing")
        with self.assertRaises(aptrepo.AptError):
            self.find(vendor)

    def test_parse_release_needs_date(self):
        with self.assertRaises(aptrepo.AptError):
            aptrepo.parse_release(b"Suite: stable\n")


if __name__ == "__main__":
    unittest.main()
