# SPDX-License-Identifier: GPL-3.0-or-later
import gzip
import hashlib
import io
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures  # noqa: E402
from agino_store_server import upstream_fetch as U  # noqa: E402

from . import build_helpers as B  # noqa: E402
from . import helpers as H  # noqa: E402

GIT = shutil.which("git")
GIT_SOURCE = {"kind": "git", "checksum": "required", "url": None}


def make_tar(path, entries, mode="w"):
    """entries: list of (name, kind, data/target, extra dict) -> a tar file at path."""
    with tarfile.open(path, mode) as tar:
        for name, kind, payload, *rest in entries:
            extra = rest[0] if rest else {}
            info = tarfile.TarInfo(name)
            info.mode = extra.get("mode", 0o644)
            info.uid, info.gid = extra.get("uid", 1000), extra.get("gid", 1000)
            info.uname = "someone"
            if kind == "file":
                data = payload if isinstance(payload, bytes) else b""
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
                continue
            info.type = {"dir": tarfile.DIRTYPE, "sym": tarfile.SYMTYPE, "hard": tarfile.LNKTYPE,
                         "chr": tarfile.CHRTYPE, "blk": tarfile.BLKTYPE, "fifo": tarfile.FIFOTYPE}[kind]
            if kind in ("sym", "hard"):
                info.linkname = payload
            tar.addfile(info)
    return path


def listing(path):
    with tarfile.open(path) as tar:
        return {m.name: m for m in tar}


class TarCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def path(self, name):
        return os.path.join(self.dir, name)

    def normalise(self, entries, **kw):
        src = make_tar(self.path("in.tar"), entries)
        dst = self.path("out.tar")
        U.normalise_tar(src, dst, kw.pop("max_bytes", 1 << 20), **kw)
        return listing(dst)

    def rejected(self, entries, needle="", **kw):
        with self.assertRaises(U.UpstreamError) as ctx:
            self.normalise(entries, **kw)
        self.assertIn(needle, str(ctx.exception))


