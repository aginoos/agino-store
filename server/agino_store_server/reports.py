# SPDX-License-Identifier: GPL-3.0-or-later
"""Import sandbox reports (docs/server.md section 9). Reports are only ever parsed as data."""

import base64
import contextlib
import json
import os
import re
import stat
import subprocess
import tempfile

ID_RE = re.compile(r"\A[a-z0-9][a-z0-9.+-]*\Z")
VERSION_RE = re.compile(r"\A(?:[0-9]+:)?[0-9][A-Za-z0-9.+~-]*\Z")
SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
MAX_REPORT_BYTES = 1 << 20
MAX_TEXT = 2000
MAX_SIG_BYTES = 256
ED25519_DER_PREFIX = bytes.fromhex("302a300506032b6570032100")
# "details": the sandbox's step-by-step data; only type-checked here. It stays in reports/done/
# for people to read and is not copied into the candidate.
TOP_KEYS = {"schema", "app", "version", "sha256", "status", "started", "finished", "host", "checks", "summary",
            "details"}
REQUIRED_KEYS = {"schema", "app", "version", "sha256", "status"}
CHECK_TYPES = {"installed": bool, "third_party_sources": list, "desktop_file": (str, type(None)),
               "window_seen": bool, "new_errors": int, "removed_clean": bool}
OPEN_STATUSES = ("pending", "approved")


class ReportError(Exception):
    pass


def _read(dir_fd, name, limit=MAX_REPORT_BYTES):
    """Contents of a regular file of at most `limit` bytes inside dir_fd, never through a symlink."""
    st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode):
        raise ReportError("not a regular file")
    if st.st_size > limit:
        raise ReportError("larger than %s" % ("1 MiB" if limit == MAX_REPORT_BYTES else "%d bytes" % limit))
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise ReportError("not a regular file")
        return f.read(limit + 1)


def load_keys(keys_dir):
    """Raw 32-byte Ed25519 public keys from the *.pub files (one line of base64 each) in keys_dir."""
    keys = []
    try:
        names = sorted(os.listdir(keys_dir))
    except OSError:
        return keys
    for name in names:
        path = os.path.join(keys_dir, name)
        if not name.endswith(".pub") or name.startswith(".") or not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as f:
                raw = base64.b64decode(f.read(1024).strip(), validate=True)
        except (OSError, ValueError):
            continue
        if len(raw) == 32:
            keys.append(raw)
    return keys


def verify_signature(data, sig_text, keys):
    """True if sig_text (one line of base64) is an Ed25519 signature of data by one of keys."""
    try:
        sig = base64.b64decode(sig_text.strip(), validate=True)
    except ValueError:
        return False
    if len(sig) != 64:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        paths = {n: os.path.join(tmp, n) for n in ("data", "sig", "pub.der")}
        for key, content in (("data", data), ("sig", sig)):
            with open(paths[key], "wb") as f:
                f.write(content)
        for raw in keys:
            with open(paths["pub.der"], "wb") as f:
                f.write(ED25519_DER_PREFIX + raw)
            done = subprocess.run(["openssl", "pkeyutl", "-verify", "-rawin", "-pubin", "-keyform", "DER",
                                   "-inkey", paths["pub.der"], "-in", paths["data"], "-sigfile", paths["sig"]],
                                  capture_output=True)
            if done.returncode == 0:
                return True
    return False


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
    if not isinstance(doc.get("details", {}), dict):
        raise ReportError("bad details")
    if doc["status"] not in ("pass", "fail"):
        raise ReportError("status must be pass or fail")
    out = {k: doc[k] for k in REQUIRED_KEYS}
    for key in ("started", "finished", "host", "summary"):
        out[key] = _text(doc.get(key, ""), key, 200 if key != "summary" else MAX_TEXT)
    out["checks"] = _check_details(doc.get("checks", {}))
    return out


