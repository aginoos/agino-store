# SPDX-License-Identifier: GPL-3.0-or-later
import io
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures  # noqa: E402
from agino_store_server import debinfo  # noqa: E402

DPKG = fixtures.have("dpkg-deb", "dpkg")


class ParseTests(unittest.TestCase):
    def test_parse_stanzas_folding(self):
        text = "Package: a\nDepends: x,\n y\n\nPackage: b\nVersion: 1\n"
        stanzas = debinfo.parse_stanzas(text)
        self.assertEqual(len(stanzas), 2)
        self.assertEqual(stanzas[0]["Depends"], "x,\ny")
        self.assertEqual(stanzas[1]["Version"], "1")

    def test_parse_list_line(self):
        e = debinfo.parse_list_line("-rwsr-xr-x root/root      1234 2026-11-02 08:00 ./usr/bin/foo bar")
        self.assertEqual((e["path"], e["size"], e["type"]), ("usr/bin/foo bar", 1234, "-"))
        self.assertEqual(e["mode"][3], "s")
        link = debinfo.parse_list_line("lrwxrwxrwx root/root 0 2026-11-02 08:00 ./usr/bin/x -> /opt/x/bin")
        self.assertEqual((link["path"], link["target"]), ("usr/bin/x", "/opt/x/bin"))
        self.assertIsNone(debinfo.parse_list_line("garbage"))

    def test_installed_size_fallback(self):
        files = [{"type": "-", "size": 10}, {"type": "d", "size": 0}, {"type": "-", "size": 5}]
        self.assertEqual(debinfo.installed_size_bytes({"Installed-Size": "3"}, files), 3072)
        self.assertEqual(debinfo.installed_size_bytes({}, files), 15)