class TestNormalise(TarCase):
    def test_owners_and_special_bits_are_dropped(self):
        found = self.normalise([
            ("src", "dir", None, {"mode": 0o2775}), ("src/a.c", "file", b"int x;", {"mode": 0o600}),
            ("src/run.sh", "file", b"#!/bin/sh", {"mode": 0o4755}), ("src/link", "sym", "a.c")])
        self.assertEqual(sorted(found), ["src", "src/a.c", "src/link", "src/run.sh"])
        self.assertEqual((found["src"].mode, found["src/a.c"].mode, found["src/run.sh"].mode), (0o755, 0o644, 0o755))
        for member in found.values():
            self.assertEqual((member.uid, member.gid, member.uname, member.gname), (0, 0, "", ""))
        self.assertEqual(found["src/link"].linkname, "a.c")
        self.assertTrue(found["src/link"].issym())

    def test_leading_dot_slash_and_root_entry(self):
        found = self.normalise([("./", "dir", None), ("./a/", "dir", None), ("./a/b", "file", b"x")])
        self.assertEqual(sorted(found), ["a", "a/b"])

    def test_relative_symlinks_inside_the_tree_are_fine(self):
        found = self.normalise([("include", "dir", None), ("lib", "dir", None), ("lib/inc", "sym", "../include"),
                                ("lib/self", "sym", "."), ("dangling", "sym", "nothing/here")])
        self.assertIn("lib/inc", found)

    def test_absolute_and_parent_paths_are_rejected(self):
        for name in ("/etc/passwd", "../x", "a/../../x", "a/../b/../../x"):
            with self.subTest(name=name):
                self.rejected([(name, "file", b"x")], "unsafe path")

    def test_control_characters_in_names_are_rejected(self):
        for name in ("a\nb", "a\x1b[31mb"):
            with self.subTest(name=name):
                self.rejected([(name, "file", b"x")], "unsafe path")

    def test_special_files_and_hard_links_are_rejected(self):
        for kind, payload in (("chr", None), ("blk", None), ("fifo", None), ("hard", "a")):
            with self.subTest(kind=kind):
                self.rejected([("a", "file", b"x"), ("b", kind, payload)], "special file or hard link")

    def test_escaping_symlinks_are_rejected(self):
        cases = {
            "absolute": [("l", "sym", "/etc/passwd")],
            "parent": [("l", "sym", "..")],
            "deep parent": [("a", "dir", None), ("a/l", "sym", "../../x")],
            "empty": [("l", "sym", "")],
            "through a dir link": [("a", "sym", "."), ("l", "sym", "a/..")],
            "chain": [("a", "sym", "b"), ("b", "sym", "../x")],
            "loop": [("a", "sym", "b"), ("b", "sym", "a/x")],
        }
        for label, entries in cases.items():
            with self.subTest(label=label):
                self.rejected(entries)

    def test_nothing_below_a_symlink_or_a_file(self):
        self.rejected([("l", "sym", "elsewhere"), ("l/x", "file", b"x")], "below a symbolic link")
        self.rejected([("f", "file", b"x"), ("f/x", "file", b"x")], "below a file")

    def test_duplicates_are_rejected(self):
        self.rejected([("a", "file", b"1"), ("a", "file", b"2")], "duplicate")
        self.rejected([("a", "file", b"1"), ("./a", "file", b"2")], "duplicate")

    def test_size_limit(self):
        self.rejected([("big", "file", b"x" * 5000)], "size limit", max_bytes=4096)

    def test_entry_limit(self):
        with mock.patch.object(U, "MAX_MEMBERS", 3):
            self.rejected([("f%d" % i, "file", b"x") for i in range(5)], "too many entries")

    def test_garbage_is_not_a_tar(self):
        H.write(self.path("junk"), b"this is not a tar file" * 100)
        with self.assertRaises(U.UpstreamError) as ctx:
            U.normalise_tar(self.path("junk"), self.path("out"), 1 << 20)
        self.assertIn("not usable", str(ctx.exception))

    def test_compressed_input_and_strip_top(self):
        raw = make_tar(self.path("raw.tar"), [("proj-1.0", "dir", None), ("proj-1.0/a", "file", b"1"),
                                              ("proj-1.0/sub", "dir", None), ("proj-1.0/sub/b", "file", b"2")])
        with open(raw, "rb") as f, gzip.open(self.path("in.tar.gz"), "wb") as g:
            g.write(f.read())
        U.normalise_tar(self.path("in.tar.gz"), self.path("out.tar"), 1 << 20, strip_top=True)
        self.assertEqual(sorted(listing(self.path("out.tar"))), ["a", "sub", "sub/b"])
        U.normalise_tar(self.path("in.tar.gz"), self.path("out2.tar"), 1 << 20)
        self.assertEqual(sorted(listing(self.path("out2.tar"))), ["proj-1.0", "proj-1.0/a", "proj-1.0/sub", "proj-1.0/sub/b"])

    def test_no_strip_with_several_top_level_names(self):
        make_tar(self.path("in.tar"), [("a", "file", b"1"), ("b", "file", b"2")])
        U.normalise_tar(self.path("in.tar"), self.path("out.tar"), 1 << 20, strip_top=True)
        self.assertEqual(sorted(listing(self.path("out.tar"))), ["a", "b"])


class TestLsRemoteParsing(unittest.TestCase):
    def test_parse(self):
        c1, c2, c3 = "1" * 40, "2" * 40, "3" * 40
        text = ("%s\trefs/tags/v1.0.0\n%s\trefs/tags/v2.0.0\n%s\trefs/tags/v2.0.0^{}\n"
                "%s\trefs/heads/main\nnot a line\n%s\trefs/tags/bad\x1b[0mtag\n%s\trefs/tags/-x\n"
                "%s\trefs/tags/a..b\n") % (c1, c2, c3, c1, c1, c1, c1)
        self.assertEqual(U.parse_ls_remote(text), {"v1.0.0": c1, "v2.0.0": c3})

    def test_sha256_object_names(self):
        self.assertEqual(U.parse_ls_remote("%s\trefs/tags/v1\n" % ("a" * 64)), {"v1": "a" * 64})

    def test_too_many_tags(self):
        with mock.patch.object(U, "MAX_TAGS", 2):
            with self.assertRaises(U.UpstreamError):
                U.parse_ls_remote("".join("%s\trefs/tags/v%d\n" % ("a" * 40, i) for i in range(3)))


