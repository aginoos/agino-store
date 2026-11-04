# SPDX-License-Identifier: GPL-3.0-or-later
"""Configuration file (section [sandbox], see server/sandbox.conf.example)."""

import configparser
import dataclasses
import re

from . import build_ai_conf as ai_conf
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
    # AI mode (docs/sandbox.md section 10.7). ai_slot "" = the first slot after the regular ones (1 by default).
    "ai_slot": "",
    "ai_max_concurrent": "1",
    "ai_limits": "cpu=8 memory_gib=16 disk_gib=40 minutes=120",
    "ai_env": ai_conf.DEFAULT_ENV,
    "ai_claude_env": ai_conf.DEFAULT_CLAUDE_ENV,
    "ai_codex_env": ai_conf.DEFAULT_CODEX_ENV,
    "ai_claude_cmd": ai_conf.DEFAULT_CLAUDE_CMD,
    "ai_codex_cmd": ai_conf.DEFAULT_CODEX_CMD,
    "ai_codex_toml": ai_conf.DEFAULT_CODEX_TOML,
}
BUILDER_INT_RANGES = {"max_concurrent": (1, 4), "max_source_mib": (1, 4096), "max_artifact_mib": (1, 8192),
                      "max_log_mib": (1, 256)}
LIMIT_RANGES = {"cpu_minutes": (1, 24 * 60), "memory_gib": (1, 64), "disk_gib": (1, 512)}
AI_LIMIT_RANGES = {"cpu": (1, 64), "memory_gib": (1, 64), "disk_gib": (1, 512), "minutes": (1, 24 * 60)}
MAX_AI_SLOT = 15
ENV_ITEM_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,63}=[A-Za-z0-9._:/@+,%~-]{0,300}\Z")
CMD_RE = re.compile(r"\A[^\x00-\x1f\x7f]{1,1000}\Z")
APT_SOURCE_RE = re.compile(r"\A(?:\[[A-Za-z0-9=,./:_+-]{1,200}\] )?https?://[A-Za-z0-9.:/_-]{1,200} "
                           r"[A-Za-z0-9._-]{1,64}(?: [a-z0-9-]{1,32}){1,8}\Z")


@dataclasses.dataclass(frozen=True)
class BuildLimits:
    """Per-task ceilings; a recipe may only lower them (docs/build-tasks.md section 7.1)."""
    cpu_minutes: int = 90
    memory_gib: int = 8
    disk_gib: int = 20


@dataclasses.dataclass(frozen=True)
class AiLimits:
    """Ceilings of one AI task (the VM size and the run time of the agent); a task may only lower them."""
    cpu: int = 8
    memory_gib: int = 16
    disk_gib: int = 40
    minutes: int = 120


def parse_env_items(text, what):
    """`NAME=value NAME=value`: values without whitespace, quotes, `$`, backslashes or backticks."""
    items = tuple(text.split())
    for item in items:
        if not ENV_ITEM_RE.match(item):
            raise SandboxError("config [builder] %s: bad item %r" % (what, item[:40]))
    names = [i.partition("=")[0] for i in items]
    if len(set(names)) != len(names):
        raise SandboxError("config [builder] %s: a name is set twice" % what)
    return items


def _command(text, what):
    text = text.strip()
    if not CMD_RE.match(text):
        raise SandboxError("config [builder] %s must be one line of at most 1000 characters" % what)
    return text


def _toml(text):
    if len(text) > 4096 or "\x00" in text:
        raise SandboxError("config [builder] ai_codex_toml must be text of at most 4096 characters")
    return text if text.endswith("\n") or not text else text + "\n"


def parse_ai_limits(text):
    """`cpu=8 memory_gib=16 disk_gib=40 minutes=120` (any subset, missing keys keep the defaults)."""
    values = {}
    for item in text.split():
        key, sep, raw = item.partition("=")
        if not sep or key not in AI_LIMIT_RANGES or key in values or not raw.isdigit():
            raise SandboxError("config [builder] ai_limits: bad item %r" % item[:40])
        low, high = AI_LIMIT_RANGES[key]
        if not low <= int(raw) <= high:
            raise SandboxError("config [builder] ai_limits: %s must be between %d and %d" % (key, low, high))
        values[key] = int(raw)
    return AiLimits(**values)


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
    ai_slot: int = 1
    ai_max_concurrent: int = 1
    ai_limits: AiLimits = AiLimits()
    ai_env: tuple = tuple(ai_conf.DEFAULT_ENV.split())
    ai_claude_env: tuple = tuple(ai_conf.DEFAULT_CLAUDE_ENV.split())
    ai_codex_env: tuple = tuple(ai_conf.DEFAULT_CODEX_ENV.split())
    ai_claude_cmd: str = ai_conf.DEFAULT_CLAUDE_CMD
    ai_codex_cmd: str = ai_conf.DEFAULT_CODEX_CMD
    ai_codex_toml: str = ai_conf.DEFAULT_CODEX_TOML

    @property
    def ai_slots(self):
        """The slots AI tasks use: contiguous, never overlapping the regular ones (checked when loading)."""
        return tuple(range(self.ai_slot, self.ai_slot + self.ai_max_concurrent))


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
    return BuilderConfig(limits=parse_limits(merged["limits"]), **out, **_ai_options(merged, out["max_concurrent"]))


def _ai_options(merged, max_concurrent):
    """The AI mode keys of [builder]; the AI slots must not overlap the regular slots 0..max_concurrent-1."""
    out = {}
    for key, (low, high) in (("ai_max_concurrent", (1, 4)), ("ai_slot", (0, MAX_AI_SLOT))):
        raw = merged[key].strip()
        if key == "ai_slot" and not raw:
            out[key] = max(1, max_concurrent)  # default: right after the regular slots (1 with the default of 1)
            continue
        try:
            n = int(raw)
        except ValueError:
            raise SandboxError("config [builder] %s must be an integer" % key) from None
        if not low <= n <= high:
            raise SandboxError("config [builder] %s must be between %d and %d" % (key, low, high))
        out[key] = n
    if out["ai_slot"] < max_concurrent:
        raise SandboxError("config [builder] ai_slot must be %d or more: the slots below are the regular ones"
                           % max_concurrent)
    if out["ai_slot"] + out["ai_max_concurrent"] - 1 > MAX_AI_SLOT:
        raise SandboxError("config [builder] ai_slot + ai_max_concurrent must not pass slot %d" % MAX_AI_SLOT)
    out["ai_limits"] = parse_ai_limits(merged["ai_limits"])
    for key in ("ai_env", "ai_claude_env", "ai_codex_env"):
        out[key] = parse_env_items(merged[key], key)
    out["ai_claude_cmd"] = _command(merged["ai_claude_cmd"], "ai_claude_cmd")
    out["ai_codex_cmd"] = _command(merged["ai_codex_cmd"], "ai_codex_cmd")
    out["ai_codex_toml"] = _toml(merged["ai_codex_toml"])
    return out


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
