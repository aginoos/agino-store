# SPDX-License-Identifier: GPL-3.0-or-later
"""Build output directory `out/<id>/`: status.json, result.json, build.log, <name>.deb (AI tasks: ai/<file>...).

Owner agino-sandbox, group agino-store (inherited from the setgid `out` directory, never set here: the units
run with RestrictSUIDSGID, so no chmod may carry the setgid bit), directories 0750 and files 0640. Everything the
guest produced is untrusted: the log is stripped of control characters and capped, the artifact is written
under a name this module is given (never one the guest chose).
"""

import codecs
import hashlib
import json
import os
import re
import time

from .build_task import TASK_ID_RE
from .util import SandboxError, log, now

FILE_MODE = 0o640
DIR_MODE = 0o750
RETENTION_SECONDS = 7 * 24 * 3600
TERMINAL_STATES = ("done", "failed", "cancelled")
STATUS_FILE = "status.json"
RESULT_FILE = "result.json"
LOG_FILE = "build.log"
AI_DIR = "ai"
FAILURE_CLASSES = ("ok", "recipe", "infra", "timeout", "model")
# C0 controls other than \n and \t, DEL, C1 controls, line/paragraph separators, bidi overrides, BOM, non-characters.
_BAD_CHARS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f  ‪-‮⁦-⁩﻿￾￿]")


def clean_log_text(text):
    """Text that is safe to show: CR/CRLF become newlines, other control characters are dropped."""
    return _BAD_CHARS.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def _unlink(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        log("cannot remove %s: %s" % (path, e.strerror))


class LogSink:
    """Appends sanitised guest output to build.log; stops (with a marker) once `limit` bytes were written."""

    def __init__(self, path, limit):
        self.limit, self.written, self.full = limit, 0, False
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, FILE_MODE)
        os.fchmod(fd, FILE_MODE)
        self._f = os.fdopen(fd, "ab")

    def feed(self, data):
        """Guest bytes (any chunking)."""
        if not self.full:
            self._append(clean_log_text(self._decoder.decode(data)))

    def note(self, text):
        """A line written by the runner itself (step headers, errors); not subject to the cap's marker logic."""
        if not self.full:
            self._append(clean_log_text(text) + "\n")

    def _append(self, text):
        raw = text.encode("utf-8")
        room = self.limit - self.written
        if len(raw) > room:
            raw = raw[:max(room, 0)].decode("utf-8", "ignore").encode("utf-8")
            raw += ("\n[log truncated: the limit of %d MiB was reached]\n" % (self.limit >> 20)).encode("utf-8")
            self.full = True
        self._f.write(raw)
        self._f.flush()
        self.written += len(raw)

    def close(self):
        self._f.close()


class ArtifactWriter:
    """Streams one artifact into `.<name>.tmp`; commit() renames it into place, abort() removes it."""

    def __init__(self, directory, name):
        self.name = name
        self._tmp = os.path.join(directory, "." + name + ".tmp")
        self._final = os.path.join(directory, name)
        fd = os.open(self._tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE)
        os.fchmod(fd, FILE_MODE)
        self._f = os.fdopen(fd, "wb")
        self._hash = hashlib.sha256()
        self.size = 0

    def write(self, data):
        self._f.write(data)
        self._hash.update(data)
        self.size += len(data)

    def commit(self):
        """(sha256 hex, size) once the file is in place."""
        self._f.close()
        os.rename(self._tmp, self._final)
        return self._hash.hexdigest(), self.size

    def abort(self):
        self._f.close()
        _unlink(self._tmp)