class TestGitHardening(unittest.TestCase):
    def test_environment_and_config(self):
        cfg = H.make_config("/nonexistent")
        env = U._git_env(cfg, "/tmp/home")
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertEqual((env["GIT_TERMINAL_PROMPT"], env["GIT_ALLOW_PROTOCOL"], env["GIT_CONFIG_GLOBAL"]),
                         ("0", "https", "/dev/null"))
        self.assertEqual(env["HOME"], "/tmp/home")
        self.assertFalse([k for k in env if k.startswith(("GIT_SSH", "SSH_AUTH", "http_proxy", "https_proxy"))])
        flags = U._git_config(cfg)
        for needed in ("protocol.allow=never", "protocol.https.allow=always", "core.hooksPath=/dev/null",
                       "core.fsmonitor=false", "http.followRedirects=false", "fetch.recurseSubmodules=false",
                       "transfer.fsckObjects=true"):
            self.assertIn(needed, flags)
        self.assertNotIn("protocol.file.allow=always", flags)
        self.assertEqual(U._git_env(H.make_config("/x", allow_insecure=True), "/h")["GIT_ALLOW_PROTOCOL"],
                         "https:file:http")

    def test_hosts_are_checked_before_git_runs(self):
        cfg = H.make_config("/nonexistent")
        for repo in ("http://github.com/a/b", "file:///tmp/x", "https://evil.example/a/b", "ssh://github.com/a/b",
                     "https://127.0.0.1/a/b", "https://user:pw@github.com/a/b"):
            with self.subTest(repo=repo):
                with self.assertRaises(U.UpstreamError):
                    U.check_repo(cfg, repo)

    def test_an_allowed_https_host_passes(self):
        U.check_repo(H.make_config("/nonexistent", upstream_allowed_hosts=("example.org",)), "https://example.org/a/b")


@unittest.skipUnless(GIT, "git not available")
class TestGitSource(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = self._tmp.name
        self.cfg = H.make_config(self.base, allow_insecure=True)
        self.repo = os.path.join(self.base, "upstream")
        self.url = "file://" + self.repo
        self.work = os.path.join(self.base, "queue-tmp")
        os.mkdir(self.work)
        self.commit = B.make_repo(self.repo, {
            "README": b"hello\n", "src/main.c": b"int main(void){return 0;}\n",
            "run.sh": ("exec", b"#!/bin/sh\n"), "link": ("symlink", "README")},
            tags=("v1.0.0", "v1.1.0"), annotated=("v1.1.0",))

    def fetch(self, tag="v1.0.0", commit=None, cfg=None, dest=None):
        dest = dest or os.path.join(self.base, "source.tar")
        return U.fetch_source(cfg or self.cfg, GIT_SOURCE, self.url, tag, commit or self.commit, self.work, dest)

    def test_list_tags(self):
        tags = U.list_tags(self.cfg, self.url, self.work)
        self.assertEqual(tags, {"v1.0.0": self.commit, "v1.1.0": self.commit})

    def test_annotated_tag_gives_the_commit(self):
        commit2 = B.commit_more(self.repo, "CHANGES", b"x")
        B.git(self.repo, "tag", "-a", "-m", "second", "v2.0.0")
        self.assertEqual(U.list_tags(self.cfg, self.url, self.work)["v2.0.0"], commit2)
        sha, size = self.fetch("v2.0.0", commit2)
        self.assertIn("CHANGES", listing(os.path.join(self.base, "source.tar")))
        self.assertEqual(size, os.path.getsize(os.path.join(self.base, "source.tar")))

    def test_fetch_builds_a_plain_source_tar(self):
        sha, size = self.fetch()
        dest = os.path.join(self.base, "source.tar")
        self.assertEqual(sha, hashlib.sha256(open(dest, "rb").read()).hexdigest())
        found = listing(dest)
        self.assertEqual(sorted(found), ["README", "link", "run.sh", "src", "src/main.c"])
        self.assertEqual((found["run.sh"].mode, found["README"].mode), (0o755, 0o644))
        self.assertEqual((found["README"].uid, found["README"].uname), (0, ""))
        self.assertTrue(found["link"].issym())
        self.assertEqual(os.listdir(self.work), [])   # nothing is left behind
        self.assertFalse([n for n in found if n.startswith(".git")])

    def test_fetch_is_reproducible(self):
        first = self.fetch(dest=os.path.join(self.base, "a.tar"))
        second = self.fetch(dest=os.path.join(self.base, "b.tar"))
        self.assertEqual(first, second)

    def test_moved_tag_is_refused(self):
        wrong = "0" * 40
        with self.assertRaises(U.UpstreamError) as ctx:
            self.fetch(commit=wrong)
        self.assertIn("now points to", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(self.base, "source.tar")))
        self.assertEqual(os.listdir(self.work), [])

    def test_missing_tag_is_refused(self):
        with self.assertRaises(U.UpstreamError):
            self.fetch(tag="v9.9.9")

    def test_bad_task_values_never_reach_git(self):
        for tag, commit in (("--upload-pack=x", self.commit), ("v1.0.0", "HEAD"), ("v1.0.0", "-x"), ("", self.commit)):
            with self.subTest(tag=tag, commit=commit):
                with self.assertRaises(U.UpstreamError):
                    self.fetch(tag=tag, commit=commit)

    def test_size_limit(self):
        B.commit_more(self.repo, "big.bin", os.urandom(2 << 20))
        B.git(self.repo, "tag", "v3.0.0")
        commit = B.git(self.repo, "rev-parse", "HEAD")
        small = H.make_config(self.base, allow_insecure=True, builds_max_source_mib=1)
        with self.assertRaises(U.UpstreamError):
            self.fetch("v3.0.0", commit, cfg=small)
        self.assertEqual(os.listdir(self.work), [])

    def test_escaping_symlink_in_the_repository_is_refused(self):
        B.commit_more(self.repo, "x", b"x")
        os.symlink("/etc/passwd", os.path.join(self.repo, "evil"))
        B.git(self.repo, "add", "-A")
        B.git(self.repo, "commit", "-q", "-m", "evil link")
        B.git(self.repo, "tag", "v4.0.0")
        with self.assertRaises(U.UpstreamError) as ctx:
            self.fetch("v4.0.0", B.git(self.repo, "rev-parse", "HEAD"))
        self.assertIn("unsafe symbolic link", str(ctx.exception))

    def test_hooks_and_config_of_the_upstream_are_not_used(self):
        marker = os.path.join(self.base, "hook-ran")
        hook = os.path.join(self.repo, ".git", "hooks", "post-checkout")
        H.write(hook, "#!/bin/sh\ntouch %s\n" % marker, "w")
        os.chmod(hook, 0o755)
        self.fetch()
        self.assertFalse(os.path.exists(marker))

    def test_failure_messages_have_no_control_characters(self):
        with self.assertRaises(U.UpstreamError) as ctx:
            U.list_tags(self.cfg, "file://" + os.path.join(self.base, "no\x1b[31mpe"), self.work)
        self.assertNotIn("\x1b", str(ctx.exception))


