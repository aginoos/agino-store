# SPDX-License-Identifier: GPL-3.0-or-later
"""server/agino-store-sign and [catalog] sign_command."""

import os
import subprocess
import sys
import tempfile
import unittest

from agino_store_server import catalogwriter as cw, config

from . import helpers as H
from .test_catalog import CatalogCase

HELPER = os.path.join(H.REPO, "server", "agino-store-sign")
MANIFEST = b'{"schema": 1, "serial": 2026110201, "expires": "2026-11-16T12:00:00Z"}\n'


class HelperCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = self._tmp.name
        self.keys = os.path.join(self.base, "keys")
        os.makedirs(self.keys)
        key, self.pub = H.make_key(self.base)
        self.key = os.path.join(self.keys, "preview.pem")
        os.rename(key, self.key)
        self.env = dict(os.environ, AGINO_STORE_SERVER_TESTS="1", AGINO_STORE_SIGN_KEYS=self.keys)

    def run_helper(self, name, data, env=None):
        return subprocess.run([sys.executable, HELPER, name], input=data, capture_output=True,
                              env=env or self.env)

    def test_signature_verifies(self):
        done = self.run_helper("preview", MANIFEST)
        self.assertEqual(done.returncode, 0, done.stderr)
        d = os.path.join(self.base, "tree")
        H.write(os.path.join(d, "manifest.json"), MANIFEST)
        H.write(os.path.join(d, "manifest.json.sig"), done.stdout)
        self.assertTrue(H.verify_signature(d, self.pub))
        self.assertIn(b"serial=2026110201", done.stderr)
        self.assertIn(b"sha256=", done.stderr)

    def test_bad_names_refused(self):
        for name in ("../preview", "Preview", "1abc", "a" * 33, "pre_view", ""):
            self.assertEqual(self.run_helper(name, MANIFEST).returncode, 2, name)

    def test_oversize_input_refused(self):
        big = MANIFEST[:-2] + b' ' * (8 << 20) + b'}\n'
        self.assertEqual(self.run_helper("preview", big).returncode, 2)

    def test_non_manifest_refused(self):
        for data in (b"not json", b"[]", b'{"schema": 2, "serial": 1, "expires": "x"}',
                     b'{"schema": 1, "expires": "x"}', b'{"schema": 1, "serial": 1}'):
            self.assertEqual(self.run_helper("preview", data).returncode, 2, data)

    def test_loose_key_mode_refused(self):
        os.chmod(self.key, 0o644)
        done = self.run_helper("preview", MANIFEST)
        self.assertEqual(done.returncode, 2)
        self.assertEqual(done.stdout, b"")

    def test_missing_key_message_has_no_path(self):
        done = self.run_helper("other", MANIFEST)
        self.assertEqual(done.returncode, 2)
        self.assertNotIn(self.keys.encode(), done.stderr)

    def test_key_dir_override_needs_test_flag(self):
        env = {k: v for k, v in self.env.items() if k != "AGINO_STORE_SERVER_TESTS"}
        done = self.run_helper("preview", MANIFEST, env)
        self.assertEqual(done.returncode, 2)


class TestConfig(unittest.TestCase):
    def load(self, key, command):
        with tempfile.TemporaryDirectory() as base:
            cfg = H.make_config(base, key)
            conf = os.path.join(base, "server.conf")
            H.write_conf_file(conf, cfg)
            with open(conf, encoding="utf-8") as f:
                text = f.read()
            text = text.replace("sign_command = \n", "sign_command = %s\n" % command)
            H.write(conf, text, "w")
            return config.load(conf)

    def test_key_only(self):
        self.assertEqual(self.load("/k.pem", "").sign_command, ())

    def test_sign_command_only(self):
        cfg = self.load("", "sudo -n -u x /bin/sign 'a b'")
        self.assertEqual(cfg.sign_command, ("sudo", "-n", "-u", "x", "/bin/sign", "a b"))
        self.assertEqual(cfg.key, "")

    def test_both_or_neither_rejected(self):
        for key, command in (("/k.pem", "/bin/sign x"), ("", "")):
            with self.assertRaises(config.ConfigError):
                self.load(key, command)


class TestCatalogViaHelper(CatalogCase):
    def setUp(self):
        super().setUp()
        keys = os.path.join(self.base, "keys")
        os.makedirs(keys)
        os.rename(self.key, os.path.join(keys, "test.pem"))
        os.environ["AGINO_STORE_SERVER_TESTS"] = "1"
        os.environ["AGINO_STORE_SIGN_KEYS"] = keys
        self.cfg = H.make_config(self.base, "", keep_catalogs=self.keep,
                                 sign_command=(sys.executable, HELPER, "test"))

    def test_build_signs_through_helper(self):
        files = {"catalog.json": b"{}"}
        out = os.path.join(self.base, "out")
        cw.write_tree(out, files, 2026110201, "r1", 14, "", sign_command=self.cfg.sign_command)
        self.assertTrue(H.verify_signature(out, self.pub))

    def test_catalog_build_uses_helper(self):
        self.build()
        self.assertTrue(H.verify_signature(os.path.join(self.cfg.preview, "current"), self.pub))

    def test_helper_failure_is_signing_failed(self):
        out = os.path.join(self.base, "out")
        with self.assertRaises(cw.CatalogWriterError) as ctx:
            cw.write_tree(out, {}, 1, "r1", 14, "", sign_command=(sys.executable, HELPER, "missing"))
        self.assertEqual(str(ctx.exception), "signing failed: agino-store-sign: key file is not readable")

    def test_garbage_output_is_signing_failed(self):
        out = os.path.join(self.base, "out")
        with self.assertRaises(cw.CatalogWriterError):
            cw.write_tree(out, {}, 1, "r1", 14, "", sign_command=("echo", "!!notbase64"))


if __name__ == "__main__":
    unittest.main()
