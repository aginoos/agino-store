# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures  # noqa: E402
from agino_store_server import fetch  # noqa: E402


class SanitizeTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(fetch.sanitize_filename("a/b/c.deb"), "c.deb")
        self.assertEqual(fetch.sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(fetch.sanitize_filename("..%2F..%2Fx.deb"), "x.deb")
        self.assertEqual(fetch.sanitize_filename("we ird$name.deb"), "we_ird_name.deb")
        self.assertEqual(fetch.sanitize_filename(".."), "download")
        self.assertEqual(fetch.sanitize_filename(".hidden"), "hidden")

    def test_check_url(self):
        allowed = frozenset(["example.org"])
        fetch.check_url("https://example.org/x", allowed, False)
        for url in ("http://example.org/x", "file:///etc/passwd", "https://evil.example/x",
                    "https://user:pw@example.org/x", "ftp://example.org/x"):
            with self.assertRaises(fetch.FetchError, msg=url):
                fetch.check_url(url, allowed, False)
        with self.assertRaises(fetch.FetchError):
            fetch.check_url("http://example.org/x", allowed, True, previous_scheme="https")
        fetch.check_url("http://example.org/x", allowed, True)


class FetcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = fixtures.TestServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.cfg = fixtures.make_cfg(self.tmp.name)
        self.fetcher = fetch.Fetcher(self.cfg, ["127.0.0.1"])
        self.downloads = os.path.join(self.tmp.name, "dl")

    def route(self, path, **kw):
        self.server.routes[path] = fixtures.Route(**kw)

    def test_get_sends_honest_user_agent(self):
        self.route("/a", body=b"hello", etag='"e1"')
        resp = self.fetcher.get(self.server.base + "/a")
        self.assertEqual((resp.data, resp.etag), (b"hello", '"e1"'))
        self.assertEqual(self.server.hits[-1][2]["User-Agent"], self.cfg.user_agent)

    def test_conditional_get_not_modified(self):
        self.route("/a", body=b"hello", etag='"e1"')
        resp = self.fetcher.get(self.server.base + "/a", etag='"e1"')
        self.assertTrue(resp.not_modified)
        self.assertEqual(self.server.hits[-1][2]["If-None-Match"], '"e1"')

    def test_head(self):
        self.route("/a", body=b"hello", etag='"e1"',
                   headers={"Last-Modified": "Mon, 02 Nov 2026 08:00:00 GMT"})
        resp = self.fetcher.head(self.server.base + "/a")
        self.assertEqual((resp.etag, resp.content_length, resp.data), ('"e1"', 5, b""))
        self.assertEqual(resp.last_modified, "Mon, 02 Nov 2026 08:00:00 GMT")

    def test_http_refused_without_allow_insecure(self):
        strict = fetch.Fetcher(fixtures.make_cfg(self.tmp.name, allow_insecure=False), ["127.0.0.1"])
        with self.assertRaises(fetch.FetchError):
            strict.get(self.server.base + "/a")

    def test_host_not_allowed(self):
        other = fetch.Fetcher(self.cfg, ["example.org"])
        with self.assertRaises(fetch.FetchError):
            other.get(self.server.base + "/a")

    def test_redirect_within_allowed_hosts(self):
        self.route("/old", redirect=self.server.base + "/new")
        self.route("/new", body=b"data")
        resp = self.fetcher.get(self.server.base + "/old")
        self.assertEqual((resp.data, resp.url), (b"data", self.server.base + "/new"))

    def test_redirect_to_other_host_refused(self):
        evil = self.server.base.replace("127.0.0.1", "localhost") + "/new"
        self.route("/old", redirect=evil)
        self.route("/new", body=b"data")
        with self.assertRaises(fetch.FetchError):
            self.fetcher.get(self.server.base + "/old")
        self.assertEqual(self.server.count("GET", "/new"), 0)

    def test_redirect_to_file_refused(self):
        self.route("/old", redirect="file:///etc/hostname")
        with self.assertRaises(fetch.FetchError):
            self.fetcher.get(self.server.base + "/old")

    def test_size_cap(self):
        self.route("/big", body=b"x" * 5000)
        with self.assertRaises(fetch.FetchTooLarge):
            self.fetcher.get(self.server.base + "/big", max_bytes=1000)
        with self.assertRaises(fetch.FetchTooLarge):
            self.fetcher.download(self.server.base + "/big", self.downloads, max_bytes=1000)

    def test_download_stores_by_hash_and_reuses(self):
        self.route("/files/My%20App.deb", body=b"payload")
        dl = self.fetcher.download(self.server.base + "/files/My%20App.deb", self.downloads)
        sha = hashlib.sha256(b"payload").hexdigest()
        self.assertEqual(dl.sha256, sha)
        self.assertEqual(dl.path, os.path.join(self.downloads, sha, "My_App.deb"))
        again = self.fetcher.download(self.server.base + "/files/My%20App.deb", self.downloads,
                                      filename="My_App.deb", expected_sha256=sha)
        self.assertTrue(again.reused)
        self.assertEqual(self.server.count("GET", "/files/My%20App.deb"), 1)

    def test_download_hash_mismatch_leaves_nothing(self):
        self.route("/f.deb", body=b"payload")
        with self.assertRaises(fetch.FetchError):
            self.fetcher.download(self.server.base + "/f.deb", self.downloads, expected_sha256="0" * 64)
        self.assertEqual(os.listdir(self.downloads), [])

    def test_download_not_modified(self):
        self.route("/f.deb", body=b"payload", etag='"z"')
        dl = self.fetcher.download(self.server.base + "/f.deb", self.downloads, etag='"z"')
        self.assertTrue(dl.not_modified)
        self.assertIsNone(dl.path)

    def test_http_error(self):
        with self.assertRaises(fetch.FetchError):
            self.fetcher.get(self.server.base + "/missing")

    def test_per_host_interval(self):
        slow = fetch.Fetcher(fixtures.make_cfg(self.tmp.name, min_interval_seconds=0.3), ["127.0.0.1"])
        self.route("/a", body=b"1")
        slow.get(self.server.base + "/a")
        start = time.monotonic()
        slow.get(self.server.base + "/a")
        self.assertGreaterEqual(time.monotonic() - start, 0.2)

    def test_file_scheme_when_insecure(self):
        path = os.path.join(self.tmp.name, "local.deb")
        with open(path, "wb") as f:
            f.write(b"local")
        dl = fetch.Fetcher(self.cfg, []).download("file://" + path, self.downloads)
        self.assertEqual(dl.size, 5)

    def test_store_file(self):
        src = os.path.join(self.tmp.name, "a b.deb")
        with open(src, "wb") as f:
            f.write(b"abc")
        path, sha, size = fetch.store_file(src, self.downloads, "a b.deb")
        self.assertEqual((size, os.path.basename(path)), (3, "a_b.deb"))
        self.assertEqual(path, os.path.join(self.downloads, sha, "a_b.deb"))


if __name__ == "__main__":
    unittest.main()
