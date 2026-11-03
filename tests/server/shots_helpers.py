# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixtures for the screenshot directory tests: tiny real PNGs (zlib + struct, no PIL) and a CLI case."""

import json
import os
import struct
import zlib

from agino_store_server import config

from . import helpers as H
from .test_cli import CliCase


def chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def make_png(width=900, height=320, seed=1, extra=(), noise=False):
    """A real truecolour PNG; `seed` changes the pixels (so the hash), `extra` are (kind, body) chunks
    inserted before IDAT, `noise` makes the data incompressible."""
    row_len = width * 3
    if noise:
        raw = b"".join(b"\x00" + os.urandom(row_len) for _ in range(height))
    else:
        raw = (b"\x00" + bytes([seed % 256, (seed * 7) % 256, 9]) * width) * height
    head = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    middle = b"".join(chunk(k, b) for k, b in extra)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head) + middle + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def chunk_kinds(data):
    kinds, pos = [], 8
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kinds.append(data[pos + 4:pos + 8].decode("latin-1"))
        pos += 12 + length
    return kinds


class ShotsCase(CliCase):
    """CliCase with paths.screenshots, shots_staging and shots_out inside the temporary directory."""

    def setUp(self):
        super().setUp()
        base = os.path.dirname(self.conf)
        self.root = os.path.join(base, "screenshots")
        self.staging = os.path.join(base, "staging")
        self.out = os.path.join(base, "shots-out")
        os.makedirs(self.root)
        os.makedirs(self.staging)
        os.makedirs(self.out)
        self.set_conf(extra="")
        self.state_lock = os.path.join(self.cfg.state, ".lock")

    def set_conf(self, extra="", paths=None):
        with open(self.conf, encoding="utf-8") as f:
            text = f.read()
        text = text.split("\n[shots]")[0]
        if "screenshots =" not in text:
            text = text.replace("[paths]\n", "[paths]\nscreenshots = %s\nshots_staging = %s\nshots_out = %s\n" % (
                self.root, self.staging, self.out), 1)
        H.write(self.conf, text + extra, "w")
        self.scfg = config.load(self.conf)

    def run_cli(self, *argv):
        """Like CliCase.run_cli, but an argparse exit (usage error, code 2) is returned, not raised."""
        try:
            return super().run_cli(*argv)
        except SystemExit as e:
            return e.code, "", ""

    def stage(self, name, data):
        H.write(os.path.join(self.staging, name), data)

    def ingest(self, name="upload-0001.png", data=None, lang="zh_CN", scene="main", status="draft",
               source="upload", reviewer="alice", app="gimp", replace=None):
        if data is not None:
            self.stage(name, data)
        extra = ["--replace", replace] if replace is not None else []
        return self.run_cli("shots-ingest", app, "--lang", lang, "--scene", scene, "--file", name,
                            "--source", source, "--status", status, "--reviewer", reviewer, *extra)

    def review(self, file, status, lang="zh_CN", reviewer="alice", app="gimp", *extra):
        return self.run_cli("shots-review", app, "--lang", lang, "--file", file, "--status", status,
                            "--reviewer", reviewer, *extra)

    def meta(self, app="gimp"):
        with open(os.path.join(self.root, app, "meta.json"), encoding="utf-8") as f:
            return json.load(f)

    def images(self, app="gimp"):
        return self.meta(app)["images"]

    def image(self, lang, file, app="gimp"):
        return next(i for i in self.images(app) if i["lang"] == lang and i["file"] == file)

    def add_image(self, lang="zh_CN", scene="main", seed=1, status="approved", app="gimp", **png):
        """Store one image through the CLI (so the tree is exactly what the tools produce)."""
        name = "stage-%s-%d.png" % (lang.lower().replace("_", ""), seed)
        code, out, err = self.ingest(name, make_png(seed=seed, **png), lang, scene, status, app=app)
        assert code == 0, (out, err)
        return max((i for i in self.images(app) if i["lang"] == lang), key=lambda i: i["file"])
