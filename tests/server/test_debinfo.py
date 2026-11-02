# SPDX-License-Identifier: GPL-3.0-or-later
import os
import sys
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

    def test_version_gt(self):
        self.assertTrue(debinfo.version_gt("1.10", "1.9"))
        self.assertFalse(debinfo.version_gt("1.0~rc1", "1.0"))
        self.assertFalse(debinfo.version_gt("2", "2"))
        with self.assertRaises(debinfo.DebError):
            debinfo.version_gt("--help", "1")


if __name__ == "__main__":
    unittest.main()
