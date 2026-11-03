# SPDX-License-Identifier: GPL-3.0-or-later
"""`status --json` screenshot data: the per-app summary of section apps and section shots,
both computed from <paths.screenshots>/<app>/meta.json (docs/server.md sections 11 and 12)."""

from . import shots_store as S


def counts(images):
    return {status: sum(1 for i in images if i["status"] == status) for status in S.STATUSES}


def summary(images):
    """{state: none|draft|approved, scenes, langs, counts} for the images of one app."""
    live = [i for i in images if i["status"] != "rejected"]
    total = counts(images)
    state = "approved" if total["approved"] else "draft" if total["draft"] else "none"
    return {"state": state, "scenes": len({i["scene"] for i in live}),
            "langs": [lang for lang in S.LANGS if any(i["lang"] == lang for i in live)], "counts": total}


def shots_info(cfg, app_id, errors):
    images, problems = S.read_images(cfg, app_id)
    for line in problems:
        errors.add("apps", "screenshots %s: %s" % (app_id, line))
    return summary(images)


def section(cfg, content, errors):
    """One entry per app of the content repository: id, counts and every image record."""
    out = []
    for app in content.apps if content is not None else ():
        images, problems = S.read_images(cfg, app.id)
        for line in problems:
            errors.add("shots", "%s: %s" % (app.id, line))
        out.append({"id": app.id, "counts": counts(images), "images": sorted(images, key=S.sort_key)})
    return out
