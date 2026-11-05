# SPDX-License-Identifier: GPL-3.0-or-later
"""Stricter approval rules (contract 7.1, docs/server.md sections 5 and 14.6).

Three kinds of candidate get them:

* origin.type == built: built from upstream source, code nobody at the vendor signed;
* origin.via == intake: the first candidate of a vendor application an AI found, whose tracking file an agent
  wrote (later versions `track` finds carry no `via` and follow the ordinary vendor rules);
* any candidate of an application whose distribution mode is `vendor` (the store does not hand the deb out, so
  the sandbox report is the only look anyone takes at it).

`approve` insists on a passing sandbox report, a written reason and an explicit yes for every script or flag
that needs a human look; the last kind only needs the passing sandbox report (no --no-sandbox).
"""

BUILT = "built"
INTAKE = "intake"
STORE, VENDOR = "store", "vendor"


def is_built(record):
    return (record.get("origin") or {}).get("type") == BUILT


def is_intake(record):
    """The first candidate of an AI-imported vendor application."""
    return (record.get("origin") or {}).get("via") == INTAKE


def is_strict(record):
    return is_built(record) or is_intake(record)


def distribution_mode(app):
    """'store' or 'vendor' from a loaded content App (or a mapping); a missing or unknown value is 'store'."""
    dist = app.get("distribution") if isinstance(app, dict) else getattr(app, "distribution", None)
    mode = dist.get("mode") if isinstance(dist, dict) else getattr(dist, "mode", None)
    return mode if mode in (STORE, VENDOR) else STORE


def sandbox_status(record):
    return ((record.get("checks") or {}).get("sandbox") or {}).get("status", "missing")


def sandbox_required(record, mode=STORE):
    """True when --no-sandbox is not an option for this candidate."""
    return is_strict(record) or mode == VENDOR


def built_blockers(record, no_sandbox, note, include_flagged, flagged, mode=STORE):
    """Why a candidate cannot be approved with these options (empty for ordinary candidates).

    `flagged` is cli._flagged(record): {"flags", "scripts_changed", "flags_added"}; `mode` is the distribution
    mode of the application.
    """
    out = []
    if is_strict(record):
        what = "built from source" if is_built(record) else "imported by the AI"
        if no_sandbox:
            out.append("--no-sandbox is not allowed for a candidate %s" % what)
        if sandbox_status(record) != "pass":
            out.append("a candidate %s needs a passing sandbox report (it is %s)" % (what, sandbox_status(record)))
        if not (note or "").strip():
            out.append("a candidate %s needs --note with the reason for approving it" % what)
        if any(flagged.values()) and not include_flagged:
            out.append("it has inspect flags or changed scripts: review them and pass --include-flagged")
    elif mode == VENDOR:
        if no_sandbox:
            out.append("--no-sandbox is not allowed: the vendor hosts this application and the sandbox report is "
                       "the only check of the deb")
        if sandbox_status(record) != "pass":
            out.append("an application the vendor hosts needs a passing sandbox report (it is %s)"
                       % sandbox_status(record))
    return out


def audit_fields(record):
    """Extra approvals.log fields for a strict candidate (where it came from)."""
    origin = record.get("origin") or {}
    if is_intake(record):
        return {"origin": origin.get("type"), "via": INTAKE, "intake": origin.get("intake")}
    return {"origin": BUILT, "source_commit": origin.get("source_commit"), "build_job": origin.get("build_job")}
