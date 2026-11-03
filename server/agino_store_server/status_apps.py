# SPDX-License-Identifier: GPL-3.0-or-later
"""`status --json` section apps: the content repository joined with screenshots and approvals."""

import os

from . import content as content_mod
from .status_io import ReadError, list_dir, read_json

LANGS = ("zh_CN", "en_US")
MAX_CONTENT_ERRORS = 50
DEFAULT_SOURCE = "submitter"


def load_content(cfg, errors):
    """The Content, or None with the problems recorded (the apps section is then empty)."""
    try:
        return content_mod.load(cfg.content, allow_insecure=cfg.allow_insecure)
    except content_mod.ContentError as e:
        for line in e.errors[:MAX_CONTENT_ERRORS]:
            errors.add("apps", "content: %s" % line)
        if len(e.errors) > MAX_CONTENT_ERRORS:
            errors.add("apps", "content: %d more errors" % (len(e.errors) - MAX_CONTENT_ERRORS))
    except Exception as e:  # noqa: BLE001 - a status command must not crash on odd content
        errors.add("apps", "content: %s: %s" % (type(e).__name__, e))
    return None


def _image_statuses(meta):
    """Status strings of the images listed in meta.json (accepts {images: ...}, a dict or a list of records)."""
    items = meta.get("images", meta) if isinstance(meta, dict) else meta
    values = items.values() if isinstance(items, dict) else items if isinstance(items, list) else []
    return [v["status"] for v in values if isinstance(v, dict) and isinstance(v.get("status"), str)]


def shots_info(cfg, app_id, errors):
    """{state: none|draft|approved, scenes, langs[, statuses]} from <screenshots>/<app>/."""
    base = os.path.join(cfg.screenshots, app_id)
    stems, langs = set(), []
    for lang in LANGS:
        try:
            names = [n for n in list_dir(os.path.join(base, lang)) if n.endswith(".png")]
        except ReadError:
            continue
        if names:
            langs.append(lang)
            stems.update(n[:-4] for n in names)
    if not langs:
        return {"state": "none", "scenes": 0, "langs": []}
    out = {"state": "draft", "scenes": len(stems), "langs": langs}
    try:
        statuses = _image_statuses(read_json(os.path.join(base, "meta.json")))
    except ReadError as e:
        if os.path.lexists(os.path.join(base, "meta.json")):
            errors.add("apps", "screenshots %s: %s" % (app_id, e))
        return out
    counts = {s: statuses.count(s) for s in sorted(set(statuses))}
    out["statuses"] = counts
    if statuses and set(statuses) == {"approved"}:
        out["state"] = "approved"
    return out


def _approval(entry):
    if not isinstance(entry, dict):
        return None
    return {"version": entry.get("version"), "approved_at": entry.get("approved_at"), "by": entry.get("by")}


def app_entry(app, shots, approved, newest):
    return {
        "id": app.id, "name": dict(app.name), "summary": dict(app.summary),
        "categories": list(app.categories), "source": app.source, "kind": app.kind,
        "package": app.package, "vendor": app.vendor, "proprietary": app.proprietary,
        "developer": dict(app.developer), "homepage": app.homepage, "maintainer": app.maintainer,
        "launchable": app.launchable, "has_icon": bool(app.icon),
        "description_source": getattr(app, "description_source", None) or DEFAULT_SOURCE,
        "l10n": bool(app.l10n), "shots": shots,
        "approved": _approval(approved), "current_candidate_status": newest,
    }


def section(cfg, content, approved, newest_status, errors):
    out = []
    for app in content.apps if content is not None else ():
        try:
            out.append(app_entry(app, shots_info(cfg, app.id, errors), approved.get(app.id),
                                 newest_status.get(app.id)))
        except Exception as e:  # noqa: BLE001
            errors.add("apps", "%s: %s: %s" % (app.id, type(e).__name__, e))
    return out
