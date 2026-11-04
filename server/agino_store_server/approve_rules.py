# SPDX-License-Identifier: GPL-3.0-or-later
"""Stricter approval rules for candidates built from upstream source (contract 7.1, docs/server.md section 5).

A built candidate comes from code nobody at the vendor signed, so `approve` insists on a passing sandbox
report, a written reason and an explicit yes for every script or flag that needs a human look.
"""

BUILT = "built"


def is_built(record):
    return (record.get("origin") or {}).get("type") == BUILT


def sandbox_status(record):
    return ((record.get("checks") or {}).get("sandbox") or {}).get("status", "missing")


def built_blockers(record, no_sandbox, note, include_flagged, flagged):
    """Why a built candidate cannot be approved with these options (empty for other candidates).

    `flagged` is cli._flagged(record): {"flags", "scripts_changed", "flags_added"}.
    """
    if not is_built(record):
        return []
    out = []
    if no_sandbox:
        out.append("--no-sandbox is not allowed for a candidate built from source")
    if sandbox_status(record) != "pass":
        out.append("a candidate built from source needs a passing sandbox report (it is %s)" % sandbox_status(record))
    if not (note or "").strip():
        out.append("a candidate built from source needs --note with the reason for approving it")
    if any(flagged.values()) and not include_flagged:
        out.append("it has inspect flags or changed scripts: review them and pass --include-flagged")
    return out


def audit_fields(record):
    """Extra approvals.log fields for a built candidate (the source it was built from)."""
    origin = record.get("origin") or {}
    return {"origin": BUILT, "source_commit": origin.get("source_commit"), "build_job": origin.get("build_job")}
