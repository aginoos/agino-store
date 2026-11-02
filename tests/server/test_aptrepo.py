# SPDX-License-Identifier: GPL-3.0-or-later
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
        self.assertEqual(res.inrelease_date, "2026-11-02T08:00:00Z")
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
        self.server.routes[path] = fixtures.Route(body=b"Date: Mon, 02 Nov 2026 08:00:00 UTC\nSHA256:\n")
        with self.assertRaises(aptrepo.AptError):
            self.find()

    def test_rollback_rejected(self):
        with self.assertRaises(aptrepo.AptError):
            self.find(memo={"inrelease_date": "2026-11-03T00:00:00Z"})
        self.assertEqual(self.find(memo={"inrelease_date": "2026-11-02T08:00:00Z"}).version, "1.10")

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
