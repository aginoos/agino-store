# SPDX-License-Identifier: GPL-3.0-or-later
"""`status --json` sections upstreams, builds and builder (docs/server.md sections 11 and 14).

Read-only like the rest of status: no lock, no network, no symlink following. Logs are never part of it.
"""

import functools
import os

from . import catalog
from .state import BUILD_ID_RE, ID_RE
from .status_io import ReadError, list_dir, pick, read_json

MAX_BUILDS = 50
MAX_VERSIONS = 30
MAX_MOVES = 10
BUILD_FIELDS = ("id", "app", "version", "upstream_version", "tag", "commit", "attempt", "status", "by", "created",
                "started", "finished", "error", "artifact", "candidate", "mode", "class", "intake")
WAITING = ("queued", "fetching", "ready")


def _version_order(versions):
    """Newest first; falls back to plain string order if a stored version is not comparable."""
    try:
        return sorted(versions, key=functools.cmp_to_key(catalog.compare_versions), reverse=True)
    except Exception:  # noqa: BLE001 - a damaged state file must not break the status document
        return sorted(versions, reverse=True)


def _memo(cfg, app, errors):
    path = os.path.join(cfg.state, "upstreams", app + ".json")
    try:
        data = read_json(path)
    except ReadError as e:
        if os.path.lexists(path):
            errors.add("upstreams", e)
        return {}
    if not isinstance(data, dict):
        errors.add("upstreams", "%s.json: not an object" % app)
        return {}
    return data


def _entry(app, up, memo):
    seen = memo.get("seen") if isinstance(memo.get("seen"), dict) else {}
    moves = [m for m in memo.get("tag_moved") or [] if isinstance(m, dict)]
    out = pick(memo, ("latest_version", "last_check", "last_ok", "last_error"))
    out.setdefault("latest_version", None)
    out.setdefault("last_check", None)
    out.setdefault("last_ok", None)
    out.setdefault("last_error", None)
    out.update(app=app, repo=up.repo if up else None, auto_task=bool(up.auto_task) if up else False,
               in_content=up is not None, tag_moved=len(moves), seen_count=len(seen),
               versions=_version_order(seen)[:MAX_VERSIONS],
               tag_moves=[pick(m, ("version", "tag", "old_commit", "new_commit", "at")) for m in moves[-MAX_MOVES:]])
    return out


def upstreams_section(cfg, content, errors):
    configured = {u.app: u for u in content.upstreams} if content is not None else {}
    ids = set(configured)
    try:
        ids.update(n[:-5] for n in list_dir(os.path.join(cfg.state, "upstreams"))
                   if n.endswith(".json") and ID_RE.match(n[:-5]))
    except ReadError:
        pass
    return [_entry(app, configured.get(app), _memo(cfg, app, errors)) for app in sorted(ids)]


def load_records(cfg, errors):
    """Every readable task record of state/builds, newest first (by created, then id)."""
    base = os.path.join(cfg.state, "builds")
    try:
        names = [n for n in list_dir(base) if n.endswith(".json") and BUILD_ID_RE.match(n[:-5])]
    except ReadError:
        return []
    out = []
    for name in names:
        try:
            record = read_json(os.path.join(base, name))
        except ReadError as e:
            errors.add("builds", e)
            continue
        if isinstance(record, dict):
            out.append(record)
        else:
            errors.add("builds", "%s: not an object" % name)
    return sorted(out, key=lambda r: (str(r.get("created") or ""), str(r.get("id"))), reverse=True)


def builds_section(records):
    return [pick(r, BUILD_FIELDS) for r in records[:MAX_BUILDS]]


def builder_section(records):
    running = next((r.get("id") for r in records if r.get("status") == "building"), None)
    return {"running": running, "queued": sum(1 for r in records if r.get("status") in WAITING)}
