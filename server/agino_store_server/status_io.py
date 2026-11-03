# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only helpers for `status --json` (docs/server.md section 11).

Nothing here writes, locks or follows symlinks on the file it opens. Every read is a single
open + read, so a file replaced concurrently (state files are renamed over atomically) is seen
either whole-old or whole-new.
"""

import datetime
import json
import os
import stat

MAX_READ = 8 << 20
SMALL_READ = 1 << 20


class ReadError(Exception):
    pass


def read_bytes(path, limit=MAX_READ):
    """Contents of a regular file of at most `limit` bytes, never through a symlink (last component)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise ReadError("%s: %s" % (os.path.basename(path), e.strerror)) from e
    with os.fdopen(fd, "rb") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ReadError("%s: not a regular file" % os.path.basename(path))
        if info.st_size > limit:
            raise ReadError("%s: larger than %d bytes" % (os.path.basename(path), limit))
        return f.read(limit + 1)


def read_json(path, limit=MAX_READ):
    """Parsed JSON of a file, or ReadError (missing, unreadable, partial or invalid)."""
    data = read_bytes(path, limit)
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise ReadError("%s: invalid JSON" % os.path.basename(path)) from e


def tail_lines(path, count, window=256 << 10):
    """The last `count` lines of a text file (read from the last `window` bytes), or ReadError."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise ReadError("%s: %s" % (os.path.basename(path), e.strerror)) from e
    with os.fdopen(fd, "rb") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ReadError("%s: not a regular file" % os.path.basename(path))
        start = max(0, info.st_size - window)
        f.seek(start)
        data = f.read(window)
    lines = data.decode("utf-8", "replace").splitlines()
    if start and lines:
        lines = lines[1:]   # the first line is probably cut
    return lines[-count:]


def list_dir(path):
    """Sorted names in a directory (hidden temp files excluded), or ReadError."""
    try:
        return sorted(n for n in os.listdir(path) if not n.startswith("."))
    except OSError as e:
        raise ReadError("%s: %s" % (os.path.basename(path) or path, e.strerror)) from e


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mtime_iso(path):
    try:
        return iso(os.lstat(path).st_mtime)
    except OSError:
        return None


def pick(mapping, keys):
    """The listed keys of a dict (empty dict if it is not one)."""
    return {k: mapping[k] for k in keys if k in mapping} if isinstance(mapping, dict) else {}


def sanitize(value, clean):
    """A copy of a JSON-like structure with every string (and key) passed through `clean`."""
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, dict):
        return {clean(str(k)): sanitize(v, clean) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v, clean) for v in value]
    return value


class Errors:
    def __init__(self):
        self.items = []

    def add(self, section, detail):
        self.items.append({"section": section, "detail": str(detail)})
