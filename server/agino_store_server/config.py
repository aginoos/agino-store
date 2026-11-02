# SPDX-License-Identifier: GPL-3.0-or-later
"""server.conf (docs/server.md section 2)."""

import configparser
import shlex
from dataclasses import dataclass

DEFAULT_PATH = "/etc/agino-store-server/server.conf"


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
        allow_insecure=_get(cp, "fetch", "allow_insecure", _bool),
        milkyway_keyring=_opt(cp, "milkyway", "keyring"),
        milkyway_fingerprints=tuple((_opt(cp, "milkyway", "fingerprints") or "").split()),
        sign_command=_sign_command(_opt(cp, "catalog", "sign_command")),
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
