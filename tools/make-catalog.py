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
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "server"))

from agino_store_server import catalogwriter as cw  # noqa: E402


class Builder:
    def __init__(self, args):
        self.args = args
        self.out = os.path.abspath(args.out)
        self.assets = cw.AssetStore(os.path.abspath(args.assets_dir))
        self.files = {}       # published path -> bytes

    def fix_deb(self, app):
        pkg = app["package"]
        if pkg.get("kind") != "deb":
            return
        deb = pkg["deb"]
        if self.args.deb_dir:
            path = os.path.join(self.args.deb_dir, pkg["name"] + ".deb")
            if os.path.isfile(path):
                deb["sha256"] = cw.sha256_file(path)
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
            app["icon"] = self.assets.add(app["icon"])
            for shots in detail.get("screenshots", {}).values():
                for shot in shots:
                    shot["image"] = self.assets.add(shot["image"])
                    if "thumb" in shot:
                        shot["thumb"] = self.assets.add(shot["thumb"])
            detail_path = "apps/%s.json" % app["id"]
            app["detail"] = detail_path
            self.files[detail_path] = cw.dump_json(dict([("id", app["id"])] + list(detail.items())))
            if stats and "downloads" in stats:
                downloads[app["id"]] = int(stats["downloads"])
            apps_out.append(app)

        catalog = {"schema": 1, "categories": src["categories"],
                   "featured": src.get("featured", []), "apps": apps_out}
        self.files["catalog.json"] = cw.dump_json(catalog)
        if downloads:
            self.files["stats.json"] = cw.dump_json({"downloads": downloads})
        self.files.update(self.assets.files)

    def write(self):
        cw.write_tree(self.out, self.files, self.args.serial, self.args.revision,
                      self.args.valid_days, self.args.key)


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
    try:
        b.build()
        b.write()
    except cw.CatalogWriterError as e:
        sys.exit(str(e))
    print("wrote %s (%d files, serial %d)" % (b.out, len(b.files) + 1, args.serial))


if __name__ == "__main__":
    main()
