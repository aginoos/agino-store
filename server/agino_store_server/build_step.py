# SPDX-License-Identifier: GPL-3.0-or-later
"""build-step: the once-a-minute driver of build tasks (docs/server.md section 14, contract 7.3).

  1. queued tasks: fetch the pinned source (upstream_fetch; no state lock is held while it runs) and publish
     builds.queue/<id>/{task.json, source.tar}; the task becomes `ready`;
  2. ready/building tasks: follow builds.out/<id>/status.json;
  3. tasks with a result.json: collect the deb as a pending candidate (build_collect);
  4. tasks that ran past their time limit fail with a timeout (and get a cancel file);
  4b. recipe failures of tracked apps open a repair task for the AI (intake_auto, at most twice per version);
  5. housekeeping: finished tasks lose their queue directory after an hour, stale fetch directories go.

The caller holds the `build-step` lock, so only one run is active; state changes take the main state
lock briefly and re-read the record first (a build-cancel may have come in meanwhile).
"""

import datetime
import os
import re
import shutil
import tempfile

from . import build_collect, builds, debinfo, intake_auto, source_assembly, upstream_fetch
from .state import write_json_atomic
from .status_io import SMALL_READ, ReadError, read_json

LOCK_WAIT = 60.0
TIMEOUT_MARGIN_MINUTES = 35   # the runner allows cpu_minutes + 25 minutes (packaging, retrieval) plus its start-up wait
KEEP_QUEUE_SECONDS = 3600
STALE_FETCH_SECONDS = 86400
FETCH_PREFIX = ".fetch-"
STATUS_FILE = "status.json"
UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
UTC_RE = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
RUNNING_STATES = ("starting", "installing_deps", "building", "packaging", "collecting", "testing",
                  "preparing", "running", "ai")   # all mean `building` here
STOPPED_STATES = ("failed", "cancelled")
STATUS_GRACE = 300   # seconds a stopped runner status may stand without a result.json


def _epoch(stamp):
    try:
        return datetime.datetime.strptime(stamp, UTC_FORMAT).replace(tzinfo=datetime.UTC).timestamp()
    except (TypeError, ValueError):
        return None


def _iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.UTC).strftime(UTC_FORMAT)


def _transition(st, build_id, allowed, **changes):
    """Under the state lock: change a record only if it still has one of the `allowed` statuses."""
    record = st.build(build_id)
    if record is None or record.get("status") not in allowed:
        return None
    updated = dict(record, **changes)
    st.save_build(updated)
    return updated


def _fail(st, build_id, allowed, message, rep, now, klass=None):
    """Mark a task failed; `klass` (recipe | infra | timeout | model) lands in the record's `class`."""
    extra = {"class": klass} if klass else {}
    with st.lock(wait=LOCK_WAIT):
        done = _transition(st, build_id, allowed, status="failed", finished=_iso(now),
                           error=builds.clean_error(message), **extra)
    if done is not None:
        rep.fail("failed %s: %s" % (build_id, done["error"]))


# 1. fetch

def _write_task(cfg, record, directory, sha, size):
    write_json_atomic(os.path.join(directory, builds.TASK_FILE), builds.task_document(record, sha, size))
    if os.path.getsize(os.path.join(directory, builds.TASK_FILE)) > builds.MAX_TASK_BYTES:
        raise upstream_fetch.RecipeSourceError("task.json would be larger than the build runner accepts")
    os.chmod(os.path.join(directory, builds.TASK_FILE), 0o640)
    os.chmod(os.path.join(directory, builds.SOURCE_FILE), 0o640)
    os.chmod(directory, 0o750)


def _publish(cfg, st, record, tmp, sha, size, rep, fetched=None):
    with st.lock(wait=LOCK_WAIT):
        current = st.build(record["id"])
        if current is None or current.get("status") != "fetching":
            rep.add("dropped the source of %s (the task is %s)" % (record["id"], (current or {}).get("status")))
            return
        final = builds.queue_dir(cfg, record["id"])
        if os.path.islink(final):
            os.unlink(final)
        elif os.path.lexists(final):
            shutil.rmtree(final)   # left over from an earlier attempt at this same task
        os.rename(tmp, final)
        extra = {"fetched": fetched} if fetched is not None else {}
        st.save_build(dict(current, status="ready", source={"sha256": sha, "size": size}, **extra))
    rep.add("fetched %s: %s %s (%.1f MiB)" % (record["id"], record["app"], record["upstream_version"], size / (1 << 20)))


def _fetch_source(cfg, record, tmp):
    """(sha256, size, fetched) of the task's source.tar written into `tmp`. A v2 recipe (sources) is assembled
    from every source; a v1 recipe has the single pinned source. `fetched` is None for v1."""
    recipe, dest = record["recipe"], os.path.join(tmp, builds.SOURCE_FILE)
    if recipe.get("sources"):
        return source_assembly.fetch_sources(cfg, record, tmp, dest)
    sha, size = upstream_fetch.fetch_source(cfg, recipe["source"], record["repo"], record["tag"], record["commit"],
                                            tmp, dest)
    return sha, size, None


