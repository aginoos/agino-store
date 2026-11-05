# SPDX-License-Identifier: GPL-3.0-or-later
"""`status --json` section intake (docs/server.md sections 11 and 15): the newest intake tasks.

Read-only like the rest of status: no lock, no network, no symlink following. No logs and no delivered files.
"""

import os

from .state import INTAKE_ID_RE
from .status_io import ReadError, list_dir, pick, read_json

MAX_INTAKE = 50
FIELDS = ("id", "kind", "app", "address", "hint", "agent", "allow_hosts", "status", "by", "created", "started",
          "finished", "error", "ai_task", "verify_task", "repair_of", "attempt", "rounds", "retries", "next_retry",
          "version", "candidate")
MAX_HOSTS = 20
MAX_CATEGORIES = 12
MAX_TEXT = 2048


def load_records(cfg, errors):
    """Every readable intake record, newest first (by created, then id)."""
    base = os.path.join(cfg.state, "intake")
    try:
        names = [n for n in list_dir(base) if n.endswith(".json") and INTAKE_ID_RE.match(n[:-5])]
    except ReadError:
        return []
    out = []
    for name in names:
        try:
            record = read_json(os.path.join(base, name))
        except ReadError as e:
            errors.add("intake", e)
            continue
        if isinstance(record, dict):
            out.append(record)
        else:
            errors.add("intake", "%s: not an object" % name)
    return sorted(out, key=lambda r: (str(r.get("created") or ""), str(r.get("id"))), reverse=True)


def _text(value, limit=MAX_TEXT):
    return value[:limit] if isinstance(value, str) else None


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _flag(value):
    return value if isinstance(value, bool) else None


def vendor_check(value):
    """The trial run of a vendor.yaml delivery (docs/server.md 15.8) reduced to known keys, types and sizes: the
    record is a state file, not trusted to be well formed. None when there is none."""
    if not isinstance(value, dict):
        return None
    flags = value.get("flags") if isinstance(value.get("flags"), dict) else {}
    categories = flags.get("categories") if isinstance(flags.get("categories"), list) else []
    hosts = value.get("hosts") if isinstance(value.get("hosts"), list) else []
    return {
        "version": _text(value.get("version"), 128), "sha256": _text(value.get("sha256"), 64),
        "size": _count(value.get("size")), "url": _text(value.get("url")), "filename": _text(value.get("filename"), 256),
        "flags": {"count": _count(flags.get("count")),
                  "categories": [_text(c, 200) for c in categories[:MAX_CATEGORIES] if isinstance(c, str)]},
        "deps_ok": _flag(value.get("deps_ok")), "matches_ai_deb": _flag(value.get("matches_ai_deb")),
        "hosts": [{"host": _text(h.get("host"), 253), "approved": h.get("approved") is True}
                  for h in hosts[:MAX_HOSTS] if isinstance(h, dict)],
    }


def _task(record):
    out = pick(record, FIELDS)
    check = vendor_check(record.get("vendor_check"))
    if check is not None:
        out["vendor_check"] = check
    return out


def intake_section(records):
    return [_task(r) for r in records[:MAX_INTAKE]]
