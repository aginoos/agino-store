# SPDX-License-Identifier: GPL-3.0-or-later
"""shots-session-import: take picked captures of an interactive sandbox session in as drafts
(docs/server.md section 12, docs/sandbox.md section 9).

The captures directory is written by the sandbox daemon and the guest is less trusted than this
user (which can sign), so everything there is treated as hostile: the directory is opened through a
file descriptor without following symlinks, only regular files with the exact names the daemon
writes are read, kind and language come from the file name (width, height and sha256 are recomputed
from the decoded image), and every image is validated and rewritten through pngclean before it is
stored. One session captures many apps: captures.json is used for exactly one thing, the `app` of
each capture, and only captures recorded for the app being imported are taken in (the others are
refused and counted as problems).
"""

import json

import errno
import hashlib
import os
import re
import stat
import struct

from . import shots_store as S
from .shots_cmds import MAX_SOURCE_BYTES, ShotsUsage, check_app, check_reviewer
from .status_io import ReadError

SESSION_RE = re.compile(r"\A[0-9a-f]{16}\Z")
PICK_RE = re.compile(r"\A[0-9]{2}\Z")
NAME_RE = re.compile(r"\A([0-9]{2})-(window|screen)-(zh_CN|en_US)\.png\Z")
MAX_PICKS = 40
MAX_NUMBER = 99
INDEX_NAME = "captures.json"
MAX_INDEX_BYTES = 1 << 20
# 8-bit RGB, not interlaced: exactly what the daemon writes (and bounds the decode to 3 bytes per pixel)
EXPECTED_IHDR = (8, 2, 0, 0, 0)


def parse_picks(text):
    """Sorted list of two-digit capture numbers from 'NN[,NN...]'; ShotsUsage for anything else."""
    if not isinstance(text, str):
        raise ShotsUsage("bad --pick: %r" % (text,))
    parts = text.split(",")
    if len(parts) > MAX_PICKS or not all(PICK_RE.match(p) for p in parts) or len(set(parts)) != len(parts):
        raise ShotsUsage("bad --pick: %r (up to %d distinct two-digit numbers, comma separated)" % (text, MAX_PICKS))
    if any(not 1 <= int(p) <= MAX_NUMBER for p in parts):
        raise ShotsUsage("bad --pick: numbers go from 01 to %d" % MAX_NUMBER)
    return sorted(parts)


