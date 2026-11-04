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


def intake_section(records):
    return [pick(r, FIELDS) for r in records[:MAX_INTAKE]]
