# SPDX-License-Identifier: GPL-3.0-or-later
"""build-step, step 3: turn the runner's result.json and .deb into a pending candidate (contract 7.3).

Everything under builds.out/<id>/ was produced by the build VM through the runner and is untrusted
data: sizes are checked, the deb is hashed, its control data is read with dpkg-deb (never installed or
run) and the candidate then goes through the same inspect, diff and dependency checks as any intake.
"""

import os
import re

from . import builds, checks, debinfo, fetch, trackers
from .state import StateError
from .status_io import SMALL_READ, ReadError, read_json

DEB_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+~-]{0,198}\.deb\Z")
RESULT_FILE = "result.json"
PARTIAL_GRACE = 300       # seconds an unreadable result.json may still be being written
LOCK_WAIT = 60.0
COLLECT_ERRORS = (trackers.TrackError, debinfo.DebError, checks.CheckError, StateError, OSError, fetch.FetchError)
ACTIVE = ("ready", "building", "collecting")


class CollectError(Exception):
    """The task failed; the message goes into its record."""


def _read_result(path, now):
    """The parsed result.json, None while it may still be half written. CollectError when it stays unreadable."""
    try:
        data = read_json(path, SMALL_READ)
    except ReadError as e:
        try:
            age = now - os.lstat(path).st_mtime
        except OSError:
            return None
        if age < PARTIAL_GRACE:
            return None
        raise CollectError("result.json is unreadable: %s" % e) from e
    if not isinstance(data, dict):
        raise CollectError("result.json is not an object")
    return data


def _failure_text(result):
    text = result.get("error") if isinstance(result.get("error"), str) else ""
    if not text.strip():
        bad = [s for s in result.get("steps") or [] if isinstance(s, dict) and s.get("exit") not in (0, None)]
        text = "build step %s exited with %s" % (bad[0].get("n"), bad[0].get("exit")) if bad else "build failed"
    return builds.clean_error(text)


def _the_deb(result):
    arts = [a for a in result.get("artifacts") or [] if isinstance(a, dict) and str(a.get("file", "")).endswith(".deb")]
    if len(arts) != 1:
        raise CollectError("expected exactly one deb in the result, found %d" % len(arts))
    art = arts[0]
    name, sha, size = art.get("file"), art.get("sha256"), art.get("size")
    if not isinstance(name, str) or not DEB_NAME_RE.match(name):
        raise CollectError("bad artifact file name")
    if not isinstance(sha, str) or not fetch.SHA256_RE.match(sha) or isinstance(size, bool) or not isinstance(size, int):
        raise CollectError("bad artifact checksum or size")
    return name, sha, size


def _checked_file(cfg, rec, name, sha, size):
    path = os.path.join(builds.out_dir(cfg, rec["id"]), name)
    try:
        info = os.lstat(path)
    except OSError as e:
        raise CollectError("the deb listed in the result is missing") from e
    if not os.path.isfile(path) or os.path.islink(path):
        raise CollectError("the deb is not a regular file")
    if info.st_size != size or size > min(cfg.builds_max_artifact_mib << 20, cfg.max_deb_bytes):
        raise CollectError("the deb has the wrong size or exceeds the artifact limit")
    if fetch.file_sha256(path) != sha:
        raise CollectError("the deb does not match its sha256")
    return path


def _ingest(cfg, st, rec, path, name, sha):
    stored, stored_sha, size = fetch.store_file(path, cfg.downloads, name)
    if stored_sha != sha:
        os.unlink(stored)
        raise CollectError("the deb changed while it was copied")
    dl = fetch.Download(rec["repo"], stored, sha, size, os.path.basename(stored), None, None)
    target = trackers.Target(rec["app"], None, {"package": rec["package"], "arch": cfg.arch})
    origin = {"type": "built", "source_url": rec["repo"], "source_ref": rec["tag"],
              "source_commit": rec["commit"], "build_job": rec["id"]}
    candidate = trackers.ingest(cfg, st, target, dl, rec["repo"], origin, rec["version"])
    return {"file": os.path.basename(stored), "sha256": sha, "size": size}, os.path.basename(candidate["_path"])[:-5]


def _outcome(cfg, st, rec, result):
    if result.get("ok") is not True:
        raise CollectError(_failure_text(result))
    name, sha, size = _the_deb(result)
    path = _checked_file(cfg, rec, name, sha, size)
    try:
        artifact, candidate = _ingest(cfg, st, rec, path, name, sha)
    except COLLECT_ERRORS as e:
        raise CollectError("the deb was refused: %s" % builds.clean_error(e)) from e
    return {"artifact": artifact, "candidate": candidate}


def _collect_one(cfg, st, rec_id, result, rep):
    with st.lock(wait=LOCK_WAIT):
        rec = st.build(rec_id)
        if rec is None or rec.get("status") not in ACTIVE:
            return
        now = debinfo.utc_now()
        st.save_build(dict(rec, status="collecting"))
        try:
            changes = dict(_outcome(cfg, st, rec, result), status="done", error="")
            rep.add("collected %s: %s %s -> candidate %s" % (rec_id, rec["app"], rec["version"], changes["candidate"]))
        except CollectError as e:
            changes = {"status": "failed", "error": builds.clean_error(e)}
            rep.fail("failed %s: %s" % (rec_id, changes["error"]))
        except Exception as e:  # noqa: BLE001 - a task must never stay in collecting because of a bug
            changes = {"status": "failed", "error": "internal error: %s" % type(e).__name__}
            rep.fail("failed %s: %s" % (rec_id, changes["error"]))
        st.save_build(dict(rec, finished=now, **changes))


def collect_results(cfg, st, rep, now):
    """Step 3: every active task that has a result.json."""
    for rec in st.builds():
        if rec.get("status") not in ACTIVE:
            continue
        directory = builds.out_dir(cfg, rec["id"])
        path = os.path.join(directory, RESULT_FILE)
        if os.path.islink(directory) or not os.path.lexists(path):
            continue
        try:
            result = _read_result(path, now)
        except CollectError as e:
            with st.lock(wait=LOCK_WAIT):
                fresh = st.build(rec["id"])
                if fresh is not None and fresh.get("status") in ACTIVE:
                    st.save_build(dict(fresh, status="failed", finished=debinfo.utc_now(),
                                       error=builds.clean_error(e)))
                    rep.fail("failed %s: %s" % (rec["id"], builds.clean_error(e)))
            continue
        if result is not None:
            _collect_one(cfg, st, rec["id"], result, rep)
