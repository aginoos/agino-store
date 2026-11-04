# SPDX-License-Identifier: GPL-3.0-or-later
"""AI source intake: task records and the commands that create or end them (docs/server.md section 15,
contract ai-intake 10.5).

An intake task is `state/intake/<id>.json`; the machine that advances it is intake_step, the acceptance is
intake_adopt. This module holds what is shared: statuses, the checks on user input, and
intake-submit / intake-cancel / intake-repair. Callers hold the state lock.
"""

import re
import time
import urllib.parse

from . import buildcfg, builds, debinfo, fetch
from .state import INTAKE_ID_RE, StateError

STATUSES = ("submitted", "fetching", "ready", "running", "verifying", "review", "adopted", "rejected", "failed",
            "cancelled", "paused")
TERMINAL = ("adopted", "rejected", "failed", "cancelled")
PROXY_STATUSES = ("ready", "running", "verifying")   # the AI task is running or about to run
KINDS = ("import", "repair")
AUTO_USER = "auto"
MAX_ADDRESS = 500
MAX_HINT = 1000
MAX_ALLOW_HOSTS = 5
MAX_ERROR = 1000
ADDRESS_FORBIDDEN_RE = re.compile(r"[\x00-\x20\x7f-\x9f\\<>\"'`{}|^]")
HINT_FORBIDDEN_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class IntakeError(Exception):
    """The request cannot be carried out (exit code 1)."""


class IntakeUsage(ValueError):
    """A bad argument (exit code 2)."""


def is_terminal(record):
    return record.get("status") in TERMINAL


def clean_error(value):
    return debinfo.clean_text(value).strip()[:MAX_ERROR]


# Checks on what a person typed

def check_address(address, allow_insecure=False):
    """(address, host): https, no credentials or fragment, a public host name, port 443 or none."""
    if not isinstance(address, str) or not address or len(address) > MAX_ADDRESS or ADDRESS_FORBIDDEN_RE.search(address):
        raise IntakeUsage("bad address: it must be one https URL of at most %d characters" % MAX_ADDRESS)
    try:
        parts = urllib.parse.urlsplit(address)
        port = parts.port
    except ValueError as e:
        raise IntakeUsage("bad address: not a valid URL") from e
    host = (parts.hostname or "").lower()
    schemes = ("https", "http") if allow_insecure else ("https",)
    if parts.scheme not in schemes or not host or parts.username or parts.password or parts.fragment:
        raise IntakeUsage("bad address: it must be an https URL without credentials or fragment")
    if port not in (None, 443) and not allow_insecure:
        raise IntakeUsage("bad address: only the default https port is allowed")
    if not allow_insecure and fetch.forbidden_host(host):
        raise IntakeUsage("bad address: the host must be a public name, not an address literal or local name")
    try:
        buildcfg.parse_hosts(host, allow_insecure)
    except ValueError as e:
        raise IntakeUsage("bad address: %s" % e) from e
    return address, host


def address_key(address):
    """What makes two addresses the same task: host, path without a trailing slash, query."""
    parts = urllib.parse.urlsplit(address)
    return "%s%s?%s" % ((parts.netloc or "").lower(), parts.path.rstrip("/"), parts.query)


def check_hint(hint):
    if hint is None:
        return ""
    if not isinstance(hint, str) or len(hint) > MAX_HINT or HINT_FORBIDDEN_RE.search(hint):
        raise IntakeUsage("bad hint: at most %d characters, no control characters" % MAX_HINT)
    return hint.strip()


def check_hosts(values, allow_insecure=False):
    """The --allow-host values: lower-case public host names, at most five."""
    values = list(values or [])
    if len(values) > MAX_ALLOW_HOSTS:
        raise IntakeUsage("at most %d --allow-host values" % MAX_ALLOW_HOSTS)
    try:
        hosts = buildcfg.parse_hosts(" ".join(values), allow_insecure) if values else ()
    except ValueError as e:
        raise IntakeUsage("bad --allow-host: %s" % e) from e
    return tuple(dict.fromkeys(hosts))


def check_agent(cfg, agent):
    chosen = agent or cfg.intake_agents[0]
    if chosen not in cfg.intake_agents:
        raise IntakeUsage("agent %r is not set up (agents: %s)" % (chosen, " ".join(cfg.intake_agents)))
    return chosen


def check_id(intake_id):
    if not isinstance(intake_id, str) or not INTAKE_ID_RE.match(intake_id):
        raise IntakeUsage("bad intake id: %r" % (intake_id,))
    return intake_id


# Records

