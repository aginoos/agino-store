# SPDX-License-Identifier: GPL-3.0-or-later
"""State directory (docs/server.md section 4). Other modules never touch these files directly."""

import contextlib
import copy
import fcntl
import json
import os
import re
import tempfile

ID_RE = re.compile(r"\A[a-z0-9][a-z0-9.+-]*\Z")
VERSION_RE = re.compile(r"\A[A-Za-z0-9.+~:-]+\Z")
SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")


class StateError(Exception):
    pass


class StateLocked(StateError):
    pass


def write_json_atomic(path, data):
    """Write JSON next to its final name, fsync, then rename over it."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return copy.deepcopy(default)
    except (OSError, ValueError) as e:
        raise StateError("cannot read %s: %s" % (path, e)) from e


def _check_id(value, what):
    if not isinstance(value, str) or not ID_RE.match(value):
        raise StateError("bad %s: %r" % (what, value))


def _candidate_name(record):
    version = record.get("version")
    if not isinstance(version, str) or not VERSION_RE.match(version):
        raise StateError("bad version: %r" % (version,))
    safe_version = version.replace(":", "_")
    sha = record.get("sha256")
    if sha is None:
        if record.get("status") != "awaiting-upload":
            raise StateError("candidate without sha256 must be awaiting-upload")
        return "%s_upload" % safe_version
    if not SHA256_RE.match(sha):
        raise StateError("bad sha256: %r" % (sha,))
    return "%s_%s" % (safe_version, sha[:12])


class State:
    def __init__(self, root):
        self.root = os.path.abspath(root)

    def _path(self, *parts):
        return os.path.join(self.root, *parts)

    @contextlib.contextmanager
    def lock(self):
        os.makedirs(self.root, exist_ok=True)
        fd = os.open(self._path(".lock"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise StateLocked("another agino-store-server task is running") from e
            yield self
        finally:
            os.close(fd)

    # Vendor tracking memo

    def vendor_memo(self, vendor_id):
        _check_id(vendor_id, "vendor id")
        return read_json(self._path("vendors", vendor_id + ".json"), {})

    def save_vendor_memo(self, vendor_id, memo):
        _check_id(vendor_id, "vendor id")
        write_json_atomic(self._path("vendors", vendor_id + ".json"), memo)

    # Candidates

    def _candidate_path(self, record):
        _check_id(record.get("app"), "app id")
        return self._path("candidates", record["app"], _candidate_name(record) + ".json")

    def candidates(self, app=None, status=None):
        base = self._path("candidates")
        apps = [app] if app else (sorted(os.listdir(base)) if os.path.isdir(base) else [])
        out = []
        for a in apps:
            _check_id(a, "app id")
            d = os.path.join(base, a)
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if not name.endswith(".json") or name.startswith("."):
                    continue
                path = os.path.join(d, name)
                rec = read_json(path, None)
                if rec is None or (status and rec.get("status") != status):
                    continue
                out.append(dict(rec, _path=path))
        return out

    def find_candidate(self, app, version, sha256=None):
        for rec in self.candidates(app=app):
            if rec.get("version") == version and (sha256 is None or rec.get("sha256") == sha256):
                return rec
        return None

    def add_candidate(self, record):
        path = self._candidate_path(record)
        existing = read_json(path, None)
        if existing is not None:
            return dict(existing, _path=path)
        clean = {k: v for k, v in record.items() if k != "_path"}
        write_json_atomic(path, clean)
        return dict(clean, _path=path)

    def update_candidate(self, record, changes):
        old_path = record.get("_path") or self._candidate_path(record)
        merged = {k: v for k, v in record.items() if k != "_path"}
        merged.update(changes)
        new_path = self._candidate_path(merged)
        write_json_atomic(new_path, merged)
        if new_path != old_path:
            # An awaiting-upload record gets its real name once the file has been taken in.
            with contextlib.suppress(FileNotFoundError):
                os.unlink(old_path)
        return dict(merged, _path=new_path)

    def inspect_dir(self, record):
        path = self._candidate_path(record)
        return path[: -len(".json")] + ".inspect"

    # Approvals

    def approved(self):
        return read_json(self._path("approved.json"), {})

    def set_approved(self, app, entry, log):
        _check_id(app, "app id")
        data = dict(self.approved())
        data[app] = entry
        write_json_atomic(self._path("approved.json"), data)
        self.append_log(log)

    def append_log(self, entry):
        os.makedirs(self.root, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
        with open(self._path("approvals.log"), "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    # milkyway versions and catalog state

    def milkyway(self):
        return read_json(self._path("milkyway.json"), {})

    def save_milkyway(self, data):
        write_json_atomic(self._path("milkyway.json"), data)

    def catalog_state(self):
        return read_json(self._path("catalog.json"), {})

    def save_catalog_state(self, data):
        write_json_atomic(self._path("catalog.json"), data)

    def cache_dir(self, name):
        _check_id(name, "cache name")
        path = self._path(name)
        os.makedirs(path, exist_ok=True)
        return path
