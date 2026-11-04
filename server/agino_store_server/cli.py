# SPDX-License-Identifier: GPL-3.0-or-later
"""agino-store-server command line (docs/server.md section 5).

Exit codes: 0 success; 1 something needs a human (failed check, tracking error, invalid
content); 2 usage or configuration error; 3 another task holds the state lock.
"""

import argparse
import configparser
import contextlib
import datetime
import functools
import getpass
import json
import os
import re
import sys
import tempfile
import time

from . import (approve_rules, build_step, builds, catalog, catalogwriter, config, content, content_edit, reports,
               sandboxqueue, shots_cmds, shots_import, shots_session, shots_store, shotsreq, status, upstreams)
from .state import ID_RE, VERSION_RE, State, StateError, StateLocked

EXIT_OK, EXIT_PROBLEM, EXIT_USAGE, EXIT_LOCKED = 0, 1, 2, 3
OPEN_STATUSES = ("pending", "awaiting-upload")
METRICS_FILE = "agino_store_server.prom"
DEFAULT_REPORT_KEYS = "/etc/agino-store-server/sandbox-keys"
TESTS_ENV = "AGINO_STORE_SERVER_TESTS"
CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class UsageError(Exception):
    pass


def _parser():
    ap = argparse.ArgumentParser(prog="agino-store-server")
    ap.add_argument("--config", default=config.DEFAULT_PATH, help="path of server.conf")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("track", help="track vendors").add_argument("--vendor")
    sub.add_parser("check", help="refresh milkyway and re-check dependencies").add_argument("--app")
    p = sub.add_parser("intake", help="take in a manually downloaded deb")
    p.add_argument("vendor")
    p.add_argument("file")
    p.add_argument("--url", default=None, help="page the file came from")
    sub.add_parser("pending", help="list candidates waiting for a decision").add_argument(
        "--json", action="store_true")
    p = sub.add_parser("approve", help="approve APP[@VERSION] or all")
    p.add_argument("target")
    p.add_argument("--no-sandbox", action="store_true", help="approve without a passing sandbox report")
    p.add_argument("--include-flagged", action="store_true",
                   help="with 'all': also approve candidates with inspect flags or changed scripts")
    p.add_argument("--description-reviewed", action="store_true",
                   help="approve although the description is AI-generated and not confirmed (not with 'all')")
    p.add_argument("--note", default="")
    p = sub.add_parser("revoke", help="withdraw the approval of APP; the next build drops it")
    p.add_argument("target")
    p.add_argument("--note", default="")
    p = sub.add_parser("reject", help="reject APP@VERSION")
    p.add_argument("target")
    p.add_argument("--note", default="")
    sub.add_parser("reports-ingest", help="import sandbox reports")
    sub.add_parser("sandbox-queue", help="hand pending candidates without a sandbox report to the sandbox host")
    p = sub.add_parser("shots-request", help="have the sandbox host take screenshots of APP")
    p.add_argument("app")
    p.add_argument("--cancel", action="store_true", help="withdraw the request")
    p.add_argument("--for-session", action="store_true",
                   help="keep the job in the queue for 2 hours for an interactive session")
    p = sub.add_parser("shots-import", help="take the newest sandbox screenshots of APP in as drafts")
    p.add_argument("app")
    p = sub.add_parser("shots-session-import", help="take picked captures of an interactive session in as drafts")
    p.add_argument("app")
    p.add_argument("--session", required=True, metavar="ID", help="16 hex digits")
    p.add_argument("--reviewer", required=True)
    p.add_argument("--pick", required=True, metavar="NN[,NN...]", help="two-digit capture numbers")
    p = sub.add_parser("shots-ingest", help="store a staged PNG as a screenshot of APP")
    p.add_argument("app")
    p.add_argument("--lang", required=True)
    p.add_argument("--scene", required=True)
    p.add_argument("--file", required=True, metavar="STAGED_NAME", help="PNG in paths.shots_staging")
    p.add_argument("--source", required=True)
    p.add_argument("--status", required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--replace", default=None, metavar="NN-SCENE.PNG", help="image whose slot the new one takes")
    p = sub.add_parser("shots-review", help="approve, reject or reset one screenshot")
    p.add_argument("app")
    p.add_argument("--lang", required=True)
    p.add_argument("--file", required=True, metavar="NN-SCENE.PNG")
    p.add_argument("--status", required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--note", default=None)
    p = sub.add_parser("shots-remove", help="move one screenshot to .prev")
    p.add_argument("app")
    p.add_argument("--lang", required=True)
    p.add_argument("--file", required=True, metavar="NN-SCENE.PNG")
    p.add_argument("--reviewer", required=True)
    p = sub.add_parser("content-edit", help="apply a staged JSON patch to the information of APP")
    p.add_argument("app")
    p.add_argument("--patch", required=True, metavar="STAGED_NAME", help="JSON file in paths.shots_staging")
    p.add_argument("--editor", required=True)
    p = sub.add_parser("content-confirm", help="mark the description of APP as confirmed by a human")
    p.add_argument("app")
    p.add_argument("--editor", required=True)
    sub.add_parser("upstream-check", help="check the tracked upstreams for new versions").add_argument("--app")
    p = sub.add_parser("build-request", help="queue a build of an upstream version")
    p.add_argument("app")
    p.add_argument("--version", required=True, help="upstream version seen by upstream-check")
    p.add_argument("--by", required=True, metavar="NAME")
    p = sub.add_parser("build-cancel", help="cancel an unfinished build task")
    p.add_argument("id")
    p.add_argument("--by", required=True, metavar="NAME")
    sub.add_parser("build-step", help="advance the build tasks (timer, every minute)")
    sub.add_parser("build", help="build and publish the catalog").add_argument("--if-changed", action="store_true")
    sub.add_parser("metrics", help="write the Prometheus text file")
    p = sub.add_parser("status", help="read-only state overview for the admin console (needs --json)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--section", action="append", default=None, metavar="NAME",
                   help="only this section (repeatable); default all")
    return ap


def _actor():
    return os.environ.get("SUDO_USER") or getpass.getuser()


def _stamp():
    return catalogwriter.now().strftime(catalogwriter.TIME_FORMAT)


def _load_content(cfg):
    return content.load(cfg.content, allow_insecure=cfg.allow_insecure, policy=cfg.build_policy())


def _clean(value):
    """Text for the terminal: control characters other than tab and newline are replaced."""
    return CONTROL_RE.sub("?", str(value))


def _describe(item):
    if isinstance(item, dict) and item.get("message"):
        who = item.get("vendor") or item.get("app") or "-"
        return _clean("%s %s: %s" % (item.get("level", "info"), who, item["message"]))
    return _clean(json.dumps(item, sort_keys=True, ensure_ascii=False, default=str))


def _is_problem(item):
    # trackers/checks results: {level: info|alert|error, vendor?, app, message}
    return isinstance(item, dict) and item.get("level") in ("alert", "error")


def _split_target(text, version_required=False):
    app, sep, version = text.partition("@")
    if not ID_RE.match(app):
        raise UsageError("bad app id: %r" % app)
    if sep and not VERSION_RE.match(version):
        raise UsageError("bad version: %r" % version)
    if version_required and not sep:
        raise UsageError("expected APP@VERSION")
    return app, (version if sep else None)


# track, check, intake (implemented in other modules, imported lazily)

def cmd_track(cfg, st, args):
    from . import trackers
    with st.lock():
        results = trackers.run_track(cfg, st, content.tracking_view(_load_content(cfg)), vendor=args.vendor)
    for item in results:
        print(_describe(item))
    return EXIT_PROBLEM if any(_is_problem(i) for i in results) else EXIT_OK


def cmd_check(cfg, st, args):
    from . import checks
    with st.lock():
        problems = checks.run_check(cfg, st, content.tracking_view(_load_content(cfg)), app=args.app)
    for item in problems:
        print(_describe(item))
    return EXIT_PROBLEM if any(_is_problem(i) for i in problems) else EXIT_OK


def cmd_intake(cfg, st, args):
    from . import trackers
    with st.lock():
        record = trackers.intake(cfg, st, content.tracking_view(_load_content(cfg)), args.vendor, args.file, args.url)
    print("taken in %s %s (%s)" % (record.get("app"), record.get("version"), record.get("status")))
    return EXIT_OK


# upstream tracking and build tasks

def cmd_upstream_check(cfg, st, args):
    if args.app is not None and not ID_RE.match(args.app):
        raise UsageError("bad app id: %r" % args.app)
    with st.lock():
        try:
            lines, problem = upstreams.check_all(cfg, st, _load_content(cfg), app=args.app)
        except upstreams.UnknownUpstream as e:
            raise UsageError(str(e)) from e
    for line in lines:
        print(_clean(line))
    return EXIT_PROBLEM if problem else EXIT_OK


def cmd_build_request(cfg, st, args):
    if not ID_RE.match(args.app):
        raise UsageError("bad app id: %r" % args.app)
    with st.lock():
        record = builds.request(cfg, st, _load_content(cfg), args.app, args.version, args.by)
    print(record["id"])
    return EXIT_OK


def cmd_build_cancel(cfg, st, args):
    with st.lock():
        record = builds.cancel(cfg, st, args.id, args.by)
    print("cancelled %s" % record["id"])
    return EXIT_OK


def cmd_build_step(cfg, st, args):
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(st.lock("build-step"))
        except StateLocked:
            print("build-step is already running")
            return EXIT_OK
        report = build_step.step(cfg, st, time.time())
    for line in report.lines:
        print(_clean(line))
    return EXIT_PROBLEM if report.failed else EXIT_OK


# pending

def _pending_records(st):
    records = [r for r in st.candidates() if r.get("status") in OPEN_STATUSES]
    return sorted(records, key=lambda r: (r.get("app", ""), r.get("found", "")))


def _public(record):
    return {k: v for k, v in record.items() if k != "_path"}


def _line(record):
    checks = record.get("checks") or {}
    deps = (checks.get("deps") or {}).get("ok")
    sandbox = (checks.get("sandbox") or {}).get("status", "missing")
    flags = len((checks.get("inspect") or {}).get("flags") or [])
    diff = (checks.get("diff") or {}).get("summary") or ""
    size = "%.1f MiB" % (record.get("size", 0) / (1 << 20)) if record.get("size") else "-"
    return _clean("%-20s %-28s %-15s %10s  deps:%s sandbox:%s flags:%d  %s" % (
        record.get("app"), record.get("version"), record.get("status"), size,
        {True: "ok", False: "FAIL"}.get(deps, "?"), sandbox, flags, diff))


def cmd_pending(cfg, st, args):
    records = _pending_records(st)
    if args.json:
        print(_clean(json.dumps([_public(r) for r in records], indent=1, sort_keys=True, ensure_ascii=False)))
    else:
        for record in records:
            print(_line(record))
    return EXIT_OK


# approve and reject

def _blockers(record, no_sandbox):
    checks = record.get("checks") or {}
    built = approve_rules.is_built(record)
    out = []
    if record.get("status") != "pending":
        out.append("status is %s" % record.get("status"))
    if (checks.get("deps") or {}).get("ok") is not True:
        out.append("dependency check has not passed")
    if not (no_sandbox and not built) and (checks.get("sandbox") or {}).get("status") != "pass":
        hint = "" if built else " (use --no-sandbox to override)"
        out.append("sandbox is %s%s" % ((checks.get("sandbox") or {}).get("status", "missing"), hint))
    return out


def _flagged(record):
    """What needs a human look before approving: inspect flags, changed scripts, newly added flags."""
    checks = record.get("checks") or {}
    diff = checks.get("diff") or {}
    return {"flags": list((checks.get("inspect") or {}).get("flags") or []),
            "scripts_changed": list(diff.get("scripts_changed") or []),
            "flags_added": list(diff.get("flags_added") or [])}


def _is_flagged(record):
    return any(_flagged(record).values())


def _print_flagged(record):
    found = _flagged(record)
    for key, label in (("flags", "inspect flag"), ("flags_added", "new flag"), ("scripts_changed", "changed script")):
        for item in found[key]:
            print(_clean("  %s: %s" % (label, item)))


def _highest(records):
    return functools.reduce(lambda a, b: b if catalog.compare_versions(b["version"], a["version"]) > 0 else a, records)


def _select(st, app, version):
    records = [r for r in st.candidates(app=app) if r.get("status") == "pending"
               and (version is None or r.get("version") == version)]
    if not records:
        raise UsageError("no pending candidate for %s" % (app if version is None else "%s@%s" % (app, version)))
    return _highest(records)


def _check_not_downgrade(st, record):
    current = st.approved().get(record["app"])
    if current and catalog.compare_versions(record["version"], current["version"]) <= 0:
        raise UsageError("%s %s is not newer than the approved %s" % (record["app"], record["version"], current["version"]))


AI_DESCRIPTION_BLOCKER = ("the description is AI-generated and not confirmed; "
                          "confirm it or pass --description-reviewed")


def _ai_description(loaded, app):
    """True when the app's description (in the loaded content) is AI-generated and not yet confirmed."""
    entry = loaded.app(app)
    return entry is not None and entry.description_source == "ai"


def approve_record(st, record, no_sandbox, note, description_reviewed=False):
    """Approve one pending candidate and supersede older pending ones of the same app."""
    _check_not_downgrade(st, record)
    who, when = _actor(), _stamp()
    sandbox = ((record.get("checks") or {}).get("sandbox") or {}).get("status", "missing")
    name = os.path.basename(record["_path"])[: -len(".json")]
    entry = {"version": record["version"], "sha256": record["sha256"], "candidate": name,
             "approved_at": when, "by": who, "note": note}
    log = {"at": when, "action": "approve", "app": record["app"], "version": record["version"],
           "sha256": record["sha256"], "by": who, "note": note, "sandbox": sandbox, "no_sandbox": no_sandbox,
           "description_reviewed": description_reviewed}
    if approve_rules.is_built(record):
        log.update(approve_rules.audit_fields(record))
    st.set_approved(record["app"], entry, log)
    st.update_candidate(record, {"status": "approved", "decided": {"action": "approve", "at": when, "by": who,
                                                                    "note": note, "no_sandbox": no_sandbox}})
    for other in st.candidates(app=record["app"], status="pending"):
        if other["_path"] != record["_path"] and catalog.compare_versions(other["version"], record["version"]) < 0:
            st.update_candidate(other, {"status": "superseded", "decided": {
                "action": "superseded", "at": when, "by": who, "note": "superseded by %s" % record["version"]}})


def _built_blockers(record, args):
    return approve_rules.built_blockers(record, args.no_sandbox, args.note, args.include_flagged, _flagged(record))


def _approve_all(cfg, st, args):
    if args.description_reviewed:
        raise UsageError("--description-reviewed cannot be used with 'all'; approve the app by name")
    apps = sorted({r["app"] for r in st.candidates(status="pending")})
    loaded = _load_content(cfg)
    skipped = 0
    for app in apps:
        record = _select(st, app, None)
        blockers = _blockers(record, args.no_sandbox) + _built_blockers(record, args)
        if blockers:
            print("skipped %s %s: %s" % (app, record["version"], "; ".join(blockers)))
            skipped += 1
            continue
        if _ai_description(loaded, app):
            print("skipped %s %s: %s (approve it by name)" % (app, record["version"], AI_DESCRIPTION_BLOCKER))
            skipped += 1
            continue
        if _is_flagged(record) and not args.include_flagged:
            print("skipped %s %s: has inspect flags or changed scripts (approve it by name, or use --include-flagged)"
                  % (app, record["version"]))
            _print_flagged(record)
            skipped += 1
            continue
        try:
            approve_record(st, record, args.no_sandbox, args.note)
        except UsageError as e:
            print("skipped %s %s: %s" % (app, record["version"], e))
            skipped += 1
            continue
        print("approved %s %s" % (app, record["version"]))
    return EXIT_PROBLEM if skipped else EXIT_OK


def cmd_approve(cfg, st, args):
    with st.lock():
        if args.target == "all":
            return _approve_all(cfg, st, args)
        app, version = _split_target(args.target)
        record = _select(st, app, version)
        blockers = _blockers(record, args.no_sandbox) + _built_blockers(record, args)
        if not args.description_reviewed and _ai_description(_load_content(cfg), app):
            blockers.append(AI_DESCRIPTION_BLOCKER)
        if blockers:
            print("cannot approve %s %s: %s" % (app, record["version"], "; ".join(blockers)), file=sys.stderr)
            return EXIT_PROBLEM
        if _is_flagged(record):
            print("approving %s %s with:" % (app, record["version"]))
            _print_flagged(record)
        approve_record(st, record, args.no_sandbox, args.note, args.description_reviewed)
        print("approved %s %s" % (app, record["version"]))
    return EXIT_OK


def cmd_revoke(cfg, st, args):
    app, version = _split_target(args.target)
    with st.lock():
        entry = st.approved().get(app)
        if entry is None or (version and entry.get("version") != version):
            raise UsageError("%s is not approved" % args.target)
        who, when = _actor(), _stamp()
        st.remove_approved(app, {"at": when, "action": "revoke", "app": app, "version": entry.get("version"),
                                 "sha256": entry.get("sha256"), "by": who, "note": args.note})
        record = st.find_candidate(app, entry.get("version"), entry.get("sha256"))
        if record is not None:
            st.update_candidate(record, {"status": "revoked", "decided": {
                "action": "revoke", "at": when, "by": who, "note": args.note}})
    print("revoked %s %s" % (app, entry.get("version")))
    return EXIT_OK


def cmd_reject(cfg, st, args):
    app, version = _split_target(args.target, version_required=True)
    with st.lock():
        records = [r for r in st.candidates(app=app) if r.get("version") == version
                   and r.get("status") in OPEN_STATUSES]
        if not records:
            raise UsageError("no pending candidate for %s@%s" % (app, version))
        who, when = _actor(), _stamp()
        for record in records:
            st.append_log({"at": when, "action": "reject", "app": app, "version": version,
                           "sha256": record.get("sha256"), "by": who, "note": args.note})
            st.update_candidate(record, {"status": "rejected", "decided": {
                "action": "reject", "at": when, "by": who, "note": args.note}})
            print("rejected %s %s" % (app, version))
    return EXIT_OK


# reports, build, metrics

def _reports_keys(path):
    """[reports] keys of server.conf (the directory of sandbox public keys)."""
    cp = configparser.ConfigParser(inline_comment_prefixes=("#",), interpolation=None)
    cp.read(path, encoding="utf-8")
    return cp.get("reports", "keys", fallback=DEFAULT_REPORT_KEYS).strip() or DEFAULT_REPORT_KEYS


def cmd_reports_ingest(cfg, st, args):
    with st.lock():
        try:
            results = reports.ingest(cfg, st, _reports_keys(args.config))
        except reports.ReportError as e:
            print("error: %s" % e, file=sys.stderr)
            return EXIT_PROBLEM
    for item in results:
        print(_describe(item))
    return EXIT_PROBLEM if any(r["result"] == "rejected" for r in results) else EXIT_OK


def cmd_sandbox_queue(cfg, st, args):
    with st.lock():
        try:
            messages = sandboxqueue.run(cfg, st, _load_content(cfg))
        except sandboxqueue.QueueError as e:
            print("error: %s" % e, file=sys.stderr)
            return EXIT_PROBLEM
    for line in messages:
        print(_clean(line))
    return EXIT_OK


def cmd_shots_request(cfg, st, args):
    if args.for_session and args.cancel:
        raise UsageError("--for-session cannot be combined with --cancel")
    with st.lock():
        if args.cancel:
            print(("cancelled shots request for %s" if st.remove_shots_request(args.app)
                   else "no shots request for %s") % args.app)
            return EXIT_OK
        entry = _load_content(cfg).app(args.app)
        if entry is None:
            raise UsageError("unknown app %s" % args.app)
        if entry.shots is None:
            raise UsageError("%s has no valid shots.yaml" % args.app)
        existing = [r for r in st.shots_requests() if r["app"] == args.app]
        if args.for_session:
            now = catalogwriter.now()
            record = {"app": args.app, "requested": existing[0].get("requested", _stamp()) if existing else _stamp(),
                      "by": existing[0].get("by", _actor()) if existing else _actor(),
                      "session": True, "expires": shotsreq.session_expiry(now)}
            st.add_shots_request(args.app, record)
            print("requested session shots for %s" % args.app)
            return EXIT_OK
        if existing:
            print("shots already requested for %s" % args.app)
            return EXIT_OK
        st.add_shots_request(args.app, {"app": args.app, "requested": _stamp(), "by": _actor()})
    print("requested shots for %s" % args.app)
    return EXIT_OK


def _shots(func):
    """Adapter: the shots-* commands work on paths.screenshots only and never take the state lock."""
    return lambda cfg, st, args: func(cfg, args)


def cmd_build(cfg, st, args):
    with st.lock():
        try:
            summary = catalog.build(cfg, st, _load_content(cfg), if_changed=args.if_changed)
        except catalog.CatalogError as e:
            print("error: %s" % e, file=sys.stderr)
            return EXIT_PROBLEM
    if not summary["changed"]:
        print("catalog unchanged (serial %s)" % summary["serial"])
        return EXIT_OK
    print("built serial %s, revision %s, %d apps" % (summary["serial"], summary["revision"], summary["apps"]))
    for item in summary["skipped"]:
        print("not published: %s (%s)" % (item["id"], item["reason"]))
    for line in summary.get("warnings", ()):
        print("warning: %s" % _clean(line))
    return EXIT_OK


def _epoch(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _vendor_ids(cfg):
    directory = os.path.join(cfg.state, "vendors")
    names = os.listdir(directory) if os.path.isdir(directory) else []
    return sorted(n[:-5] for n in names if n.endswith(".json") and ID_RE.match(n[:-5]))


def _catalog_expiry(cfg):
    try:
        with open(os.path.join(cfg.preview, "current", "manifest.json"), encoding="utf-8") as f:
            return _epoch(json.load(f)["expires"])
    except (OSError, ValueError, KeyError):
        return None


def metrics_text(cfg, st):
    candidates = st.candidates()
    count = lambda status: sum(1 for r in candidates if r.get("status") == status)  # noqa: E731
    state = st.catalog_state()
    lines = [("agino_store_pending_candidates", None, count("pending")),
             ("agino_store_awaiting_upload_candidates", None, count("awaiting-upload")),
             ("agino_store_approved_apps", None, len(st.approved())),
             ("agino_store_catalog_last_build_timestamp_seconds", None, _epoch(state.get("built_at"))),
             ("agino_store_catalog_serial", None, state.get("serial")),
             ("agino_store_catalog_expires_timestamp_seconds", None, _catalog_expiry(cfg))]
    for vendor in _vendor_ids(cfg):
        memo = st.vendor_memo(vendor)
        label = '{vendor="%s"}' % vendor
        lines.append(("agino_store_vendor_last_ok_timestamp_seconds", label, _epoch(memo.get("last_ok"))))
        lines.append(("agino_store_vendor_last_error", label, 1 if memo.get("last_error") else 0))
    return "".join("%s%s %s\n" % (name, label or "", "%.0f" % value if isinstance(value, float) else value)
                   for name, label, value in lines if value is not None)


def cmd_metrics(cfg, st, args):
    text = metrics_text(cfg, st)
    os.makedirs(cfg.metrics, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=cfg.metrics)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, os.path.join(cfg.metrics, METRICS_FILE))
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise
    return EXIT_OK


def cmd_status(cfg, st, args):
    if not args.json:
        raise UsageError("status needs --json")
    try:
        text = status.build(cfg, args.section, _clean)
    except status.UnknownSection as e:
        raise UsageError(str(e)) from e
    print(text)
    return EXIT_OK


COMMANDS = {"track": cmd_track, "check": cmd_check, "intake": cmd_intake, "pending": cmd_pending,
            "approve": cmd_approve, "revoke": cmd_revoke, "reject": cmd_reject, "reports-ingest": cmd_reports_ingest,
            "sandbox-queue": cmd_sandbox_queue, "shots-request": cmd_shots_request,
            "shots-import": _shots(shots_import.cmd_import),
            "shots-session-import": _shots(shots_session.cmd_session_import), "shots-ingest": _shots(shots_cmds.cmd_ingest),
            "shots-review": _shots(shots_cmds.cmd_review), "shots-remove": _shots(shots_cmds.cmd_remove),
            "content-edit": content_edit.cmd_edit, "content-confirm": content_edit.cmd_confirm,
            "upstream-check": cmd_upstream_check, "build-request": cmd_build_request,
            "build-cancel": cmd_build_cancel, "build-step": cmd_build_step,
            "build": cmd_build, "metrics": cmd_metrics, "status": cmd_status}
# Commands whose implementation lives in modules written separately: any failure there is reported, not traced.
GUARDED = ("track", "check", "intake")


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        cfg = config.load(args.config)
    except config.ConfigError as e:
        print("error: %s" % e, file=sys.stderr)
        return EXIT_USAGE
    if cfg.allow_insecure and os.environ.get(TESTS_ENV) != "1":
        print("error: [fetch] allow_insecure = true is for tests only (set %s=1 to override)" % TESTS_ENV,
              file=sys.stderr)
        return EXIT_USAGE
    st = State(cfg.state)
    try:
        return COMMANDS[args.command](cfg, st, args)
    except (UsageError, shots_cmds.ShotsUsage, builds.BuildUsage) as e:
        print("error: %s" % e, file=sys.stderr)
        return EXIT_USAGE
    except StateLocked as e:
        print("error: %s" % e, file=sys.stderr)
        return EXIT_LOCKED
    except content.ContentError as e:
        print("store-content is invalid:\n%s" % e, file=sys.stderr)
        return EXIT_PROBLEM
    except (StateError, shots_store.ShotsError, content_edit.EditError, builds.BuildError, OSError) as e:
        print("error: %s" % e, file=sys.stderr)
        return EXIT_PROBLEM
    except Exception as e:  # noqa: BLE001 - see GUARDED
        if args.command not in GUARDED:
            raise
        print("error: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return EXIT_PROBLEM
