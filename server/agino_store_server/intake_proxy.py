# SPDX-License-Identifier: GPL-3.0-or-later
"""The extra allow list of the builder's forwarding proxy (contract ai-intake, coordinator addendum).

Every intake-step run writes the `allow_hosts` of the tasks whose AI task is running or about to run
(ready, running, verifying), without duplicates, one host name per line, to [intake] proxy_allow_file; an empty
file when there are none. The proxy merges it with [upstream] allowed_hosts. The file is replaced atomically
(a temporary file in the same directory, then rename), so the proxy never reads half a list. When the AI task of
a task ends, the next run drops its hosts again.
"""

import os
import re
import tempfile

from . import debinfo, intake

HOST_RE = re.compile(r"\A[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?\Z")


def wanted_hosts(st, rep=None):
    """(sorted unique host names, dropped values) of the tasks that need the proxy now."""
    hosts, dropped = set(), []
    for rec in st.intakes():
        if rec.get("status") not in intake.PROXY_STATUSES:
            continue
        for host in rec.get("allow_hosts") or []:
            if isinstance(host, str) and HOST_RE.match(host):
                hosts.add(host)
            else:
                dropped.append(host)
    return sorted(hosts), dropped


def render(hosts):
    return "".join("%s\n" % h for h in hosts)


def write_atomic(path, text):
    """Replace `path` with `text` (mode 0644); nothing is touched when it already has exactly that content."""
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return False
    except OSError:
        pass
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".allow-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise
    return True


def update(cfg, st, rep):
    """Write the allow file; problems are reported (and counted as a failure) but never raised."""
    hosts, dropped = wanted_hosts(st)
    for value in dropped:
        rep.add("proxy allow list: dropped an invalid host name: %s" % debinfo.clean_text(repr(value))[:100])
    try:
        if write_atomic(cfg.intake_proxy_allow_file, render(hosts)):
            rep.add("proxy allow list: %d extra host(s)" % len(hosts))
    except OSError as e:
        rep.fail("cannot write %s: %s" % (cfg.intake_proxy_allow_file, e.strerror))
