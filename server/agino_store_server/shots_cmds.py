# SPDX-License-Identifier: GPL-3.0-or-later
"""shots-ingest, shots-review, shots-remove and the argument checks shared by the shots-* commands
(docs/server.md section 12). They write only below paths.screenshots, under the per-app lock."""

import errno
import os
import re
import stat

from . import shots_store as S

STAGED_RE = re.compile(r"\A[A-Za-z0-9_-]{8,64}\.png\Z")
CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f]")
MAX_SOURCE_BYTES = 4 << 20
INGEST_SOURCES = ("upload", "manual")


class ShotsUsage(ValueError):
    """A bad argument (exit code 2)."""


def check_app(cfg, app):
    if not isinstance(app, str) or not S.APP_RE.match(app):
        raise ShotsUsage("bad app id: %r" % (app,))
    path = os.path.join(cfg.content, "apps", app, "app.yaml")
    if os.path.islink(path) or not os.path.isfile(path):
        raise ShotsUsage("unknown app %s" % app)
    return app


def check_choice(value, allowed, what):
    if value not in allowed:
        raise ShotsUsage("bad %s: %r (one of %s)" % (what, value, ", ".join(allowed)))
    return value


def check_match(value, regex, what):
    if not isinstance(value, str) or not regex.match(value):
        raise ShotsUsage("bad %s: %r" % (what, value))
    return value


def check_reviewer(value):
    return check_match(value, S.REVIEWER_RE, "reviewer")


def check_note(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > S.MAX_TEXT or CONTROL_RE.search(value) or value.startswith("-"):
        raise ShotsUsage("bad note: at most %d characters, no control characters, must not start with '-'" % S.MAX_TEXT)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as e:
        raise ShotsUsage("bad note: not valid UTF-8") from e
    return value


def check_target(args):
    """(lang, file) of an existing-image argument pair."""
    return (check_choice(args.lang, S.LANGS, "language"),
            check_match(args.file, S.FILE_RE, "file (expected NN-scene.png)"))


# staging directory

def read_staged(cfg, name, limit=MAX_SOURCE_BYTES):
    """The bytes of staging/<name>: a regular file of at most `limit` bytes opened without following symlinks."""
    try:
        directory = os.open(cfg.shots_staging, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as e:
        raise S.ShotsError("staging directory %s: %s" % (cfg.shots_staging, e.strerror)) from e
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except OSError as e:
            raise S.ShotsError("staged file %s: %s" % (name, "is a symlink" if e.errno == errno.ELOOP else e.strerror)) from e
        with os.fdopen(fd, "rb") as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise S.ShotsError("staged file %s is not a regular file" % name)
            if info.st_size > limit:
                raise S.ShotsError("staged file %s is larger than %d bytes" % (name, limit))
            return f.read(limit + 1)
    finally:
        os.close(directory)


def delete_staged(cfg, name):
    directory = os.open(cfg.shots_staging, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.unlink(name, dir_fd=directory)
    finally:
        os.close(directory)


# commands

def cmd_ingest(cfg, args):
    app = check_app(cfg, args.app)
    lang = check_choice(args.lang, S.LANGS, "language")
    scene = check_match(args.scene, S.SCENE_RE, "scene")
    name = check_match(args.file, STAGED_RE, "staging file name")
    source = check_choice(args.source, INGEST_SOURCES, "source")
    status = check_choice(args.status, ("draft", "approved"), "status")
    reviewer = check_reviewer(args.reviewer)
    replace = check_match(args.replace, S.FILE_RE, "--replace file") if args.replace is not None else None
    raw = read_staged(cfg, name)
    payload, width, height = S.check_png(cfg, raw)
    with S.locked(cfg, app) as album:
        record = album.store(lang, scene, payload, width, height, source=source, status=status,
                             reviewer=reviewer, origin="", replace=replace)
        album.save()
    print("stored %s/%s (%s, %s)" % (lang, record["file"], source, status))
    try:
        delete_staged(cfg, name)
    except OSError as e:
        raise S.ShotsError("stored, but cannot delete the staged file %s: %s" % (name, e.strerror)) from e
    return 0


def cmd_review(cfg, args):
    app = check_app(cfg, args.app)
    lang, file = check_target(args)
    status = check_choice(args.status, S.STATUSES, "status")
    reviewer = check_reviewer(args.reviewer)
    note = check_note(args.note)
    with S.locked(cfg, app) as album:
        album.set_status(lang, file, status, reviewer, note)
        album.save()
    print("%s %s/%s" % (status, lang, file))
    return 0


def cmd_remove(cfg, args):
    app = check_app(cfg, args.app)
    lang, file = check_target(args)
    reviewer = check_reviewer(args.reviewer)
    with S.locked(cfg, app) as album:
        album.remove(lang, file)
        album.save()
    print("removed %s/%s (kept in .prev, by %s)" % (lang, file, reviewer))
    return 0

