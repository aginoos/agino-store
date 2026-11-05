# SPDX-License-Identifier: GPL-3.0-or-later
"""server.conf (docs/server.md section 2)."""

import configparser
import shlex
from dataclasses import dataclass

from . import buildcfg

DEFAULT_PATH = "/etc/agino-store-server/server.conf"
DEFAULT_SANDBOX_QUEUE = "/srv/store/sandbox/queue"
DEFAULT_SHOTS_OUT = "/srv/store/sandbox/shots-out"
DEFAULT_SANDBOX_STATE = "/srv/store/sandbox/state"
DEFAULT_CAPTURES = "/srv/store/sandbox/captures"
DEFAULT_SCREENSHOTS = "/srv/store/screenshots"
DEFAULT_SHOTS_STAGING = "/var/lib/agino-admin/shots-staging"
DEFAULT_SHOTS_MAX_PER_LANG = 6
DEFAULT_SHOTS_MIN_WIDTH = 900
DEFAULT_BUILD_QUEUE = "/srv/store/build-queue"
DEFAULT_BUILD_OUT = "/srv/store/build-out"
DEFAULT_PROXY_ALLOW_FILE = "/srv/store/builder-proxy/allow-extra.list"
INTAKE_AGENTS = ("claude", "codex")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    content: str
    state: str
    downloads: str
    preview: str
    reports: str
    metrics: str
    milkyway_url: str
    milkyway_suite: str
    milkyway_components: tuple
    arch: str
    key: str
    deb_base_url: str
    valid_days: int
    renew_after_hours: int
    keep_catalogs: int
    user_agent: str
    min_interval_seconds: float
    max_deb_bytes: int
    max_index_bytes: int
    allow_insecure: bool
    milkyway_keyring: str | None = None
    milkyway_fingerprints: tuple = ()
    sign_command: tuple = ()
    sandbox_queue: str = DEFAULT_SANDBOX_QUEUE
    kick_dir: str | None = None                    # [paths] kick: see kick.py; unset = no kicks (tests)
    shots_out: str = DEFAULT_SHOTS_OUT
    sandbox_state: str = DEFAULT_SANDBOX_STATE   # read only by `status`
    screenshots: str = DEFAULT_SCREENSHOTS       # screenshot directory (docs/server.md section 12)
    captures: str = DEFAULT_CAPTURES             # interactive session captures, read by shots-session-import
    shots_staging: str = DEFAULT_SHOTS_STAGING   # where the admin console drops uploads for shots-ingest
    shots_max_per_lang: int = DEFAULT_SHOTS_MAX_PER_LANG
    shots_min_width: int = DEFAULT_SHOTS_MIN_WIDTH
    content_push: bool = False          # [content] push: content-edit/content-confirm also push to origin
    content_push_ssh_command: str = ""  # [content] push_ssh_command: GIT_SSH_COMMAND for those pushes
    upstream_allowed_hosts: tuple = buildcfg.DEFAULT_UPSTREAM_HOSTS   # [upstream] allowed_hosts
    upstream_check_interval_hours: int = 6                            # [upstream] check_interval_hours (timer hint)
    builds_queue: str = DEFAULT_BUILD_QUEUE      # [builds] queue: task.json + source.tar, written by build-step
    builds_out: str = DEFAULT_BUILD_OUT          # [builds] out: written by the build runner, read by build-step
    builds_max_concurrent: int = 1               # [builds] max_concurrent (read by the runner's config too)
    builds_limits: buildcfg.Limits = buildcfg.DEFAULT_LIMITS   # [builds] limits: the ceilings recipes may lower
    builds_max_source_mib: int = 512
    builds_max_artifact_mib: int = 1024
    builds_max_log_mib: int = 8
    intake_agents: tuple = ("claude",)           # [intake] agents: the AI agents that are set up, first = default
    intake_switch_agent: bool = False            # [intake] switch_agent: after a model failure try the next agent
    intake_ai_minutes: int = 120                 # [intake] ai_minutes: time limit of one AI task
    intake_max_rounds: int = 3                   # [intake] max_rounds: validation / replay corrections per task
    intake_model_retry_minutes: int = 5          # [intake] model_retry_minutes: pause after a model failure
    intake_model_max_retries: int = 12           # [intake] model_max_retries
    intake_auto_repair: bool = True              # [intake] auto_repair: open a repair task for recipe failures
    intake_max_auto_repairs: int = 2             # [intake] max_auto_repairs: per (app, version)
    intake_max_unfinished: int = 20              # [intake] max_unfinished: open tasks at once
    intake_proxy_allow_file: str = DEFAULT_PROXY_ALLOW_FILE   # [intake] proxy_allow_file
    intake_max_page_kib: int = 1024              # [intake] max_page_kib: one fetched page
    intake_max_inputs_mib: int = 8               # [intake] max_inputs_mib: everything handed to the agent
    intake_keep_days: int = 14                   # [intake] keep_days: working files of finished tasks

    def build_policy(self):
        """What the content validation needs: the allowed upstream hosts and the global build limits."""
        return buildcfg.Policy(self.upstream_allowed_hosts, self.builds_limits)


