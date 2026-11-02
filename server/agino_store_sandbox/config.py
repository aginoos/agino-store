# SPDX-License-Identifier: GPL-3.0-or-later
"""Configuration file (section [sandbox], see server/sandbox.conf.example)."""

import configparser
import dataclasses

from .util import SandboxError

DEFAULT_PATH = "/etc/agino-store-sandbox/sandbox.conf"
MAX_SLOTS = 4

DEFAULTS = {
    "queue": "/srv/store/sandbox/queue",
    "state": "/srv/store/sandbox/state",
    "run": "/srv/store/sandbox/run",
    "shots": "/srv/store/sandbox/shots",
    "reports": "/srv/store/reports",
    "base": "/srv/store/sandbox/base/current.qcow2",
    "ovmf_code": "/usr/share/OVMF/OVMF_CODE_4M.fd",
    "base_vars": "/srv/store/sandbox/base/current.vars",
    "key": "/etc/agino-store-sandbox/report.pem",
    "qemu": "qemu-system-x86_64",
    "qemu_img": "qemu-img",
    "qemu_sandbox": "auto",
    "slots": "4",
    "memory_mb": "4096",
    "cpus": "4",
}


@dataclasses.dataclass(frozen=True)
class Config:
    queue: str
    state: str
    run: str
    shots: str
    reports: str
    base: str
    ovmf_code: str
    base_vars: str
    key: str
    qemu: str
    qemu_img: str
    qemu_sandbox: str
    slots: int
    memory_mb: int
    cpus: int


def _int(values, key, low, high):
    try:
        n = int(values[key])
    except ValueError:
        raise SandboxError("config %s must be an integer" % key) from None
    if not low <= n <= high:
        raise SandboxError("config %s must be between %d and %d" % (key, low, high))
    return n


def from_dict(values):
    merged = dict(DEFAULTS, **values)
    if merged["qemu_sandbox"] not in ("auto", "on", "off"):
        raise SandboxError("config qemu_sandbox must be auto, on or off")
    ints = {"slots": _int(merged, "slots", 1, MAX_SLOTS), "memory_mb": _int(merged, "memory_mb", 512, 65536),
            "cpus": _int(merged, "cpus", 1, 64)}
    strs = {k: v for k, v in merged.items() if k not in ints}
    unknown = set(strs) - {f.name for f in dataclasses.fields(Config)}
    if unknown:
        raise SandboxError("unknown config keys: %s" % ", ".join(sorted(unknown)))
    return Config(**strs, **ints)


def load(path=DEFAULT_PATH):
    parser = configparser.ConfigParser(inline_comment_prefixes=("#",), interpolation=None)
    try:
        with open(path, encoding="utf-8") as f:
            parser.read_file(f)
    except OSError as e:
        raise SandboxError("cannot read config %s: %s" % (path, e.strerror)) from None
    return from_dict(dict(parser["sandbox"]) if parser.has_section("sandbox") else {})
