# SPDX-License-Identifier: GPL-3.0-or-later
"""source.tar of a recipe v2 task: every source fetched on the host, pinned or checksummed."""

import gzip
import hashlib
import os
import shutil
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures  # noqa: E402
from agino_store_server import source_assembly as S  # noqa: E402
from agino_store_server import upstream_fetch as U  # noqa: E402

from . import build_helpers as B  # noqa: E402
from . import helpers as H  # noqa: E402
from .test_upstream_fetch import make_tar  # noqa: E402

GIT = shutil.which("git")


def sha(data):
    return hashlib.sha256(data).hexdigest()


@unittest.skipUnless(GIT, "git not available")
class AssemblyCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = self._tmp.name
        self.server = fixtures.TestServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.repo = os.path.join(self.base, "upstream")
        self.commit = B.make_repo(self.repo, {"README": b"hello\n", "src/main.c": b"int main(void){return 0;}\n"},
                                  tags=("v1.0.0",))
        raw = make_tar(os.path.join(self.base, "raw.tar"), [("vendor-1.0", "dir", None),
                                                           ("vendor-1.0/bin/tool", "file", b"#!/bin/sh\n")])
        with open(raw, "rb") as f:
            self.tarball = gzip.compress(f.read())
        self.blob = b"\x7fELF-ish blob"
        self.server.routes["/dl/vendor-1.0.tar.gz"] = fixtures.Route(body=self.tarball)
        self.server.routes["/dl/app.AppImage"] = fixtures.Route(body=self.blob)
        self.cfg = H.make_config(self.base, allow_insecure=True, upstream_allowed_hosts=("127.0.0.1",))
        self.work = os.path.join(self.base, "work")
        os.mkdir(self.work)
        self.dest = os.path.join(self.base, "source.tar")

    def git_source(self, **kw):
        return dict({"name": "src", "kind": "git", "url": "file://" + self.repo, "ref": "v1.0.0"}, **kw)

    def tar_source(self, **kw):
        return dict({"name": "vendor", "kind": "tarball", "url": self.server.base + "/dl/vendor-1.0.tar.gz",
                     "sha256": sha(self.tarball)}, **kw)

    def file_source(self, **kw):
        return dict({"name": "blob", "kind": "file", "url": self.server.base + "/dl/app.AppImage",
                     "sha256": sha(self.blob)}, **kw)

    def record(self, *sources, **extra):
        return dict({"recipe": {"sources": list(sources)}}, **extra)

    def fetch(self, *sources, **extra):
        return S.fetch_sources(self.cfg, self.record(*sources, **extra), self.work, self.dest)

    def names(self):
        with tarfile.open(self.dest) as tar:
            return {m.name: m for m in tar}


class TestLayout(AssemblyCase):
    def test_one_directory_per_source(self):
        digest, size, fetched = self.fetch(self.git_source(), self.tar_source(), self.file_source())
        found = self.names()
        self.assertEqual(sorted(n for n in found if "/" not in n), ["blob", "src", "vendor"])
        self.assertIn("src/src/main.c", found)
        self.assertIn("src/README", found)
        self.assertIn("vendor/bin/tool", found)            # the single top-level directory is removed
        self.assertIn("blob/app.AppImage", found)
        self.assertEqual(found["blob/app.AppImage"].size, len(self.blob))
        self.assertEqual(digest, hashlib.sha256(open(self.dest, "rb").read()).hexdigest())
        self.assertEqual(size, os.path.getsize(self.dest))
        self.assertEqual(fetched["src"], {"kind": "git", "commit": self.commit})
        self.assertEqual(fetched["vendor"], {"kind": "tarball", "sha256": sha(self.tarball)})
        self.assertEqual(fetched["blob"]["kind"], "file")

    def test_owners_and_modes_are_normalised(self):
        self.fetch(self.git_source(), self.file_source())
        for member in self.names().values():
            self.assertEqual((member.uid, member.gid), (0, 0))
            self.assertIn(member.mode, (0o644, 0o755))

    def test_the_work_directory_is_left_empty(self):
        self.fetch(self.git_source(), self.tar_source())
        self.assertEqual(os.listdir(self.work), [])

    def test_a_commit_ref_pins_itself(self):
        digest, _, fetched = self.fetch(self.git_source(ref=self.commit))
        self.assertEqual(fetched["src"]["commit"], self.commit)

    def test_no_sources_is_an_error(self):
        with self.assertRaises(U.UpstreamError):
            self.fetch()


