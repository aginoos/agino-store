# SPDX-License-Identifier: GPL-3.0-or-later
"""Shots-only sandbox jobs (docs/server.md section 10; screenshot pipeline contract section 4).

`shots-request APP` leaves a request in the state directory. Every open request becomes a queue job
with "mode": "shots" (vendor apps: the newest pending or approved candidate; milkyway apps: the
version in the cached milkyway index) until the sandbox host has written shots_out/<job>/shots.json.
"""

import datetime
import functools
import hashlib
import os
import stat

from . import catalog

SESSION_TTL = datetime.timedelta(hours=2)
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def milkyway_base(app, package, version):
    """Job name of a milkyway app: <app>_<first 12 hex of sha256("milkyway:" + package + "=" + version)>."""
    return "%s_%s" % (app, hashlib.sha256(("milkyway:%s=%s" % (package, version)).encode()).hexdigest()[:12])


def newest_candidate(st, app):
    """The newest pending or approved candidate of app that has a downloaded file, or None."""
    records = [r for r in st.candidates(app=app) if r.get("status") in ("pending", "approved")
               and r.get("file") and isinstance(r.get("sha256"), str) and isinstance(r.get("version"), str)]
    if not records:
        return None
    return functools.reduce(
        lambda a, b: b if catalog.compare_versions(b["version"], a["version"]) > 0 else a, records)


def is_done(shots_out, base):
    """True if shots_out/<base>/shots.json is a regular file. Entries are opened without following
    symlinks; a missing or unreadable shots_out closes nothing."""
    try:
        top = os.open(shots_out, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return False
    try:
        sub = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=top)
        try:
            fd = os.open("shots.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=sub)
            try:
                return stat.S_ISREG(os.fstat(fd).st_mode)
            finally:
                os.close(fd)
        finally:
            os.close(sub)
    except OSError:
        return False
    finally:
        os.close(top)


def session_expiry(now):
    """The "expires" stamp of a session request made at now (a UTC datetime)."""
    return (now + SESSION_TTL).strftime(TIME_FORMAT)


def is_session(request):
    return request.get("session") is True


def session_expired(request, now):
    """True if a session request has no readable "expires" stamp or it is not after now."""
    try:
        expires = datetime.datetime.strptime(request.get("expires"), TIME_FORMAT).replace(
            tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return True
    return expires <= now
