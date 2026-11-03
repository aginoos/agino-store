# SPDX-License-Identifier: GPL-3.0-or-later
"""Configuration file (section [sandbox], see server/sandbox.conf.example)."""

import configparser
import dataclasses
import re

from .util import SandboxError

DEFAULT_PATH = "/etc/agino-store-sandbox/sandbox.conf"
MAX_SLOTS = 4

DEFAULTS = {
    "queue": "/srv/store/sandbox/queue",
    "state": "/srv/store/sandbox/state",
    "run": "/srv/store/sandbox/run",
    "shot_dir": "/srv/store/sandbox/shots",
    "shots": "on",
    "shots_out": "/srv/store/sandbox/shots-out",
    "corner_radius": "8",
    "screen": "1920x1080",
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


SESSION_DEFAULTS = {
    "runtime_dir": "/run/agino-session",
    "captures": "/srv/store/sandbox/captures",
    "web_group": "agino-admin",
    "max_sessions": "2",
    "run_minutes": "30",
    "idle_minutes": "10",
    "total_minutes": "60",
    "install_minutes": "15",
    "overlay_cap_gib": "8",
    "min_free_gib": "20",
    "max_captures": "40",
    "retention_hours": "24",
    "traffic_cap_gib": "2",
}


@dataclasses.dataclass(frozen=True)
class SessionConfig:
    """The [session] section (interactive screenshot sessions, docs/sandbox.md section 9)."""
    runtime_dir: str = SESSION_DEFAULTS["runtime_dir"]
    captures: str = SESSION_DEFAULTS["captures"]
    web_group: str = SESSION_DEFAULTS["web_group"]
    max_sessions: int = 2
    run_minutes: int = 30
    idle_minutes: int = 10
    total_minutes: int = 60
    install_minutes: int = 15
    overlay_cap_gib: int = 8
    min_free_gib: int = 20
    max_captures: int = 40
    retention_hours: int = 24
    traffic_cap_gib: int = 2


SESSION_INT_RANGES = {"max_sessions": (1, 4), "run_minutes": (1, 120), "idle_minutes": (1, 120),
                      "total_minutes": (1, 240), "install_minutes": (1, 120), "overlay_cap_gib": (1, 512),
                      "min_free_gib": (0, 4096), "max_captures": (1, 99), "retention_hours": (1, 24 * 30),
                      "traffic_cap_gib": (1, 1024)}


def session_from_dict(values):
    merged = dict(SESSION_DEFAULTS, **values)
    unknown = set(merged) - set(SESSION_DEFAULTS)
    if unknown:
        raise SandboxError("unknown [session] keys: %s" % ", ".join(sorted(unknown)))
    out = {k: merged[k] for k in ("runtime_dir", "captures", "web_group")}
    for key, (low, high) in SESSION_INT_RANGES.items():
        try:
            n = int(merged[key])
        except ValueError:
            raise SandboxError("config [session] %s must be an integer" % key) from None
        if not low <= n <= high:
            raise SandboxError("config [session] %s must be between %d and %d" % (key, low, high))
        out[key] = n
    for key in ("runtime_dir", "captures"):
        if not out[key].startswith("/"):
            raise SandboxError("config [session] %s must be an absolute path" % key)
    return SessionConfig(**out)


@dataclasses.dataclass(frozen=True)
class Config:
    queue: str
    state: str
    run: str
    shot_dir: str
    shots: bool
    shots_out: str
    corner_radius: int
    screen: tuple
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
    session: SessionConfig = SessionConfig()


def _int(values, key, low, high):
    try:
        n = int(values[key])
    except ValueError:
        raise SandboxError("config %s must be an integer" % key) from None
    if not low <= n <= high:
        raise SandboxError("config %s must be between %d and %d" % (key, low, high))
    return n


def _screen(text):
    m = re.match(r"\A([0-9]{3,4})x([0-9]{3,4})\Z", text)
    if not m or not (800 <= int(m.group(1)) <= 3840 and 600 <= int(m.group(2)) <= 2160):
        raise SandboxError("config screen must look like 1920x1080 (800..3840 x 600..2160)")
    return int(m.group(1)), int(m.group(2))


def from_dict(values, session=None):
    merged = dict(DEFAULTS, **values)
    if merged["shots"].startswith("/"):  # older configs used `shots` for the check screenshot directory
        merged["shot_dir"] = merged["shots"]
        merged["shots"] = "on"
    if merged["shots"] not in ("on", "off"):
        raise SandboxError("config shots must be on or off")
    if merged["qemu_sandbox"] not in ("auto", "on", "off"):
        raise SandboxError("config qemu_sandbox must be auto, on or off")
    ints = {"slots": _int(merged, "slots", 1, MAX_SLOTS), "memory_mb": _int(merged, "memory_mb", 512, 65536),
            "cpus": _int(merged, "cpus", 1, 64),
            "corner_radius": _int(merged, "corner_radius", 0, 64), "screen": _screen(merged["screen"]),
            "shots": merged["shots"] == "on"}
    strs = {k: v for k, v in merged.items() if k not in ints}
    unknown = set(strs) - {f.name for f in dataclasses.fields(Config)}
    if unknown:
        raise SandboxError("unknown config keys: %s" % ", ".join(sorted(unknown)))
    return Config(**strs, **ints, session=session_from_dict(session or {}))


def load(path=DEFAULT_PATH):
    parser = configparser.ConfigParser(inline_comment_prefixes=("#",), interpolation=None)
    try:
        with open(path, encoding="utf-8") as f:
            parser.read_file(f)
    except OSError as e:
        raise SandboxError("cannot read config %s: %s" % (path, e.strerror)) from None
    return from_dict(dict(parser["sandbox"]) if parser.has_section("sandbox") else {},
                     dict(parser["session"]) if parser.has_section("session") else None)
