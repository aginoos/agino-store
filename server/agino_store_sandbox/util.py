# SPDX-License-Identifier: GPL-3.0-or-later
"""Small shared helpers: errors, logging, text sanitising, time."""

import datetime
import os
import re
import sys

ID_RE = re.compile(r"\A[a-z0-9][a-z0-9.+-]*\Z")
VERSION_RE = re.compile(r"\A(?:[0-9]+:)?[0-9][A-Za-z0-9.+~-]*\Z")
SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
DESKTOP_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+-]*\.desktop\Z")
DESKTOP_PATH_RE = re.compile(r"\A/usr/share/applications/(?:[A-Za-z0-9._+-]+/)*[A-Za-z0-9][A-Za-z0-9._+-]*\.desktop\Z")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class SandboxError(Exception):
    """Anything that makes a run impossible to judge (VM, guest agent, bad config)."""


class InfraError(SandboxError):
    """The sandbox itself failed (VM did not boot, guest agent timed out): retry later."""


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(TIME_FORMAT)


def log(msg):
    print("%s %s" % (now(), msg), file=sys.stderr, flush=True)


def give_group(target, gid):
    """Best-effort chgrp of a path or an open file descriptor; returns None or the OSError.

    The daemon is not a member of the platform group: directories below /run/agino-session are setgid and
    hand that group to everything created in them, so the chown only matters when the group differs, and
    a refusal (EPERM) is not fatal.
    """
    try:
        info = os.fstat(target) if isinstance(target, int) else os.lstat(target)
        if info.st_gid == gid:
            return None
        if isinstance(target, int):
            os.fchown(target, -1, gid)
        else:
            os.chown(target, -1, gid)
    except OSError as e:
        return e
    return None


def clean(text, limit):
    """Guest-provided text made safe for reports: control characters dropped, length capped."""
    if not isinstance(text, str):
        text = str(text)
    text = "".join(c if c == " " or (c.isprintable() and c not in "  ") else "?" for c in text)
    return text if len(text) <= limit else text[:limit - 1] + "…"