class TestTarballSource(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = self._tmp.name
        self.server = fixtures.TestServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.cfg = H.make_config(self.base, allow_insecure=True, upstream_allowed_hosts=("127.0.0.1",))
        raw = make_tar(os.path.join(self.base, "raw.tar"), [("proj", "dir", None), ("proj/a.c", "file", b"int a;")])
        with open(raw, "rb") as f:
            self.body = gzip.compress(f.read())
        self.server.routes["/proj-1.0.tar.gz"] = fixtures.Route(body=self.body)
        self.work = os.path.join(self.base, "tmp")
        os.mkdir(self.work)

    def source(self, checksum=None):
        return {"kind": "tarball", "url": self.server.base + "/proj-1.0.tar.gz",
                "checksum": checksum or hashlib.sha256(self.body).hexdigest()}

    def test_download_verify_and_repack(self):
        dest = os.path.join(self.base, "source.tar")
        sha, _ = U.fetch_source(self.cfg, self.source(), "https://github.com/a/b", "v1", "1" * 40, self.work, dest)
        self.assertEqual(sorted(listing(dest)), ["a.c"])
        self.assertEqual(sha, hashlib.sha256(open(dest, "rb").read()).hexdigest())
        self.assertEqual(os.listdir(self.work), [])

    def test_wrong_checksum(self):
        dest = os.path.join(self.base, "source.tar")
        with self.assertRaises(U.UpstreamError) as ctx:
            U.fetch_source(self.cfg, self.source("0" * 64), "https://github.com/a/b", "v1", "1" * 40, self.work, dest)
        self.assertIn("sha256 mismatch", str(ctx.exception))
        self.assertFalse(os.path.exists(dest))

    def test_host_must_be_allowed(self):
        cfg = H.make_config(self.base, allow_insecure=True, upstream_allowed_hosts=("example.org",))
        with self.assertRaises(U.UpstreamError):
            U.fetch_source(cfg, self.source(), "https://github.com/a/b", "v1", "1" * 40, self.work,
                           os.path.join(self.base, "source.tar"))

    def test_download_size_limit(self):
        cfg = H.make_config(self.base, allow_insecure=True, upstream_allowed_hosts=("127.0.0.1",),
                            builds_max_source_mib=1)
        big = gzip.compress(os.urandom(2 << 20))
        self.server.routes["/big.tar.gz"] = fixtures.Route(body=big)
        source = {"kind": "tarball", "url": self.server.base + "/big.tar.gz", "checksum": hashlib.sha256(big).hexdigest()}
        with self.assertRaises(U.UpstreamError):
            U.fetch_source(cfg, source, "https://github.com/a/b", "v1", "1" * 40, self.work,
                           os.path.join(self.base, "source.tar"))


if __name__ == "__main__":
    unittest.main()
