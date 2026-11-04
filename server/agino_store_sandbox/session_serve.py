# SPDX-License-Identifier: GPL-3.0-or-later
"""The `session` subcommands: serve (the daemon of one VM slot vm0, vm1, ...), status (debugging) and cleanup."""

import grp
import json
import os
import re
import secrets
import signal
import stat
import threading
import time

from . import session as S
from .session_capture import NAME_RE
from .session_vm import PRIVATE_NAMES
from .session_web import WebServer
from .util import SandboxError, give_group, log

FAILED_LINGER = 60
CLOSE_GRACE = 0.5  # lets the reply to `end` reach the platform before the sockets are shut
STATUS_FILE = "status.json"
STATUS_EVERY = 5.0
STALE_NAMES = ("web.sock", "token.once", "session.id")
CAPTURE_TMP_RE = re.compile(r"\A\.[A-Za-z0-9._-]+\.tmp\Z")
DIR_RE = re.compile(r"\A[0-9a-f]{16}\Z")


def _group_id(name):
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return os.getegid()


def _write(path, data, mode, exclusive=False, gid=None):
    """Create path with `mode` (never wider, whatever the umask) and `data`; atomically replaced unless exclusive."""
    target = path if exclusive else path + ".tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(target, flags, mode)
    with os.fdopen(fd, "wb") as f:
        os.fchmod(f.fileno(), mode)
        if gid is not None:
            err = give_group(f.fileno(), gid)
            if err is not None:
                log("cannot give %s to group %d: %s" % (path, gid, err.strerror))
        f.write(data)
    if not exclusive:
        os.replace(target, path)


def prepare_dirs(scfg, slot):
    """(slot dir, private dir): fresh, owned by us, 0750 (group web_group) and 0700.

    The unit runs with RestrictSUIDSGID and without the web group: no chmod here may carry or clear the setgid
    bit and the chown is only a fallback. The group comes from the setgid /run/agino-session (tmpfiles.d, 2750):
    the slot directory takes it at mkdir time (like the captures directory), and the files in it inherit it.
    """
    gid = _group_id(scfg.web_group)
    os.makedirs(scfg.runtime_dir, mode=0o750, exist_ok=True)
    app_dir = os.path.join(scfg.runtime_dir, slot)
    private = os.path.join(app_dir, "private")
    old = os.umask(0o027)  # the slot directory must come out 0750 without a chmod (that would drop the setgid bit)
    try:
        os.makedirs(app_dir, mode=0o750, exist_ok=True)
    finally:
        os.umask(old)
    os.makedirs(private, mode=0o700, exist_ok=True)
    _ensure_mode(private, 0o700)
    _ensure_mode(app_dir, 0o750)
    err = give_group(app_dir, gid)
    if err is not None:
        log("cannot give %s to group %s: %s" % (app_dir, scfg.web_group, err.strerror))
    for name in STALE_NAMES:
        _unlink(os.path.join(app_dir, name))
    _unlink(os.path.join(private, STATUS_FILE))
    return app_dir, private


def _ensure_mode(path, mode):
    """chmod only when the permission bits differ, so a setgid bit inherited at mkdir time is not cleared."""
    if stat.S_IMODE(os.lstat(path).st_mode) & 0o777 != mode:
        os.chmod(path, mode)