class OutDir:
    def __init__(self, out_root, task_id, max_log_bytes):
        if not TASK_ID_RE.match(task_id):
            raise SandboxError("bad task id")
        self.root, self.id = out_root, task_id
        self.directory = os.path.join(out_root, task_id)
        self.max_log_bytes = max_log_bytes
        self.sink = None

    def create(self):
        """Make out/<id>/ (0750 through the umask; the group comes from the setgid parent) and open the log."""
        old = os.umask(0o027)
        try:
            os.makedirs(self.root, mode=DIR_MODE, exist_ok=True)
            os.makedirs(self.directory, mode=DIR_MODE, exist_ok=True)
        finally:
            os.umask(old)
        self.sink = LogSink(os.path.join(self.directory, LOG_FILE), self.max_log_bytes)

    def close(self):
        if self.sink is not None:
            self.sink.close()
            self.sink = None

    def write_json(self, name, doc):
        """Atomic: temp file, then rename, so a reader never sees a partial file."""
        data = (json.dumps(doc, indent=1, sort_keys=True) + "\n").encode("utf-8")
        path = os.path.join(self.directory, name)
        tmp = os.path.join(self.directory, "." + name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, FILE_MODE)
        try:
            with os.fdopen(fd, "wb") as f:
                os.fchmod(f.fileno(), FILE_MODE)
                f.write(data)
            os.rename(tmp, path)
        except BaseException:
            _unlink(tmp)
            raise

    def write_status(self, state, started, step=0, steps_total=0, error=""):
        self.write_json(STATUS_FILE, {"id": self.id, "state": state, "started": started, "updated": now(),
                                      "step": step, "steps_total": steps_total, "error": error[:300]})

    def write_result(self, ok, error, steps, artifacts, klass=None, files=None):
        """result.json. `klass` is the failure class (ok, recipe, infra, timeout, model; by default ok or infra);
        `files` (AI tasks only) lists the delivered files in ai/."""
        if ok:
            klass = "ok"
        elif klass not in FAILURE_CLASSES or klass == "ok":
            klass = "infra"
        doc = {"ok": bool(ok), "error": error[:300], "steps": steps, "artifacts": artifacts, "class": klass}
        if files is not None:
            doc["files"] = list(files)
        self.write_json(RESULT_FILE, doc)

    def ai_directory(self):
        """out/<id>/ai/ (0750 through the umask), created on first use; returns its path."""
        path = os.path.join(self.directory, AI_DIR)
        old = os.umask(0o027)
        try:
            os.makedirs(path, mode=DIR_MODE, exist_ok=True)
        finally:
            os.umask(old)
        return path

    def ai_artifact(self, name):
        return ArtifactWriter(self.ai_directory(), name)

    def artifact(self, name):
        return ArtifactWriter(self.directory, name)


def read_status(out_root, task_id):
    """The parsed status.json of a task, {} when there is none, None when it is unreadable or malformed."""
    path = os.path.join(out_root, task_id, STATUS_FILE)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as f:
            doc = json.loads(f.read(1 << 16).decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None
    return doc if isinstance(doc, dict) else None


def _remove_ai_dir(path):
    """Unlink the regular files of out/<id>/ai/ (the only subdirectory we create), then the directory itself."""
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return
    for name in names:
        entry = os.path.join(path, name)
        if not os.path.isdir(entry) or os.path.islink(entry):
            _unlink(entry)
    try:
        os.rmdir(path)
    except OSError:
        pass


def cleanup_old(out_root, queue, wall=time.time):
    """Remove the files of out/<id>/ directories older than 7 days; returns the ids touched.

    Only `^[0-9a-f]{16}$` directories that are real directories are considered, only terminal tasks, and only
    the files found directly in them are unlinked. While queue/<id>/ still exists the status.json is kept as a
    tombstone: without it the runner would see the task as new and build it again."""
    touched = []
    try:
        names = sorted(os.listdir(out_root))
    except OSError:
        return touched
    for name in names:
        if not TASK_ID_RE.match(name):
            continue
        directory = os.path.join(out_root, name)
        try:
            info = os.lstat(os.path.join(directory, STATUS_FILE))  # the terminal status is written once
        except OSError:
            continue
        status = read_status(out_root, name)
        if os.path.islink(directory) or not os.path.isdir(directory) or not status \
                or status.get("state") not in TERMINAL_STATES or wall() - info.st_mtime < RETENTION_SECONDS:
            continue
        keep_tombstone = os.path.lexists(os.path.join(queue, name))
        try:
            entries = sorted(os.listdir(directory))
        except OSError:
            continue
        for entry in entries:
            path = os.path.join(directory, entry)
            if entry == AI_DIR and os.path.isdir(path) and not os.path.islink(path):
                _remove_ai_dir(path)
                continue
            if os.path.isdir(path) and not os.path.islink(path):
                continue
            if keep_tombstone and entry == STATUS_FILE:
                continue
            _unlink(path)
        if not keep_tombstone:
            try:
                os.rmdir(directory)
            except OSError:
                pass
        touched.append(name)
    return touched
