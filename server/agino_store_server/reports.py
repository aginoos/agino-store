# SPDX-License-Identifier: GPL-3.0-or-later
"""Import sandbox reports (docs/server.md section 9). Reports are only ever parsed as data."""

import json
import os
import re
import stat

ID_RE = re.compile(r"\A[a-z0-9][a-z0-9.+-]*\Z")
VERSION_RE = re.compile(r"\A[A-Za-z0-9.+~:-]+\Z")
SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
MAX_REPORT_BYTES = 1 << 20
MAX_TEXT = 2000
TOP_KEYS = {"schema", "app", "version", "sha256", "status", "started", "finished", "host", "checks", "summary"}
REQUIRED_KEYS = {"schema", "app", "version", "sha256", "status"}
CHECK_TYPES = {"installed": bool, "third_party_sources": list, "desktop_file": (str, type(None)),
               "window_seen": bool, "new_errors": int, "removed_clean": bool}
OPEN_STATUSES = ("pending", "approved")


class ReportError(Exception):
    pass


def _read(path):
    """Contents of a regular file of at most MAX_REPORT_BYTES, never through a symlink."""
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise ReportError("not a regular file")
    if st.st_size > MAX_REPORT_BYTES:
        raise ReportError("larger than 1 MiB")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise ReportError("not a regular file")
        return f.read(MAX_REPORT_BYTES + 1)


def _text(value, what, limit=MAX_TEXT):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 0x20 and c not in "\n\t" for c in value):
        raise ReportError("bad %s" % what)
    return value


def _check_details(checks):
    if not isinstance(checks, dict) or set(checks) - set(CHECK_TYPES):
        raise ReportError("bad checks")
    for key, value in checks.items():
        want = CHECK_TYPES[key]
        if not isinstance(value, want) or (want is int and isinstance(value, bool)):
            raise ReportError("bad checks.%s" % key)
    sources = checks.get("third_party_sources", [])
    if not all(isinstance(s, str) and len(s) <= 500 for s in sources):
        raise ReportError("bad checks.third_party_sources")
    if isinstance(checks.get("desktop_file"), str):
        _text(checks["desktop_file"], "checks.desktop_file", 255)
    return {k: (list(v) if isinstance(v, list) else v) for k, v in checks.items()}


def parse(raw):
    """Validated copy of a report, or ReportError."""
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise ReportError("not valid JSON") from e
    if not isinstance(doc, dict) or set(doc) - TOP_KEYS or REQUIRED_KEYS - set(doc):
        raise ReportError("unexpected or missing fields")
    if doc["schema"] != 1 or isinstance(doc["schema"], bool):
        raise ReportError("unsupported schema")
    if not isinstance(doc["app"], str) or not ID_RE.match(doc["app"]):
        raise ReportError("bad app")
    if not isinstance(doc["version"], str) or not VERSION_RE.match(doc["version"]):
        raise ReportError("bad version")
    if not isinstance(doc["sha256"], str) or not SHA256_RE.match(doc["sha256"]):
        raise ReportError("bad sha256")
    if doc["status"] not in ("pass", "fail"):
        raise ReportError("status must be pass or fail")
    out = {k: doc[k] for k in REQUIRED_KEYS}
    for key in ("started", "finished", "host", "summary"):
        out[key] = _text(doc.get(key, ""), key, 200 if key != "summary" else MAX_TEXT)
    out["checks"] = _check_details(doc.get("checks", {}))
    return out


def _unique_dest(directory, name):
    dest = os.path.join(directory, name)
    n = 1
    while os.path.lexists(dest):
        dest = os.path.join(directory, "%s.%d" % (name, n))
        n += 1
    return dest


def _move(cfg, name, target):
    directory = os.path.join(cfg.reports, target)
    os.makedirs(directory, exist_ok=True)
    os.rename(os.path.join(cfg.reports, name), _unique_dest(directory, name))


def _apply(st, name, report):
    record = st.find_candidate(report["app"], report["version"], report["sha256"])
    if record is None:
        raise ReportError("no candidate with this app, version and sha256")
    if record.get("status") not in OPEN_STATUSES:
        raise ReportError("candidate is %s" % record.get("status"))
    sandbox = {"status": report["status"], "report": name, "summary": report["summary"],
               "finished": report["finished"], "host": report["host"], "details": report["checks"]}
    st.update_candidate(record, {"checks": dict(record.get("checks") or {}, sandbox=sandbox)})


def _ingest_one(cfg, st, name):
    try:
        report = parse(_read(os.path.join(cfg.reports, name)))
        _apply(st, name, report)
    except (ReportError, OSError) as e:
        _move(cfg, name, "rejected")
        return {"file": name, "result": "rejected", "reason": str(e)}
    _move(cfg, name, "done")
    return {"file": name, "result": "done", "app": report["app"], "version": report["version"],
            "status": report["status"]}


def ingest(cfg, st):
    """Import new reports below cfg.reports; returns one result dict per file seen."""
    try:
        names = sorted(os.listdir(cfg.reports))
    except FileNotFoundError:
        return []
    results = []
    for name in names:
        path = os.path.join(cfg.reports, name)
        if not name.endswith(".json") or name.startswith("."):
            continue
        if os.path.isdir(path) and not os.path.islink(path):
            continue
        results.append(_ingest_one(cfg, st, name))
    return results
