# SPDX-License-Identifier: GPL-3.0-or-later
"""Prometheus lines for upstream tracking and build tasks (the `metrics` command; docs/server.md section 14)."""

import os

from .state import ID_RE

STATUSES = ("queued", "fetching", "ready", "building", "collecting", "done", "failed", "cancelled")


def lines(cfg, st, epoch):
    """[(name, label or None, value)]: per upstream last_ok / last_error / tag_moved, and tasks per status.

    `epoch` turns a UTC stamp into seconds (None for junk, which drops the line).
    """
    out = []
    directory = os.path.join(cfg.state, "upstreams")
    names = os.listdir(directory) if os.path.isdir(directory) else []
    for app in sorted(n[:-5] for n in names if n.endswith(".json") and ID_RE.match(n[:-5])):
        memo = st.upstream_memo(app)
        label = '{app="%s"}' % app
        out.append(("agino_store_upstream_last_ok_timestamp_seconds", label, epoch(memo.get("last_ok"))))
        out.append(("agino_store_upstream_last_error", label, 1 if memo.get("last_error") else 0))
        out.append(("agino_store_upstream_tag_moved", label, len(memo.get("tag_moved") or [])))
    records = st.builds()
    if records:
        for status in STATUSES:
            out.append(("agino_store_builds", '{status="%s"}' % status,
                        sum(1 for r in records if r.get("status") == status)))
    return out
