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
    "run_minutes": "30",  # no longer used since the session VM is reused (docs/sandbox.md); kept so old configs load
    "idle_minutes": "10",
    "total_minutes": "120",
    "install_minutes": "15",
    "overlay_cap_gib": "12",
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
    total_minutes: int = 120
    install_minutes: int = 15
    overlay_cap_gib: int = 12
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


BUILDER_DEFAULTS = {
    "base": "/srv/store/sandbox/base/builder.qcow2",
    "base_vars": "/srv/store/sandbox/base/builder.vars",
    # The apt line after "deb ": [options] URL SUITE COMPONENT...; the only place the build VM may fetch from.
    "apt_source": "http://51.38.63.36:8080/ milkyway main",
    "queue": "/srv/store/build-queue",
    "out": "/srv/store/build-out",
    "max_concurrent": "1",
    "limits": "cpu_minutes=90 memory_gib=8 disk_gib=20",
    "max_source_mib": "512",
    "max_artifact_mib": "1024",
    "max_log_mib": "8",
}
BUILDER_INT_RANGES = {"max_concurrent": (1, 4), "max_source_mib": (1, 4096), "max_artifact_mib": (1, 8192),
                      "max_log_mib": (1, 256)}
LIMIT_RANGES = {"cpu_minutes": (1, 24 * 60), "memory_gib": (1, 64), "disk_gib": (1, 512)}
APT_SOURCE_RE = re.compile(r"\A(?:\[[A-Za-z0-9=,./:_+-]{1,200}\] )?https?://[A-Za-z0-9.:/_-]{1,200} "
                           r"[A-Za-z0-9._-]{1,64}(?: [a-z0-9-]{1,32}){1,8}\Z")


@dataclasses.dataclass(frozen=True)
class BuildLimits:
    """Per-task ceilings; a recipe may only lower them (docs/build-tasks.md section 7.1)."""
    cpu_minutes: int = 90
    memory_gib: int = 8
    disk_gib: int = 20


@dataclasses.dataclass(frozen=True)
class BuilderConfig:
    """The [builder] section (build VMs, docs/sandbox.md section 10)."""
    base: str = BUILDER_DEFAULTS["base"]
    base_vars: str = BUILDER_DEFAULTS["base_vars"]
    apt_source: str = BUILDER_DEFAULTS["apt_source"]
    queue: str = BUILDER_DEFAULTS["queue"]
    out: str = BUILDER_DEFAULTS["out"]
    max_concurrent: int = 1
    limits: BuildLimits = BuildLimits()
    max_source_mib: int = 512
    max_artifact_mib: int = 1024
    max_log_mib: int = 8


def parse_limits(text):
    """`cpu_minutes=90 memory_gib=8 disk_gib=20` (any subset, missing keys keep the defaults)."""
    values = {}
    for item in text.split():
        key, sep, raw = item.partition("=")
        if not sep or key not in LIMIT_RANGES or key in values or not raw.isdigit():
            raise SandboxError("config [builder] limits: bad item %r" % item[:40])
        low, high = LIMIT_RANGES[key]
        if not low <= int(raw) <= high:
            raise SandboxError("config [builder] limits: %s must be between %d and %d" % (key, low, high))
        values[key] = int(raw)
    return BuildLimits(**values)


def builder_from_dict(values):
    merged = dict(BUILDER_DEFAULTS, **values)
    unknown = set(merged) - set(BUILDER_DEFAULTS)
    if unknown:
        raise SandboxError("unknown [builder] keys: %s" % ", ".join(sorted(unknown)))
    out = {k: merged[k] for k in ("base", "base_vars", "apt_source", "queue", "out")}
    for key, (low, high) in BUILDER_INT_RANGES.items():
        try:
            n = int(merged[key])
        except ValueError:
            raise SandboxError("config [builder] %s must be an integer" % key) from None
        if not low <= n <= high:
            raise SandboxError("config [builder] %s must be between %d and %d" % (key, low, high))
        out[key] = n
    for key in ("base", "base_vars", "queue", "out"):
        if not out[key].startswith("/") or "," in out[key]:
            raise SandboxError("config [builder] %s must be an absolute path without a comma" % key)
    if not APT_SOURCE_RE.match(out["apt_source"]):
        raise SandboxError("config [builder] apt_source must look like '[options] URL SUITE COMPONENT...'")
    return BuilderConfig(limits=parse_limits(merged["limits"]), **out)


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
    builder: BuilderConfig = BuilderConfig()


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


def from_dict(values, session=None, builder=None):
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
    return Config(**strs, **ints, session=session_from_dict(session or {}),
                  builder=builder_from_dict(builder or {}))


def load(path=DEFAULT_PATH):
    parser = configparser.ConfigParser(inline_comment_prefixes=("#",), interpolation=None)
    try:
        with open(path, encoding="utf-8") as f:
            parser.read_file(f)
    except OSError as e:
        raise SandboxError("cannot read config %s: %s" % (path, e.strerror)) from None
    return from_dict(dict(parser["sandbox"]) if parser.has_section("sandbox") else {},
                     dict(parser["session"]) if parser.has_section("session") else None,
                     dict(parser["builder"]) if parser.has_section("builder") else None)