def _open_dir(name, dir_fd=None):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=dir_fd)
    except OSError as e:
        raise ReportError("%s is a symlink or not a directory (%s)" % (name, e.strerror)) from e


def _open_subdir(root_fd, name):
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, 0o755, dir_fd=root_fd)
    return _open_dir(name, root_fd)


def _unique_name(dir_fd, name):
    candidate, n = name, 1
    while True:
        try:
            os.stat(candidate, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return candidate
        candidate = "%s.%d" % (name, n)
        n += 1


def _move(root_fd, dest_fd, name):
    """Move name and name.sig (if there) from root_fd to dest_fd, keeping the pair's names in step."""
    new = _unique_name(dest_fd, name)
    os.rename(name, new, src_dir_fd=root_fd, dst_dir_fd=dest_fd)
    with contextlib.suppress(FileNotFoundError):
        os.rename(name + ".sig", new + ".sig", src_dir_fd=root_fd, dst_dir_fd=dest_fd)


def _apply(st, name, report):
    record = st.find_candidate(report["app"], report["version"], report["sha256"])
    if record is None:
        raise ReportError("no candidate with this app, version and sha256")
    if record.get("status") not in OPEN_STATUSES:
        raise ReportError("candidate is %s" % record.get("status"))
    sandbox = {"status": report["status"], "report": name, "summary": report["summary"],
               "finished": report["finished"], "host": report["host"], "details": report["checks"]}
    st.update_candidate(record, {"checks": dict(record.get("checks") or {}, sandbox=sandbox)})


def _checked(root_fd, name, keys):
    """The validated report once its signature has been verified; nothing is parsed before that."""
    data = _read(root_fd, name)
    if len(data) > MAX_REPORT_BYTES:
        raise ReportError("larger than 1 MiB")
    try:
        sig = _read(root_fd, name + ".sig", MAX_SIG_BYTES)
    except FileNotFoundError:
        raise ReportError("unsigned (no %s.sig)" % name) from None
    if not keys or len(sig) > MAX_SIG_BYTES or not verify_signature(data, sig.decode("ascii", "replace"), keys):
        raise ReportError("signature missing or not valid for any key in the sandbox key directory")
    return parse(data)


def _ingest_one(st, root_fd, done_fd, rejected_fd, name, keys):
    try:
        report = _checked(root_fd, name, keys)
        _apply(st, name, report)
    except (ReportError, OSError) as e:
        _move(root_fd, rejected_fd, name)
        return {"file": name, "result": "rejected", "reason": str(e)}
    _move(root_fd, done_fd, name)
    return {"file": name, "result": "done", "app": report["app"], "version": report["version"],
            "status": report["status"]}


def _candidates(root_fd):
    """Names of the .json entries that are not real directories, sorted."""
    names = []
    with os.scandir(root_fd) as it:
        for entry in it:
            if not entry.name.endswith(".json") or entry.name.startswith("."):
                continue
            if stat.S_ISDIR(entry.stat(follow_symlinks=False).st_mode):
                continue
            names.append(entry.name)
    return sorted(names)


def ingest(cfg, st, keys_dir):
    """Import new signed reports below cfg.reports; returns one result dict per file seen.

    keys_dir holds the sandbox hosts' public keys. Unsigned or badly signed reports are rejected.
    Raises ReportError if reports/, done/ or rejected/ is a symlink or not a directory.
    """
    try:
        root_fd = _open_dir(cfg.reports)
    except ReportError:
        if not os.path.lexists(cfg.reports):
            return []
        raise
    done_fd = rejected_fd = None
    try:
        done_fd = _open_subdir(root_fd, "done")
        rejected_fd = _open_subdir(root_fd, "rejected")
        keys = load_keys(keys_dir)
        return [_ingest_one(st, root_fd, done_fd, rejected_fd, name, keys) for name in _candidates(root_fd)]
    finally:
        for fd in (root_fd, done_fd, rejected_fd):
            if fd is not None:
                os.close(fd)
