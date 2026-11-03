# SPDX-License-Identifier: GPL-3.0-or-later
"""`status --json` section apps: the content repository joined with screenshots and approvals."""

from . import content as content_mod
from .status_shots import shots_info

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


def _approval(entry):
    if not isinstance(entry, dict):
        return None
    return {"version": entry.get("version"), "approved_at": entry.get("approved_at"), "by": entry.get("by")}


def app_entry(app, shots, approved, newest):
    return {
        "id": app.id, "name": dict(app.name), "summary": dict(app.summary),
        "description": {lang: list(paragraphs) for lang, paragraphs in app.description.items()},
        "license": app.license,
        "categories": list(app.categories), "source": app.source, "kind": app.kind,
        "package": app.package, "vendor": app.vendor, "proprietary": app.proprietary,
        "developer": dict(app.developer), "homepage": app.homepage, "maintainer": app.maintainer,
        "launchable": app.launchable, "has_icon": bool(app.icon),
        "description_source": app.description_source or DEFAULT_SOURCE,
        "description_confirmed": dict(app.description_confirmed) if app.description_confirmed else None,
        "l10n": bool(app.l10n), "shots": shots,
        "approved": _approval(approved), "current_candidate_status": newest,
    }


def categories_section(content):
    return [{"id": c.id, "name": dict(c.name)} for c in content.categories] if content is not None else []


def section(cfg, content, approved, newest_status, errors):
    out = []
    for app in content.apps if content is not None else ():
        try:
            out.append(app_entry(app, shots_info(cfg, app.id, errors), approved.get(app.id),
                                 newest_status.get(app.id)))
        except Exception as e:  # noqa: BLE001
            errors.add("apps", "%s: %s: %s" % (app.id, type(e).__name__, e))
    return out
