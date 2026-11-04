# SPDX-License-Identifier: GPL-3.0-or-later
"""`status --json` sections sandbox (queue, job states, reports) and timers (systemctl show)."""

import os
import subprocess

from .sandboxqueue import NAME_RE
from .status_io import SMALL_READ, ReadError, iso, list_dir, mtime_iso, pick, read_json

MAX_REPORTS = 30
MAX_SUMMARY = 200
SYSTEMCTL_TIMEOUT = 5
UNIT_NAMES = ("track", "check", "build", "sandbox", "reports", "upstream-check", "build-step", "intake-step")
UNITS = tuple("agino-store-%s.%s" % (n, kind) for n in UNIT_NAMES for kind in ("service", "timer"))
PROPERTIES = ("LoadState", "ActiveState", "SubState", "Result", "LastTriggerUSec",
              "NextElapseUSecRealtime", "ExecMainStatus")


def _queue(cfg, errors):
    out = []
    try:
        names = [n for n in list_dir(cfg.sandbox_queue) if n.endswith(".json") and NAME_RE.match(n)]
    except ReadError as e:
        if os.path.lexists(cfg.sandbox_queue):
            errors.add("sandbox", e)
        return out
    for name in names:
        path = os.path.join(cfg.sandbox_queue, name)
        try:
            job = read_json(path, SMALL_READ)
        except ReadError as e:
            errors.add("sandbox", "queue %s" % e)
            continue
        job = job if isinstance(job, dict) else {}
        out.append({"job": name[:-5], "mode": job.get("mode") or "check", "app": job.get("app"),
                    "version": job.get("version"), "size": job.get("size"),
                    "created": job.get("queued") or mtime_iso(path)})
    return sorted(out, key=lambda j: (str(j["created"] or ""), j["job"]), reverse=True)


def _jobs(cfg, errors):
    out = []
    try:
        names = [n for n in list_dir(cfg.sandbox_state) if n.endswith(".json")]
    except ReadError:
        return out   # the state directory belongs to the sandbox host; it may not exist here
    for name in names:
        path = os.path.join(cfg.sandbox_state, name)
        try:
            doc = read_json(path, SMALL_READ)
        except ReadError as e:
            errors.add("sandbox", "state %s" % e)
            continue
        entry = pick(doc, ("status", "result", "attempts", "reason", "report"))
        entry.update(job=name[:-5], updated=(doc.get("updated") if isinstance(doc, dict) else None)
                     or mtime_iso(path))
        out.append(entry)
    return sorted(out, key=lambda j: (str(j["updated"] or ""), j["job"]), reverse=True)


def _report_files(cfg):
    found = []
    for sub in ("", "done"):
        directory = os.path.join(cfg.reports, sub)
        try:
            names = [n for n in list_dir(directory) if n.endswith(".json")]
        except ReadError:
            continue
        for name in names:
            path = os.path.join(directory, name)
            try:
                found.append((os.lstat(path).st_mtime, path, name, sub or "incoming"))
            except OSError:
                continue
    return sorted(found, reverse=True)[:MAX_REPORTS]


def _reports(cfg, errors):
    out = []
    for mtime, path, name, where in _report_files(cfg):
        try:
            doc = read_json(path, SMALL_READ)
        except ReadError as e:
            errors.add("sandbox", "report %s" % e)
            continue
        if not isinstance(doc, dict):
            errors.add("sandbox", "report %s: not an object" % name)
            continue
        out.append({"name": name, "location": where, "app": doc.get("app"), "version": doc.get("version"),
                    "result": doc.get("status"), "finished": doc.get("finished"), "at": iso(mtime),
                    "summary": str(doc.get("summary") or "")[:MAX_SUMMARY]})
    return out


def sandbox_section(cfg, errors):
    return {"queue": _queue(cfg, errors), "jobs": _jobs(cfg, errors), "reports": _reports(cfg, errors)}


def _show(unit):
    argv = ["systemctl", "show", "--no-pager"] + ["-p%s" % p for p in PROPERTIES] + [unit]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT, check=False)
    values = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
    return dict({"unit": unit}, **{k: v for k, v in values.items() if k in PROPERTIES})


def timers_section(errors):
    out = []
    for unit in UNITS:
        try:
            out.append(_show(unit))
        except FileNotFoundError:
            errors.add("timers", "systemctl is not available")
            return []
        except (OSError, subprocess.SubprocessError) as e:
            errors.add("timers", "%s: %s" % (unit, type(e).__name__))
    return out
