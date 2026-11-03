# SPDX-License-Identifier: GPL-3.0-or-later
"""Approved screenshots in the catalog (docs/server.md sections 8 and 12).

The directory paths.screenshots wins over app.yaml `screenshots:` for every app that has at
least one approved image there. Images are published by hash like icons.
"""

import os
import struct

from . import catalogwriter as cw, shots_store as S


def collect(cfg, content):
    """({app id: [approved image records ordered by (lang, file)]}, [warnings]) from meta.json files only."""
    by_app, warnings = {}, []
    if not os.path.isdir(cfg.screenshots):
        return by_app, warnings
    for app in content.apps:
        images, problems = S.approved_images(cfg, app.id)
        warnings.extend("screenshots %s: %s" % (app.id, p) for p in problems)
        if images:
            by_app[app.id] = images
    return by_app, warnings


def inputs(by_app):
    """What the rebuild decision depends on: (app, lang, file, sha256) of every approved image, or None."""
    rows = [[app, i["lang"], i["file"], i["sha256"]] for app in sorted(by_app) for i in by_app[app]]
    return rows or None


def publish(cfg, store, app_id, images, warnings):
    """{lang: [{image, thumb, width, height}]} for the approved images that are really on disk, or None."""
    root = S.app_dir(cfg, app_id)
    out = {lang: [] for lang in S.LANGS}
    for image in images:
        rel = "%s/%s" % (image["lang"], image["file"])
        try:
            published = store.add(rel, root=root)
            if published != "assets/%s.png" % image["sha256"]:
                raise cw.CatalogWriterError("does not match its recorded sha256")
            with open(os.path.join(root, rel), "rb") as f:
                header = f.read(24)
            width, height = struct.unpack(">II", header[16:24])
        except (cw.CatalogWriterError, OSError, struct.error) as e:
            warnings.append("screenshots %s: %s left out: %s" % (app_id, rel, e))
            continue
        out[image["lang"]].append({"image": published, "thumb": published, "width": width, "height": height})
    return out if any(out.values()) else None