def _fetch_one(cfg, st, build_id, rep, now):
    with st.lock(wait=LOCK_WAIT):
        record = _transition(st, build_id, ("queued",), status="fetching")
    if record is None:
        return
    os.makedirs(cfg.builds_queue, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=FETCH_PREFIX, dir=cfg.builds_queue)
    try:
        try:
            sha, size, fetched = _fetch_source(cfg, record, tmp)
            _write_task(cfg, record, tmp, sha, size)
        except (upstream_fetch.UpstreamError, OSError) as e:
            _fail(st, build_id, ("fetching",), "fetching the source failed: %s" % e, rep, now,
                  getattr(e, "klass", "infra"))
            return
        except Exception as e:  # noqa: BLE001 - one task must not stop the others
            _fail(st, build_id, ("fetching",), "internal error while fetching: %s" % type(e).__name__, rep, now,
                  "infra")
            return
        try:
            _publish(cfg, st, record, tmp, sha, size, rep, fetched)
        except OSError as e:
            _fail(st, build_id, ("fetching",), "publishing the source failed: %s" % e.strerror, rep, now)
    finally:
        if os.path.lexists(tmp):
            shutil.rmtree(tmp, ignore_errors=True)


def _fetch_queued(cfg, st, rep, now):
    with st.lock(wait=LOCK_WAIT):
        for record in st.builds():
            if record.get("status") == "fetching":   # a run that died while fetching
                st.save_build(dict(record, status="queued"))
        queued = [r["id"] for r in st.builds() if r.get("status") == "queued"]
    for build_id in queued:
        _fetch_one(cfg, st, build_id, rep, now)


# 2. runner status

def _read_out(cfg, build_id, name):
    directory = builds.out_dir(cfg, build_id)
    if os.path.islink(directory):
        return None
    try:
        data = read_json(os.path.join(directory, name), SMALL_READ)
    except ReadError:
        return None
    return data if isinstance(data, dict) else None


def _status_age(cfg, build_id, now):
    try:
        return now - os.lstat(os.path.join(builds.out_dir(cfg, build_id), STATUS_FILE)).st_mtime
    except OSError:
        return 0.0


def _sync_status(cfg, st, rep, now):
    """Follow builds.out/<id>/status.json: {id, state, started, updated, step, steps_total, error}."""
    for record in st.builds():
        if record.get("status") not in ("ready", "building"):
            continue
        data = _read_out(cfg, record["id"], STATUS_FILE)
        state = (data or {}).get("state")
        if state in RUNNING_STATES and record["status"] == "ready":
            started = data.get("started") if isinstance(data.get("started"), str) and UTC_RE.match(data["started"]) else _iso(now)
            with st.lock(wait=LOCK_WAIT):
                if _transition(st, record["id"], ("ready",), status="building", started=started) is not None:
                    rep.add("building %s: %s %s" % (record["id"], record["app"], record["version"]))
        elif state in STOPPED_STATES and _status_age(cfg, record["id"], now) > STATUS_GRACE:
            if not os.path.lexists(os.path.join(builds.out_dir(cfg, record["id"]), build_collect.RESULT_FILE)):
                error = data.get("error") if isinstance(data.get("error"), str) and data["error"].strip() else state
                _fail(st, record["id"], ("ready", "building"), "the runner stopped: %s" % error, rep, now, "infra")


# 4. timeouts

def _deadline(record):
    started = _epoch(record.get("started"))
    minutes = (record.get("limits") or {}).get("cpu_minutes")
    if started is None or not isinstance(minutes, int):
        return None
    return started + (minutes + TIMEOUT_MARGIN_MINUTES) * 60


def _expire(cfg, st, rep, now):
    for record in st.builds():
        if record.get("status") not in ("ready", "building"):
            continue
        deadline = _deadline(record)
        if deadline is None or now <= deadline:
            continue
        if os.path.lexists(os.path.join(builds.out_dir(cfg, record["id"]), build_collect.RESULT_FILE)):
            continue   # the next run collects it
        builds.write_cancel_file(cfg, record, "build-step", _iso(now))
        _fail(st, record["id"], ("ready", "building"), "timeout", rep, now, "timeout")


# 5. housekeeping

def _remove_dir(path):
    """Delete exactly `path` (a directory below builds.queue chosen by this module)."""
    if os.path.islink(path):
        os.unlink(path)
    else:
        shutil.rmtree(path, ignore_errors=True)


def _cleanup(cfg, st, rep, now):
    for record in st.builds():
        finished = _epoch(record.get("finished"))
        if not builds.is_terminal(record) or finished is None or now - finished < KEEP_QUEUE_SECONDS:
            continue
        directory = builds.queue_dir(cfg, record["id"])
        if os.path.lexists(directory):
            _remove_dir(directory)
            rep.add("removed the queue directory of %s" % record["id"])
    try:
        names = sorted(os.listdir(cfg.builds_queue))
    except OSError:
        return
    for name in names:
        path = os.path.join(cfg.builds_queue, name)
        if name.startswith(FETCH_PREFIX) and os.path.isdir(path) and not os.path.islink(path):
            if now - os.lstat(path).st_mtime > STALE_FETCH_SECONDS:
                _remove_dir(path)
                rep.add("removed the stale fetch directory %s" % name)


def step(cfg, st, now):
    """One build-step run (the caller holds the build-step lock); `now` is epoch seconds. Returns a Report."""
    rep = builds.Report()
    _fetch_queued(cfg, st, rep, now)
    _sync_status(cfg, st, rep, now)
    build_collect.collect_results(cfg, st, rep, now)
    _expire(cfg, st, rep, now)
    intake_auto.create_repairs(cfg, st, rep, now)
    _cleanup(cfg, st, rep, now)
    return rep