def tar_with(members):
    """members: list of (name, type, data/linkname)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, kind, payload in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind == tarfile.REGTYPE:
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            else:
                info.linkname = payload
                tar.addfile(info)
    return buf.getvalue()


class ToolTests(unittest.TestCase):
    def test_stdout_cap_is_an_error(self):
        with self.assertRaises(debinfo.DebError):
            debinfo.run_tool([sys.executable, "-I", "-c", "print('x' * 100000)"], max_stdout=1000)

    def test_output_within_cap(self):
        proc = debinfo.run_tool([sys.executable, "-I", "-c", "print('ok')"], max_stdout=1000)
        self.assertEqual((proc.returncode, proc.stdout), (0, b"ok\n"))

    def test_timeout(self):
        with self.assertRaises(debinfo.DebError):
            debinfo.run_tool([sys.executable, "-I", "-c", "import time; time.sleep(30)"], timeout=0.5)

    def test_stdin_is_passed(self):
        proc = debinfo.run_tool([sys.executable, "-I", "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
                                stdin=b"abc")
        self.assertEqual(proc.stdout, b"abc")

    def test_resource_limits_applied(self):
        code = "import resource; print(resource.getrlimit(resource.RLIMIT_FSIZE)[0])"
        proc = debinfo.run_tool([sys.executable, "-I", "-c", code])
        self.assertEqual(int(proc.stdout), debinfo.LIMIT_FSIZE)

    def test_clean_text_and_versions(self):
        self.assertEqual(debinfo.clean_text("a\x1b[31mb\nc\td"), "a[31mbc\td")
        for good in ("1.0", "2:1.0-1", "0~rc1"):
            self.assertTrue(debinfo.VERSION_RE.match(good), good)
        for bad in ("-1", "--help", "a1", ":1", "1 2", ""):
            self.assertFalse(debinfo.VERSION_RE.match(bad), bad)


class ControlMemberTests(unittest.TestCase):
    def extract(self, members):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dest = os.path.join(tmp.name, "control")
        os.mkdir(dest)
        with tarfile.open(fileobj=io.BytesIO(tar_with(members)), mode="r:") as tar:
            for member in tar:
                debinfo._extract_member(tar, member, dest)
        return dest

    def test_only_known_regular_top_level_files(self):
        dest = self.extract([
            ("./control", tarfile.REGTYPE, b"Package: x\n"), ("./postinst", tarfile.REGTYPE, b"#!/bin/sh\n"),
            ("./evil", tarfile.REGTYPE, b"x"), ("./sub/control", tarfile.REGTYPE, b"x"),
            ("../control2", tarfile.REGTYPE, b"x"), ("./prerm", tarfile.SYMTYPE, "/etc/passwd"),
            ("./config", tarfile.DIRTYPE, ""), ("./triggers", tarfile.LNKTYPE, "control"),
        ])
        self.assertEqual(sorted(os.listdir(dest)), ["control", "postinst"])

    def test_oversized_file_skipped_and_duplicate_ignored(self):
        dest = self.extract([
            ("./postrm", tarfile.REGTYPE, b"x" * (debinfo.CONTROL_FILE_MAX + 1)),
            ("./control", tarfile.REGTYPE, b"first"), ("./control", tarfile.REGTYPE, b"second"),
        ])
        self.assertEqual(os.listdir(dest), ["control"])
        with open(os.path.join(dest, "control"), "rb") as f:
            self.assertEqual(f.read(), b"first")


@unittest.skipUnless(DPKG, "dpkg-deb not available")
class DebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.deb = fixtures.build_deb(
            self.tmp.name, "demo", "1.0-1", scripts={"postinst": "#!/bin/sh\necho hi\n"},
            files={"usr/bin/demo": b"x" * 100}, modes={"usr/bin/demo": 0o4755},
            extra_control={"Installed-Size": "7"})

    def test_control_fields(self):
        fields = debinfo.control_fields(self.deb)
        self.assertEqual((fields["Package"], fields["Version"], fields["Architecture"]),
                         ("demo", "1.0-1", "amd64"))

    def test_bad_deb(self):
        bad = os.path.join(self.tmp.name, "bad.deb")
        with open(bad, "wb") as f:
            f.write(b"not a deb")
        with self.assertRaises(debinfo.DebError):
            debinfo.control_fields(bad)

    def test_file_list_and_size(self):
        files = debinfo.file_list(self.deb)
        demo = next(e for e in files if e["path"] == "usr/bin/demo")
        self.assertEqual(demo["size"], 100)
        self.assertEqual(demo["mode"][3], "s")
        self.assertEqual(debinfo.installed_size_bytes(debinfo.control_fields(self.deb), files), 7 * 1024)

    def test_extract_control_does_not_run_scripts(self):
        dest = os.path.join(self.tmp.name, "control")
        debinfo.extract_control(self.deb, dest)
        self.assertTrue(os.path.isfile(os.path.join(dest, "postinst")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "control")))

    def test_extract_control_refuses_existing_dir(self):
        dest = os.path.join(self.tmp.name, "exists")
        os.mkdir(dest)
        with self.assertRaises(FileExistsError):
            debinfo.extract_control(self.deb, dest)

    def test_version_gt(self):
        self.assertTrue(debinfo.version_gt("1.10", "1.9"))
        self.assertFalse(debinfo.version_gt("1.0~rc1", "1.0"))
        self.assertFalse(debinfo.version_gt("2", "2"))
        with self.assertRaises(debinfo.DebError):
            debinfo.version_gt("--help", "1")

    def test_version_gt_matches_dpkg(self):
        versions = ["1.0", "1.0-1", "1.0-1~bpo13+1", "1:0.9", "1.0~rc1", "1.0+dfsg-2", "1.0.1",
                    "2:1.0", "1.0-1aginoos1", "1.0a", "1.0~~", "1.0~", "0.9.99", "10.0", "1.0-1.1"]
        for a in versions:
            for b in versions:
                rc = subprocess.run(["dpkg", "--compare-versions", a, "gt", b]).returncode
                self.assertEqual(debinfo.version_gt(a, b), rc == 0, (a, b))


if __name__ == "__main__":
    unittest.main()
