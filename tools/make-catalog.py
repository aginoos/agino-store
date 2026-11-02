#!/usr/bin/python3 -I
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build a signed agino-store catalog tree (docs/design.md section 4) from a source file.

Source JSON: {"categories": [...], "featured": [...], "apps": [...]}. Each app holds the
catalog.json summary fields plus:
  "detail": object written to apps/<id>.json (the "id" key is added here)
  "stats":  optional {"downloads": N}
Asset references (app "icon", detail screenshots "image"/"thumb") are file names relative to
--assets-dir; they are copied to assets/<sha256>.<ext> and rewritten.
The input is only parsed as data; nothing in it is executed.
"""

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ASSET_EXTS = {".svg", ".png", ".webp", ".jpg", ".jpeg"}
PLACEHOLDER_SHA = "0" * 64
KNOWN_OUTPUTS = ("catalog.json", "apps", "stats.json", "assets", "manifest.json", "manifest.json.sig")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def dump_json(obj):
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class Builder:
    def __init__(self, args):
        self.args = args
        self.out = os.path.abspath(args.out)
        self.assets_dir = os.path.abspath(args.assets_dir)
        self.asset_map = {}   # source name -> published path
        self.files = {}       # published path -> bytes or ("file", src)

    def asset(self, name):
        if name in self.asset_map:
            return self.asset_map[name]
        src = os.path.realpath(os.path.join(self.assets_dir, name))
        root = os.path.realpath(self.assets_dir)
        if os.path.commonpath([src, root]) != root or not os.path.isfile(src):
            sys.exit("asset not found or outside assets dir: %s" % name)
        ext = os.path.splitext(src)[1].lower()
        if ext not in ASSET_EXTS:
            sys.exit("unsupported asset type: %s" % name)
        published = "assets/%s%s" % (sha256_file(src), ext)
        self.files[published] = ("file", src)
        self.asset_map[name] = published
        return published

    def fix_deb(self, app):
        pkg = app["package"]
        if pkg.get("kind") != "deb":
            return
        deb = pkg["deb"]
        if self.args.deb_dir:
            path = os.path.join(self.args.deb_dir, pkg["name"] + ".deb")
            if os.path.isfile(path):
                deb["sha256"] = sha256_file(path)
                deb["size"] = os.path.getsize(path)
                return
        print("warning: no deb file for %s, keeping placeholder hash" % pkg["name"], file=sys.stderr)

    def build(self):
        with open(self.args.source, "r", encoding="utf-8") as f:
            src = json.load(f)
        apps_out = []
        downloads = {}
        for app in src["apps"]:
            app = json.loads(json.dumps(app))   # deep copy
            if not re.fullmatch(r"[a-z0-9][a-z0-9._+-]*", app["id"]):
                sys.exit("bad app id: %s" % app["id"])
            detail = app.pop("detail")
            stats = app.pop("stats", None)
            if app["source"] == "personal" and not detail.get("maintainer"):
                sys.exit("personal app %s needs a maintainer" % app["id"])
            self.fix_deb(app)
            app["icon"] = self.asset(app["icon"])
            for shots in detail.get("screenshots", {}).values():
                for shot in shots:
                    shot["image"] = self.asset(shot["image"])
                    if "thumb" in shot:
                        shot["thumb"] = self.asset(shot["thumb"])
            detail_path = "apps/%s.json" % app["id"]
            app["detail"] = detail_path
            self.files[detail_path] = dump_json(dict([("id", app["id"])] + list(detail.items())))
            if stats and "downloads" in stats:
                downloads[app["id"]] = int(stats["downloads"])
            apps_out.append(app)

        catalog = {"schema": 1, "categories": src["categories"],
                   "featured": src.get("featured", []), "apps": apps_out}
        self.files["catalog.json"] = dump_json(catalog)
        if downloads:
            self.files["stats.json"] = dump_json({"downloads": downloads})

    def write(self):
        os.makedirs(self.out, exist_ok=True)
        for name in KNOWN_OUTPUTS:
            p = os.path.join(self.out, name)
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            elif os.path.lexists(p):
                os.remove(p)

        entries = []
        for path in sorted(self.files):
            data = self.files[path]
            dest = os.path.join(self.out, path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if isinstance(data, tuple):
                shutil.copyfile(data[1], dest)
            else:
                with open(dest, "wb") as f:
                    f.write(data)
            entries.append({"path": path, "sha256": sha256_file(dest), "size": os.path.getsize(dest)})

        epoch = os.environ.get("SOURCE_DATE_EPOCH")
        now = (datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc) if epoch
               else datetime.datetime.now(datetime.timezone.utc)).replace(microsecond=0)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        manifest = {
            "schema": 1,
            "serial": self.args.serial,
            "revision": self.args.revision,
            "created": now.strftime(fmt),
            "expires": (now + datetime.timedelta(days=self.args.valid_days)).strftime(fmt),
            "files": entries,
        }
        manifest_path = os.path.join(self.out, "manifest.json")
        with open(manifest_path, "wb") as f:
            f.write(dump_json(manifest))
        self.sign(manifest_path)

    def sign(self, manifest_path):
        with tempfile.TemporaryDirectory() as tmp:
            sig_path = os.path.join(tmp, "sig.bin")
            subprocess.run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", self.args.key,
                            "-in", manifest_path, "-out", sig_path], check=True)
            with open(sig_path, "rb") as f:
                sig = f.read()
        if len(sig) != 64:
            sys.exit("unexpected signature length %d" % len(sig))
        with open(manifest_path + ".sig", "w") as f:
            f.write(base64.b64encode(sig).decode("ascii") + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True, help="catalog source JSON")
    ap.add_argument("--assets-dir", required=True, help="directory with icons and screenshots")
    ap.add_argument("--key", required=True, help="Ed25519 private key (PEM)")
    ap.add_argument("--out", required=True, help="output catalog directory")
    ap.add_argument("--serial", type=int, required=True)
    ap.add_argument("--revision", required=True)
    ap.add_argument("--valid-days", type=int, default=14)
    ap.add_argument("--deb-dir", help="directory with <package>.deb files to hash")
    args = ap.parse_args()
    if args.serial <= 0 or args.valid_days <= 0:
        sys.exit("serial and valid-days must be positive")

    b = Builder(args)
    b.build()
    b.write()
    print("wrote %s (%d files, serial %d)" % (b.out, len(b.files) + 1, args.serial))


if __name__ == "__main__":
    main()
