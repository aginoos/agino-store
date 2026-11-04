# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings shared by config.py and the content validation for upstream tracking and builds
(docs/server.md section 14): the allowed upstream hosts and the global build limits."""

import ipaddress
import re
from dataclasses import dataclass

DEFAULT_UPSTREAM_HOSTS = ("github.com", "gitlab.com", "codeberg.org", "invent.kde.org", "gitlab.gnome.org")
LIMIT_KEYS = ("cpu_minutes", "memory_gib", "disk_gib")
HOST_RE = re.compile(r"\A[a-z0-9]([a-z0-9.-]*[a-z0-9])?\Z")


@dataclass(frozen=True)
class Limits:
    cpu_minutes: int = 90
    memory_gib: int = 8
    disk_gib: int = 20

    def as_dict(self):
        return {key: getattr(self, key) for key in LIMIT_KEYS}


DEFAULT_LIMITS = Limits()


@dataclass(frozen=True)
class Policy:
    """What the content validation needs from server.conf."""
    allowed_hosts: tuple = DEFAULT_UPSTREAM_HOSTS
    limits: Limits = DEFAULT_LIMITS


def parse_limits(raw):
    """'cpu_minutes=90 memory_gib=8 disk_gib=20' -> Limits (missing keys keep their default). ValueError on junk."""
    values = DEFAULT_LIMITS.as_dict()
    for token in (raw or "").replace(",", " ").split():
        key, sep, number = token.partition("=")
        if not sep or key not in LIMIT_KEYS or not number.isdigit() or not 1 <= int(number) <= 1_000_000:
            raise ValueError("bad limits entry %r (use %s=<positive number>)" % (token, "|".join(LIMIT_KEYS)))
        values[key] = int(number)
    return Limits(**values)


def _is_ip(host):
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def parse_hosts(raw, allow_insecure=False):
    """Space separated host names -> tuple of lower-case names. ValueError for anything that is not a public name."""
    hosts = tuple(h.lower() for h in (raw or "").split())
    if not hosts:
        raise ValueError("at least one host is needed")
    for host in hosts:
        if not HOST_RE.match(host) or len(host) > 253:
            raise ValueError("bad host name %r" % host)
        if allow_insecure:
            continue
        if _is_ip(host):
            raise ValueError("IP literals are not allowed: %r" % host)
        labels = host.split(".")
        if len(labels) < 2 or labels[-1] == "localhost" or labels[-1].isdigit():
            raise ValueError("must be a fully qualified public name: %r" % host)
    return hosts