def _unlink(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        log("cannot remove %s: %s" % (path, e.strerror))


def remove_runtime(app_dir, private):
    """Remove exactly the files the daemon creates, then the two (then empty) directories."""
    for name in STALE_NAMES:
        _unlink(os.path.join(app_dir, name))
    for name in PRIVATE_NAMES + (STATUS_FILE, STATUS_FILE + ".tmp"):
        _unlink(os.path.join(private, name))
    for directory in (private, app_dir):
        try:
            os.rmdir(directory)
        except OSError as e:
            log("cannot remove %s: %s" % (directory, e.strerror))


class StatusPublisher:
    """Writes private/status.json when the state changed and at most every STATUS_EVERY seconds otherwise."""

    def __init__(self, path):
        self.path, self.last, self.state = path, 0.0, None

    def __call__(self, session):
        now = time.monotonic()
        status = session.status()
        if status["state"] == self.state and now - self.last < STATUS_EVERY:
            return
        self.last, self.state = now, status["state"]
        doc = dict(status, written=time.time())
        try:
            _write(self.path, (json.dumps(doc) + "\n").encode("utf-8"), 0o600)
        except OSError:
            pass


def serve(cfg, slot, deps=None, linger=FAILED_LINGER):
    """Run the session of VM slot `slot` (vm0, vm1, ...) in this process; returns the exit code."""
    scfg = cfg.session
    if not S.SLOT_RE.match(slot):
        raise SandboxError("bad slot name")
    os.umask(0o077)
    session_id = secrets.token_hex(8)
    token = secrets.token_bytes(32)
    app_dir, private = prepare_dirs(scfg, slot)
    gid = _group_id(scfg.web_group)
    publisher = StatusPublisher(os.path.join(private, STATUS_FILE))
    holder = {}
    session = S.Session(cfg, slot, session_id, private, deps, publish=publisher,
                        drop_vnc=lambda: holder["web"].drop_vnc() if "web" in holder else None)
    try:
        session.store.create_directory()
        web = WebServer(session, os.path.join(app_dir, "web.sock"), token, scfg.web_group)
        holder["web"] = web
        web.bind()
        threading.Thread(target=web.serve, daemon=True).start()
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: session.request_end("stopped"))
        _write(os.path.join(app_dir, "session.id"), (session_id + "\n").encode("ascii"), 0o640, gid=gid)
        _write(os.path.join(app_dir, "token.once"), token.hex().encode("ascii") + b"\n", 0o600, exclusive=True)
        publisher(session)
        threading.Thread(target=session.watch, daemon=True).start()
        log("session %s on %s started" % (session_id, slot))
        session.run()
        deadline = time.monotonic() + linger
        while session.state == "failed" and time.monotonic() < deadline:  # let the platform read the error
            time.sleep(0.5)
    finally:
        session.request_end("")
        session.finished.set()
        time.sleep(CLOSE_GRACE)
        if "web" in holder:
            holder["web"].close()
        remove_runtime(app_dir, private)
    log("session %s finished" % session_id)
    return 0


def status(cfg, slot):
    """The last status the daemon published, with the clocks advanced to now."""
    if not S.SLOT_RE.match(slot):
        raise SandboxError("bad slot name")
    path = os.path.join(cfg.session.runtime_dir, slot, "private", STATUS_FILE)
    try:
        with open(path, "rb") as f:
            doc = json.loads(f.read(1 << 16).decode("utf-8"))
    except (OSError, ValueError):
        raise SandboxError("no running session on %s" % slot) from None
    age = max(0, int(time.time() - doc.pop("written", time.time())))
    if doc.get("state") in S.IDLE_STATES:
        doc["idle_s"] = doc.get("idle_s", 0) + age
    doc["remaining_s"] = max(0, doc.get("remaining_s", 0) - age)
    return json.dumps(doc, indent=1, sort_keys=True)


def cleanup(cfg, now=None):
    """Remove captures directories older than retention_hours; only explicit names below the captures root."""
    root, limit = cfg.session.captures, (now or time.time()) - cfg.session.retention_hours * 3600
    try:
        names = sorted(n for n in os.listdir(root) if DIR_RE.match(n))
    except OSError as e:
        raise SandboxError("cannot list %s: %s" % (root, e.strerror)) from None
    removed = []
    for name in names:
        path = os.path.join(root, name)
        try:
            info = os.lstat(path)
            if not os.path.isdir(path) or os.path.islink(path) or info.st_mtime >= limit:
                continue
            for entry in sorted(os.listdir(path)):
                if NAME_RE.match(entry) or entry == "captures.json" or CAPTURE_TMP_RE.match(entry):
                    os.unlink(os.path.join(path, entry))
            os.rmdir(path)
            removed.append(name)
        except OSError as e:
            log("cleanup: cannot remove %s: %s" % (name, e.strerror))
    return removed
