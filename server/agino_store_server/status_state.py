# SPDX-License-Identifier: GPL-3.0-or-later
"""`status --json` sections read from the state directory and the preview catalog."""

import json
import os

from . import catalog
from .state import ID_RE
from .status_io import ReadError, list_dir, pick, read_json, tail_lines

LOG_LINES = 50
OFFICIAL_ORIGINS = ("apt", "https")
PERSONAL_ORIGINS = ("manual", "built")


def source_class(origin_type):
    if origin_type in OFFICIAL_ORIGINS:
        return "official"
    if origin_type in PERSONAL_ORIGINS:
        return "personal"
    return "unknown"


def _checks(record):
    checks = record.get("checks")
    checks = checks if isinstance(checks, dict) else {}
    inspect = checks.get("inspect") if isinstance(checks.get("inspect"), dict) else {}
    return {
        "deps": pick(checks.get("deps"), ("ok", "detail")),
        "inspect": {"flags": list(inspect.get("flags") or [])},
        "diff": pick(checks.get("diff"), ("against", "scripts_changed", "flags_added", "summary")),
        "sandbox": pick(checks.get("sandbox"), ("status", "summary", "report")),
    }


def trim_candidate(record, name):
    origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
    kind = origin.get("type")
    out = pick(record, ("app", "vendor", "package", "version", "size", "installed_size", "found", "status"))
    out.update(id=name, origin={"type": kind}, source_class=source_class(kind), checks=_checks(record),
               decided=record.get("decided"))
    if kind == "apt":
        out["origin"]["signed_by"] = origin.get("signed_by")
    return out


def load_candidates(cfg, errors):
    """Every readable candidate record, trimmed, newest first; bad files are recorded and skipped."""
    base = os.path.join(cfg.state, "candidates")
    out = []
    try:
        apps = list_dir(base)
    except ReadError:
        return out
    for app in apps:
        if not ID_RE.match(app):
            continue
        try:
            names = [n for n in list_dir(os.path.join(base, app)) if n.endswith(".json")]
        except ReadError:
            continue   # not a directory, or gone meanwhile
        for name in names:
            try:
                record = read_json(os.path.join(base, app, name))
            except ReadError as e:
                errors.add("candidates", "%s/%s" % (app, e))
                continue
            if not isinstance(record, dict):
                errors.add("candidates", "%s/%s: not an object" % (app, name))
                continue
            out.append(trim_candidate(record, "%s/%s" % (app, name[:-5])))
    return sorted(out, key=lambda r: (str(r.get("found") or ""), r["id"]), reverse=True)


def newest_status(candidates):
    """{app: status of its newest candidate by version}."""
    best = {}
    for r in candidates:
        version = r.get("version")
        if not isinstance(version, str) or not r.get("app"):
            continue
        if r["app"] not in best or catalog.compare_versions(version, best[r["app"]][0]) > 0:
            best[r["app"]] = (version, r.get("status"))
    return {app: status for app, (_, status) in best.items()}


def read_approved(cfg, errors):
    try:
        data = read_json(os.path.join(cfg.state, "approved.json"))
    except ReadError as e:
        if os.path.lexists(os.path.join(cfg.state, "approved.json")):
            errors.add("approved", e)
        return {}
    if not isinstance(data, dict):
        errors.add("approved", "approved.json: not an object")
        return {}
    return data


def approved_section(cfg, approved, errors):
    log = []
    try:
        lines = tail_lines(os.path.join(cfg.state, "approvals.log"), LOG_LINES)
    except ReadError as e:
        if os.path.lexists(os.path.join(cfg.state, "approvals.log")):
            errors.add("approved", e)
        lines = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            log.append(entry)
    return {"apps": approved, "log": log}


def vendors_section(cfg, content, errors):
    config_by_id = {v.id: v for v in content.vendors} if content is not None else {}
    ids = set(config_by_id)
    try:
        ids.update(n[:-5] for n in list_dir(os.path.join(cfg.state, "vendors"))
                   if n.endswith(".json") and ID_RE.match(n[:-5]))
    except ReadError:
        pass
    out = []
    for vid in sorted(ids):
        try:
            memo = read_json(os.path.join(cfg.state, "vendors", vid + ".json"))
        except ReadError as e:
            memo = {}
            if os.path.lexists(os.path.join(cfg.state, "vendors", vid + ".json")):
                errors.add("vendors", e)
        entry = pick(memo, ("last_check", "last_ok", "last_error", "seen_version"))
        vendor = config_by_id.get(vid)
        entry.update(id=vid, name=vendor.package if vendor else None, in_content=vendor is not None)
        out.append(entry)
    return out


def _published(cfg, errors):
    base = os.path.join(cfg.preview, "current")
    try:
        manifest = read_json(os.path.join(base, "manifest.json"))
    except ReadError:
        if os.path.lexists(os.path.join(base, "manifest.json")):
            errors.add("catalog", "preview manifest.json is unreadable")
        return None
    if not isinstance(manifest, dict):
        errors.add("catalog", "preview manifest.json: not an object")
        return None
    out = pick(manifest, ("serial", "revision", "created", "expires"))
    out["valid_until"] = manifest.get("expires")
    try:
        apps = read_json(os.path.join(base, "catalog.json")).get("apps")
        out["apps"] = len(apps) if isinstance(apps, list) else None
    except (ReadError, AttributeError):
        out["apps"] = None
    sig = os.path.join(base, "manifest.json.sig")
    out["signature_present"] = os.path.isfile(sig) and not os.path.islink(sig)
    return out


def catalog_section(cfg, errors):
    try:
        state = read_json(os.path.join(cfg.state, "catalog.json"))
    except ReadError as e:
        state = {}
        if os.path.lexists(os.path.join(cfg.state, "catalog.json")):
            errors.add("catalog", e)
    return {"state": pick(state, ("serial", "revision", "built_at", "inputs_sha256", "apps")),
            "published": _published(cfg, errors)}