class TestFailures(AssemblyCase):
    def assertRecipeFault(self, *sources, needle="", **extra):
        with self.assertRaises(U.UpstreamError) as ctx:
            self.fetch(*sources, **extra)
        self.assertEqual(ctx.exception.klass, "recipe", str(ctx.exception))
        self.assertIn(needle, str(ctx.exception))
        self.assertFalse(os.path.exists(self.dest))
        self.assertEqual(os.listdir(self.work), [])

    def test_wrong_sha256_of_a_tarball(self):
        self.assertRecipeFault(self.tar_source(sha256="0" * 64), needle="sha256 mismatch")

    def test_wrong_sha256_of_a_file(self):
        self.assertRecipeFault(self.git_source(), self.file_source(sha256="1" * 64), needle="sha256 mismatch")

    def test_a_file_that_has_gone(self):
        self.assertRecipeFault(self.file_source(url=self.server.base + "/dl/gone.bin"), needle="HTTP 404")

    def test_a_tag_that_does_not_exist(self):
        self.assertRecipeFault(self.git_source(ref="v9.9.9"), needle="remote ref")

    def test_a_moved_tag_is_not_the_recipes_fault(self):
        with self.assertRaises(U.UpstreamError) as ctx:
            self.fetch(self.git_source(), pins={"src": "f" * 40})
        self.assertEqual(ctx.exception.klass, "infra")
        self.assertIn("now points to", str(ctx.exception))

    def test_a_matching_pin_is_fine(self):
        self.fetch(self.git_source(), pins={"src": self.commit})

    def test_host_not_on_the_list(self):
        cfg = H.make_config(self.base, allow_insecure=True, upstream_allowed_hosts=("example.org",))
        with self.assertRaises(U.UpstreamError):
            S.fetch_sources(cfg, self.record(self.file_source()), self.work, self.dest)

    def test_task_level_extra_host(self):
        cfg = H.make_config(self.base, allow_insecure=True, upstream_allowed_hosts=("example.org",))
        S.fetch_sources(cfg, self.record(self.file_source(), allow_hosts=["127.0.0.1"]), self.work, self.dest)
        self.assertIn("blob/app.AppImage", self.names())

    def test_size_limit_of_all_sources_together(self):
        cfg = H.make_config(self.base, allow_insecure=True, upstream_allowed_hosts=("127.0.0.1",),
                            builds_max_source_mib=1)
        big = os.urandom(700 << 10)
        self.server.routes["/dl/a.bin"] = fixtures.Route(body=big)
        self.server.routes["/dl/b.bin"] = fixtures.Route(body=big)
        sources = [self.file_source(name="a", url=self.server.base + "/dl/a.bin", sha256=sha(big)),
                   self.file_source(name="b", url=self.server.base + "/dl/b.bin", sha256=sha(big))]
        with self.assertRaises(U.UpstreamError):
            S.fetch_sources(cfg, self.record(*sources), self.work, self.dest)
        self.assertFalse(os.path.exists(self.dest))

    def test_unsafe_tarball_content_is_refused(self):
        raw = make_tar(os.path.join(self.base, "bad.tar"), [("pkg", "dir", None), ("pkg/evil", "sym", "../../etc/passwd")])
        with open(raw, "rb") as f:
            body = f.read()
        self.server.routes["/dl/bad.tar"] = fixtures.Route(body=body)
        with self.assertRaises(U.UpstreamError):
            self.fetch(self.tar_source(url=self.server.base + "/dl/bad.tar", sha256=sha(body)))


class TestHelpers(unittest.TestCase):
    def test_hosts_for(self):
        cfg = H.make_config("/nonexistent", upstream_allowed_hosts=("github.com",))
        self.assertEqual(S.hosts_for(cfg, {"allow_hosts": ["dl.vendor.example", "BAD host", 5, "github.com"]}),
                         ("github.com", "dl.vendor.example"))
        self.assertEqual(S.hosts_for(cfg, {}), ("github.com",))

    def test_pin_map(self):
        sources = [{"name": "src", "kind": "git", "url": "https://github.com/a/b.git", "ref": "v1"},
                   {"name": "other", "kind": "git", "url": "https://github.com/c/d.git", "ref": "v1"},
                   {"name": "exact", "kind": "git", "url": "https://github.com/e/f.git", "ref": "a" * 40},
                   {"name": "blob", "kind": "file", "url": "https://github.com/x/y", "sha256": "b" * 64}]
        self.assertEqual(S.pin_map(sources, "https://github.com/a/b.git", "v1", "c" * 40),
                         {"src": "c" * 40, "exact": "a" * 40})
        self.assertEqual(S.pin_map(sources, None, None, None), {"exact": "a" * 40})


if __name__ == "__main__":
    unittest.main()
