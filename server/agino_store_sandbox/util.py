# SPDX-License-Identifier: GPL-3.0-or-later
"""Small shared helpers: errors, logging, text sanitising, time."""

import datetime
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


def clean(text, limit):
    """Guest-provided text made safe for reports: control characters dropped, length capped."""
    if not isinstance(text, str):
        text = str(text)
    text = "".join(c if c == " " or (c.isprintable() and c not in "  ") else "?" for c in text)
    return text if len(text) <= limit else text[:limit - 1] + "…"