def open_session_dir(cfg, session):
    """File descriptor of captures/<session>, a real directory (never through a symlink)."""
    try:
        root = os.open(cfg.captures, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as e:
        raise S.ShotsError("captures directory %s: %s" % (cfg.captures, e.strerror)) from e
    try:
        try:
            return os.open(session, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        except OSError as e:
            why = "is not a directory" if e.errno in (errno.ELOOP, errno.ENOTDIR) else e.strerror
            raise S.ShotsError("session %s: %s" % (session, why)) from e
    finally:
        os.close(root)


def read_capture(directory, name, limit=MAX_SOURCE_BYTES):
    """Bytes of one capture file: a regular file of a sane size, opened without following symlinks."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except OSError as e:
        raise ReadError("%s: %s" % (name, "is a symlink" if e.errno == errno.ELOOP else e.strerror)) from e
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ReadError("%s: not a regular file" % name)
        if info.st_size > limit:
            raise ReadError("%s: larger than %d bytes" % (name, limit))
        with os.fdopen(fd, "rb") as f:
            fd = -1
            return f.read(limit + 1)
    finally:
        if fd >= 0:
            os.close(fd)


def check_capture(cfg, data):
    """(stripped PNG bytes, width, height) of a capture; ShotsError when it must not be taken in."""
    if len(data) < 33 or struct.unpack(">BBBBB", data[24:29]) != EXPECTED_IHDR:
        raise S.ShotsError("not an 8-bit RGB PNG as the session daemon writes")
    return S.check_png(cfg, data, S.SESSION_MAX_BYTES)


def capture_owners(directory):
    """{number: (file name, app)} from captures.json; hostile input: only well-formed items count, nothing else of
    the file is used. ReadError/ShotsError when the file cannot be read or is not a JSON document of items."""
    try:
        doc = json.loads(read_capture(directory, INDEX_NAME, MAX_INDEX_BYTES).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise S.ShotsError("%s is not valid JSON" % INDEX_NAME) from None
    items = doc.get("items") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        raise S.ShotsError("%s has no list of items" % INDEX_NAME)
    owners = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        n, name, app = item.get("n"), item.get("file"), item.get("app")
        if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= MAX_NUMBER \
                and isinstance(name, str) and isinstance(app, str):
            owners["%02d" % n] = (name, app)
    return owners


def _candidates(directory, picks):
    """({number: [file names]}, problems): the files of the picked numbers, found by listing the directory."""
    found, problems = {p: [] for p in picks}, []
    for name in sorted(os.listdir(directory)):
        m = NAME_RE.match(name)
        if m and m.group(1) in found:
            found[m.group(1)].append(name)
    for number in picks:
        if len(found[number]) != 1:
            problems.append("capture %s: %s" % (number, "not found" if not found[number] else "more than one file"))
    return {n: v[0] for n, v in found.items() if len(v) == 1}, problems


def _import_one(cfg, album, session, directory, number, name, reviewer):
    """('imported'|'skipped', text); ShotsError/ReadError mean a problem."""
    lang = NAME_RE.match(name).group(3)
    payload, width, height = check_capture(cfg, read_capture(directory, name))
    twin = album.duplicate(lang, hashlib.sha256(payload).hexdigest(), rejected_too=True)
    if twin is not None:
        return "skipped", "%s/%s: identical to %s" % (lang, name, twin["file"])
    if len(S.active(album.images, lang)) >= cfg.shots_max_per_lang:
        return "skipped", "%s/%s: %s already has %d images" % (lang, name, lang, cfg.shots_max_per_lang)
    kind = NAME_RE.match(name).group(2)
    record = album.store(lang, "session-%s" % number, payload, width, height, source="manual", status="draft",
                         reviewer=None, origin=S.SESSION_ORIGIN + session,
                         note="%s capture %s of session %s, picked by %s" % (kind, number, session, reviewer))
    return "imported", "%s/%s" % (lang, record["file"])


def cmd_session_import(cfg, args):
    app = check_app(cfg, args.app)
    if not isinstance(args.session, str) or not SESSION_RE.match(args.session):
        raise ShotsUsage("bad session id: %r" % (args.session,))
    reviewer = check_reviewer(args.reviewer)
    picks = parse_picks(args.pick)
    directory = open_session_dir(cfg, args.session)
    counts = {"imported": 0, "skipped": 0}
    try:
        names, problems = _candidates(directory, picks)
        try:
            owners = capture_owners(directory)
        except (S.ShotsError, ReadError) as e:
            owners, names = {}, {}
            problems.append("%s: %s" % (INDEX_NAME, e))
        for number in sorted(names):
            if owners.get(number) != (names[number], app):
                problems.append("capture %s: not recorded for %s" % (number, app))
                del names[number]
        with S.locked(cfg, app) as album:
            try:
                for number in picks:
                    if number not in names:
                        continue
                    try:
                        kind, text = _import_one(cfg, album, args.session, directory, number, names[number], reviewer)
                    except (S.ShotsError, ReadError) as e:
                        problems.append("%s: %s" % (names[number], e))
                        continue
                    counts[kind] += 1
                    print("%s %s" % (kind, text))
            finally:
                if counts["imported"]:
                    album.save()
    finally:
        os.close(directory)
    for line in problems:
        print("problem %s" % line)
    print("session %s: imported %d, skipped %d, problems %d" % (args.session, counts["imported"], counts["skipped"],
                                                                len(problems)))
    return 1 if problems else 0
