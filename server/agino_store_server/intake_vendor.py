# SPDX-License-Identifier: GPL-3.0-or-later
"""AI intake of a vendor application, step 2: the trial run (docs/server.md section 15.8).

A delivery with vendor.yaml has no recipe to replay in a virtual machine. What can be verified is that the
tracking file really works: the server runs it once with the same discovery, fetcher and checks as `track` (no
state recorded) and keeps the result in the task as `vendor_check`. The deb the server fetched itself is the one
that is adopted; the deb the agent tested is only compared with it.
"""

import tempfile
import urllib.parse

from . import checks, debinfo, fetch, trackers
from .intake_delivery import DeliveryError, approved_hosts
from .state import StateError

TRIAL_ERRORS = (trackers.TrackError, fetch.FetchError, debinfo.DebError, checks.CheckError, StateError, OSError,
                ValueError)
MAX_CATEGORIES = 12
MAX_URL = 2048
FLAG_CATEGORIES = (("writes /etc/apt", "writes /etc/apt"), ("systemd unit", "systemd unit"), ("cron ", "cron"),
                   ("udev rule", "udev rule"), ("setuid ", "setuid"), ("setgid ", "setgid"),
                   ("writes /", "writes system configuration"), ("dpkg triggers", "dpkg triggers"))


def flag_category(flag):
    """The kind of an inspect flag without its file name: 'setuid usr/bin/x' is 'setuid'. Script flags
    ('postinst mentions apt-key') are already general."""
    for prefix, label in FLAG_CATEGORIES:
        if flag.startswith(prefix):
            return label
    return flag[:80]


def _hosts(cfg, record, vendor):
    allowed = approved_hosts(cfg, record)
    return [{"host": h, "approved": h.lower() in allowed} for h in vendor["allowed_hosts"]]


def _failure_text(error):
    return ("The server tried your vendor.yaml and could not get the vendor's deb with it: %s. The tracking file "
            "must make the server find the newest deb by itself (and later versions too) using only the hosts you "
            "may use. Fix vendor.yaml and deliver again." % debinfo.clean_text(error))


def run(cfg, st, checked, record, ai_deb):
    """The `vendor_check` of a validated vendor delivery. `ai_deb` is {name, sha256, size} of the agent's own deb or
    None. DeliveryError (with the text for the agent) when the tracking file does not work."""
    target = trackers.Target(checked.app_id, checked.app_id, checked.tracking)
    try:
        with tempfile.TemporaryDirectory(prefix="agino-vtrial-") as work:
            trial = trackers.trial_run(cfg, st, target, work)
    except TRIAL_ERRORS as e:
        raise DeliveryError(_failure_text(e)) from e
    dl, flags = trial.download, list(trial.inspected.get("flags") or [])
    categories = sorted({flag_category(f) for f in flags})[:MAX_CATEGORIES]
    return {
        "version": trial.version, "sha256": dl.sha256, "size": dl.size, "url": trial.url[:MAX_URL],
        "filename": dl.filename, "flags": {"count": len(flags), "categories": categories},
        "deps_ok": trial.deps.get("ok") is True,
        "matches_ai_deb": None if ai_deb is None else ai_deb["sha256"] == dl.sha256,
        "hosts": _hosts(cfg, record, checked.tracking),
    }


def page_host(url):
    """Lower-case host of a URL, or None."""
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower() or None
    except ValueError:
        return None