def _get(cp, section, key, conv=str):
    try:
        raw = cp.get(section, key)
    except (configparser.NoSectionError, configparser.NoOptionError) as e:
        raise ConfigError("missing [%s] %s" % (section, key)) from e
    try:
        return conv(raw.strip())
    except ValueError as e:
        raise ConfigError("bad value for [%s] %s: %r" % (section, key, raw)) from e


def _bool(raw):
    low = raw.lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    raise ValueError(raw)


def _opt(cp, section, key):
    try:
        return cp.get(section, key).strip() or None
    except (configparser.NoSectionError, configparser.NoOptionError):
        return None


def _shots_int(cp, key, default, low, high):
    raw = _opt(cp, "shots", key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ConfigError("bad value for [shots] %s: %r" % (key, raw)) from e
    if not low <= value <= high:
        raise ConfigError("[shots] %s must be between %d and %d" % (key, low, high))
    return value


def _content_push(cp):
    raw = _opt(cp, "content", "push")
    if raw is None:
        return False
    try:
        return _bool(raw)
    except ValueError as e:
        raise ConfigError("bad value for [content] push: %r (yes or no)" % raw) from e


def _section_int(cp, section, key, default, low, high):
    raw = _opt(cp, section, key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ConfigError("bad value for [%s] %s: %r" % (section, key, raw)) from e
    if not low <= value <= high:
        raise ConfigError("[%s] %s must be between %d and %d" % (section, key, low, high))
    return value


def _upstream_hosts(cp, allow_insecure):
    raw = _opt(cp, "upstream", "allowed_hosts")
    if raw is None:
        return buildcfg.DEFAULT_UPSTREAM_HOSTS
    try:
        return buildcfg.parse_hosts(raw, allow_insecure)
    except ValueError as e:
        raise ConfigError("bad value for [upstream] allowed_hosts: %s" % e) from e


def _build_limits(cp):
    try:
        return buildcfg.parse_limits(_opt(cp, "builds", "limits"))
    except ValueError as e:
        raise ConfigError("bad value for [builds] limits: %s" % e) from e


def _intake_agents(cp):
    raw = _opt(cp, "intake", "agents")
    if raw is None:
        return ("claude",)
    agents = tuple(dict.fromkeys(raw.replace(",", " ").split()))
    if not agents or any(a not in INTAKE_AGENTS for a in agents):
        raise ConfigError("bad value for [intake] agents: %r (use %s)" % (raw, " and ".join(INTAKE_AGENTS)))
    return agents


def _intake_bool(cp, key, default):
    raw = _opt(cp, "intake", key)
    if raw is None:
        return default
    try:
        return _bool(raw)
    except ValueError as e:
        raise ConfigError("bad value for [intake] %s: %r (yes or no)" % (key, raw)) from e


def _sign_command(raw):
    if not raw:
        return ()
    try:
        return tuple(shlex.split(raw))
    except ValueError as e:
        raise ConfigError("bad value for [catalog] sign_command") from e


def load(path=DEFAULT_PATH):
    cp = configparser.ConfigParser(inline_comment_prefixes=("#",), interpolation=None)
    try:
        with open(path, encoding="utf-8") as f:
            cp.read_file(f)
    except OSError as e:
        raise ConfigError("cannot read %s: %s" % (path, e.strerror)) from e
    insecure = _get(cp, "fetch", "allow_insecure", _bool)
    cfg = Config(
        content=_get(cp, "paths", "content"),
        state=_get(cp, "paths", "state"),
        downloads=_get(cp, "paths", "downloads"),
        preview=_get(cp, "paths", "preview"),
        reports=_get(cp, "paths", "reports"),
        metrics=_get(cp, "paths", "metrics"),
        milkyway_url=_get(cp, "milkyway", "url").rstrip("/"),
        milkyway_suite=_get(cp, "milkyway", "suite"),
        milkyway_components=tuple(_get(cp, "milkyway", "components").split()),
        arch=_get(cp, "milkyway", "arch"),
        key=_opt(cp, "catalog", "key") or "",
        deb_base_url=_get(cp, "catalog", "deb_base_url"),
        valid_days=_get(cp, "catalog", "valid_days", int),
        renew_after_hours=_get(cp, "catalog", "renew_after_hours", int),
        keep_catalogs=_get(cp, "catalog", "keep_catalogs", int),
        user_agent=_get(cp, "fetch", "user_agent"),
        min_interval_seconds=_get(cp, "fetch", "min_interval_seconds", float),
        max_deb_bytes=_get(cp, "fetch", "max_deb_bytes", int),
        max_index_bytes=_get(cp, "fetch", "max_index_bytes", int),
        allow_insecure=insecure,
        milkyway_keyring=_opt(cp, "milkyway", "keyring"),
        milkyway_fingerprints=tuple((_opt(cp, "milkyway", "fingerprints") or "").split()),
        sign_command=_sign_command(_opt(cp, "catalog", "sign_command")),
        sandbox_queue=_opt(cp, "paths", "sandbox_queue") or DEFAULT_SANDBOX_QUEUE,
        kick_dir=_opt(cp, "paths", "kick"),
        shots_out=_opt(cp, "paths", "shots_out") or DEFAULT_SHOTS_OUT,
        sandbox_state=_opt(cp, "paths", "sandbox_state") or DEFAULT_SANDBOX_STATE,
        screenshots=_opt(cp, "paths", "screenshots") or DEFAULT_SCREENSHOTS,
        captures=_opt(cp, "paths", "captures") or DEFAULT_CAPTURES,
        shots_staging=_opt(cp, "paths", "shots_staging") or DEFAULT_SHOTS_STAGING,
        shots_max_per_lang=_shots_int(cp, "max_per_lang", DEFAULT_SHOTS_MAX_PER_LANG, 1, 20),
        shots_min_width=_shots_int(cp, "min_width", DEFAULT_SHOTS_MIN_WIDTH, 100, 4096),
        content_push=_content_push(cp),
        content_push_ssh_command=_opt(cp, "content", "push_ssh_command") or "",
        upstream_allowed_hosts=_upstream_hosts(cp, insecure),
        upstream_check_interval_hours=_section_int(cp, "upstream", "check_interval_hours", 6, 1, 24 * 14),
        builds_queue=_opt(cp, "builds", "queue") or DEFAULT_BUILD_QUEUE,
        builds_out=_opt(cp, "builds", "out") or DEFAULT_BUILD_OUT,
        builds_max_concurrent=_section_int(cp, "builds", "max_concurrent", 1, 1, 16),
        builds_limits=_build_limits(cp),
        builds_max_source_mib=_section_int(cp, "builds", "max_source_mib", 512, 1, 4096),
        builds_max_artifact_mib=_section_int(cp, "builds", "max_artifact_mib", 1024, 1, 8192),
        builds_max_log_mib=_section_int(cp, "builds", "max_log_mib", 8, 1, 64),
        intake_agents=_intake_agents(cp),
        intake_switch_agent=_intake_bool(cp, "switch_agent", False),
        intake_ai_minutes=_section_int(cp, "intake", "ai_minutes", 120, 10, 600),
        intake_max_rounds=_section_int(cp, "intake", "max_rounds", 3, 0, 10),
        intake_model_retry_minutes=_section_int(cp, "intake", "model_retry_minutes", 5, 1, 1440),
        intake_model_max_retries=_section_int(cp, "intake", "model_max_retries", 12, 0, 1000),
        intake_auto_repair=_intake_bool(cp, "auto_repair", True),
        intake_max_auto_repairs=_section_int(cp, "intake", "max_auto_repairs", 2, 0, 20),
        intake_max_unfinished=_section_int(cp, "intake", "max_unfinished", 20, 1, 1000),
        intake_proxy_allow_file=_opt(cp, "intake", "proxy_allow_file") or DEFAULT_PROXY_ALLOW_FILE,
        intake_max_page_kib=_section_int(cp, "intake", "max_page_kib", 1024, 16, 8192),
        intake_max_inputs_mib=_section_int(cp, "intake", "max_inputs_mib", 8, 1, 64),
        intake_keep_days=_section_int(cp, "intake", "keep_days", 14, 1, 365),
    )
    if bool(cfg.key) == bool(cfg.sign_command):
        raise ConfigError("[catalog] exactly one of key and sign_command must be set")
    if cfg.milkyway_keyring and not cfg.milkyway_fingerprints:
        raise ConfigError("[milkyway] keyring needs fingerprints")
    if not cfg.deb_base_url.endswith("/"):
        raise ConfigError("[catalog] deb_base_url must end with /")
    if cfg.valid_days <= 0 or cfg.renew_after_hours <= 0 or cfg.keep_catalogs <= 0:
        raise ConfigError("[catalog] valid_days, renew_after_hours and keep_catalogs must be positive")
    return cfg