def new_record(st, kind, address, hint, agent, allow_hosts, by, now, repair_of=None):
    """A fresh `submitted` record. Beyond the contract's fields it keeps what the state machine needs:
    rounds (corrections used), retries and next_retry (model pauses), agents_tried, version (of the verification),
    candidate (name, once adoption made it) and notes."""
    return {
        "id": st.new_intake_id(), "kind": kind, "app": (repair_of or {}).get("app"), "address": address,
        "hint": hint, "agent": agent, "allow_hosts": list(allow_hosts), "status": "submitted", "by": by,
        "created": now, "started": None, "finished": None, "error": "", "ai_task": None, "verify_task": None,
        "repair_of": repair_of, "attempt": 0, "created_ns": time.time_ns(),   # orders records of one second
        "rounds": 0, "retries": 0, "next_retry": None, "agents_tried": [], "version": None, "candidate": None,
    }


def unfinished(st):
    return [r for r in st.intakes() if not is_terminal(r)]


def _check_capacity(cfg, st):
    if len(unfinished(st)) >= cfg.intake_max_unfinished:
        raise IntakeError("there are already %d unfinished intake tasks" % cfg.intake_max_unfinished)


def submit(cfg, st, address, hint, agent, allow_hosts, by, now=None):
    """intake-submit: a new import task. Returns the record."""
    builds.check_by(by)
    address, host = check_address(address, cfg.allow_insecure)
    hint = check_hint(hint)
    agent = check_agent(cfg, agent)
    hosts = list(check_hosts(allow_hosts, cfg.allow_insecure))
    if host not in cfg.upstream_allowed_hosts and host not in hosts:
        hosts.append(host)   # the agent has to be able to reach the page it was pointed at
    key = address_key(address)
    for other in unfinished(st):
        if other.get("kind") == "import" and address_key(other.get("address") or "") == key:
            raise IntakeError("there is already an unfinished task for this address (%s)" % other["id"])
    _check_capacity(cfg, st)
    record = new_record(st, "import", address, hint, agent, hosts, by, now or debinfo.utc_now())
    st.save_intake(record)
    return record


def latest_failed_build(st, app, version):
    """The newest failed ordinary build task of (app, upstream version), or None."""
    found = [b for b in st.builds(app=app) if b.get("upstream_version") == version and b.get("status") == "failed"
             and b.get("mode") != "ai" and not b.get("verify")]
    return found[-1] if found else None


def create_repair(cfg, st, content, app, version, by, now=None, failed_task=None):
    """A repair task for the recipe of `app` at upstream `version` (needs upstreams/<app>.yaml, a recipe and a
    version seen by upstream-check). Raises IntakeError; callers hold the state lock. Returns the record."""
    builds.check_by(by)
    builds.check_version(version)
    up, recipe = content.upstream(app), content.recipe(app)
    if up is None or recipe is None:
        raise IntakeError("%s has no upstream tracking or no build recipe" % app)
    if not isinstance((st.upstream_memo(app).get("seen") or {}).get(version), dict):
        raise IntakeError("version %s of %s has not been seen by upstream-check" % (version, app))
    for other in unfinished(st):
        of = other.get("repair_of") or {}
        if other.get("kind") == "repair" and of.get("app") == app and of.get("version") == version:
            raise IntakeError("there is already an unfinished repair task for %s %s (%s)" % (app, version, other["id"]))
    _check_capacity(cfg, st)
    failed = failed_task or latest_failed_build(st, app, version)
    repair_of = {"app": app, "version": version, "failed_task": failed["id"] if failed else None}
    record = new_record(st, "repair", up.repo, "", check_agent(cfg, None), [], by, now or debinfo.utc_now(), repair_of)
    st.save_intake(record)
    return record


def auto_repairs(st, app, version):
    """How many automatic repair tasks (by auto) exist for (app, version), whatever their state."""
    return sum(1 for r in st.intakes() if r.get("kind") == "repair" and r.get("by") == AUTO_USER
               and (r.get("repair_of") or {}).get("app") == app and (r.get("repair_of") or {}).get("version") == version)


def cancel(cfg, st, intake_id, by, reject=False, now=None):
    """intake-cancel [--reject]: end an unfinished task; its unfinished build tasks are cancelled too."""
    builds.check_by(by)
    check_id(intake_id)
    try:
        record = st.intake(intake_id)
    except StateError as e:
        raise IntakeUsage(str(e)) from e
    if record is None:
        raise IntakeUsage("unknown intake task %s" % intake_id)
    if is_terminal(record):
        raise IntakeError("intake task %s is already %s" % (intake_id, record.get("status")))
    now = now or debinfo.utc_now()
    for key in ("ai_task", "verify_task"):
        if record.get(key):
            _cancel_build(cfg, st, record[key], by, now)
    status = "rejected" if reject else "cancelled"
    updated = dict(record, status=status, finished=now, error="%s by %s" % (status, by))
    st.save_intake(updated)
    return updated


def _cancel_build(cfg, st, build_id, by, now):
    try:
        build = st.build(build_id)
        if build is not None and not builds.is_terminal(build):
            builds.cancel(cfg, st, build_id, by, now=now)
    except (builds.BuildError, builds.BuildUsage, StateError):
        pass   # already over, or the record is gone: nothing to stop

