# SPDX-License-Identifier: GPL-3.0-or-later
"""intake-step: the once-a-minute machine of AI intake tasks (docs/server.md section 15, contract 10.5).

  submitted   fetch the reading material (intake_materials), make workspace.tar, create the AI build task
  ready       the AI task waits for the runner
  running     the runner works on it
  (AI task done)  read and validate the delivery; a bad delivery goes back to the agent (a correction round,
                  at most [intake] max_rounds); a good one gets a verification build (recipe replayed in a clean VM).
                  A delivery with vendor.yaml (the vendor's own deb) has no recipe to replay: the server runs the
                  tracking file once (intake_vendor, no VM) and goes straight to `review`, or hands the failure back
  verifying   the verification build runs (ordinary build task, `verify`, no candidate is made from it)
  review      replay passed; a person looks at the recipe and adopts or rejects it (intake_adopt, intake-cancel)
  paused      the model service was unavailable; retried every [intake] model_retry_minutes, at most model_max_retries

Failures of the AI task: class `model` switches the agent (when allowed and another one is set up) or pauses
the task; anything else ends it. The caller holds the `intake-step` lock; state changes take the main lock
briefly and re-read the record first, so intake-cancel and approve are not held up by fetches.
"""

import datetime
import os
import shutil
import tempfile

from . import (builds, content, debinfo, intake, intake_delivery, intake_materials, intake_proxy, intake_vendor,
               intake_workspace)
from .intake import IntakeError
from .state import StateError, StateLocked, write_json_atomic
from .upstream_fetch import UpstreamError

LOCK_WAIT = 60.0
FETCH_PREFIX = ".fetch-"
FEEDBACK_DIR = "feedback"
PREVIOUS_DIR = "previous"
WAITING_BUILD = ("queued", "fetching", "ready", "building", "collecting")
ACTIVE = ("submitted", "fetching", "ready", "running", "verifying", "paused")
UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def stamp(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.UTC).strftime(UTC_FORMAT)


def epoch_of(text):
    try:
        return datetime.datetime.strptime(text, UTC_FORMAT).replace(tzinfo=datetime.UTC).timestamp()
    except (TypeError, ValueError):
        return None


def _move(st, intake_id, allowed, **changes):
    """Under the state lock: change a record only if it still has one of the `allowed` statuses."""
    with st.lock(wait=LOCK_WAIT):
        record = st.intake(intake_id)
        if record is None or record.get("status") not in allowed:
            return None
        updated = dict(record, **changes)
        st.save_intake(updated)
        return updated


def _fail(st, rec, message, rep, now):
    done = _move(st, rec["id"], ACTIVE + ("review",), status="failed", finished=stamp(now),
                 error=intake.clean_error(message))
    if done is not None:
        rep.fail("failed intake %s: %s" % (rec["id"], done["error"]))
    return done


class Context:
    """What one run needs: config, state, the loaded content (loaded once, only when a task needs it)."""

    def __init__(self, cfg, st, rep, now):
        self.cfg, self.st, self.rep, self.now = cfg, st, rep, now
        self._content = None
        self.broken = False

    def content(self):
        if self._content is None and not self.broken:
            try:
                self._content = content.load(self.cfg.content, allow_insecure=self.cfg.allow_insecure,
                                             policy=self.cfg.build_policy())
            except content.ContentError:
                self.broken = True
                self.rep.fail("store-content is invalid: intake tasks that need it wait")
        return self._content


# Launching an AI task

def _round_inputs(st, rec):
    """(feedback, previous) saved for the current correction round: (name, text) or None, {name: bytes} or None."""
    base = st.intake_dir(rec["id"])
    feedback = previous = None
    directory = os.path.join(base, FEEDBACK_DIR)
    for name in intake_workspace.FEEDBACK_NAMES:
        path = os.path.join(directory, name)
        if os.path.isfile(path) and not os.path.islink(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                feedback = (name, f.read(intake_workspace.MAX_FEEDBACK_BYTES))
    directory = os.path.join(base, PREVIOUS_DIR)
    if os.path.isdir(directory) and not os.path.islink(directory):
        previous = {}
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            if os.path.isfile(path) and not os.path.islink(path):
                with open(path, "rb") as f:
                    previous[name] = f.read(intake_delivery.MAX_TEXT)
    return feedback, previous


def _save_round(st, rec, feedback_name, text, previous):
    """Replace the saved feedback and previous delivery of a task (inputs of the next AI task)."""
    base = st.intake_dir(rec["id"], create=True)
    for part in (FEEDBACK_DIR, PREVIOUS_DIR):
        shutil.rmtree(os.path.join(base, part), ignore_errors=True)
    os.makedirs(os.path.join(base, FEEDBACK_DIR), mode=0o750)
    with open(os.path.join(base, FEEDBACK_DIR, feedback_name), "w", encoding="utf-8") as f:
        f.write(text)
    os.makedirs(os.path.join(base, PREVIOUS_DIR), mode=0o750)
    for name, data in (previous or {}).items():
        with open(os.path.join(base, PREVIOUS_DIR, name), "wb") as f:
            f.write(data)


def _ai_record(cfg, st, rec, agent, now):
    of = rec.get("repair_of") or {}
    label = "intake-" + rec["id"][:8]
    version = of.get("version") or "0"
    return {
        "id": st.new_build_id(), "app": of.get("app") or label, "version": version, "upstream_version": version,
        "tag": None, "commit": None, "attempt": rec["attempt"] + 1, "status": "ready", "by": rec["by"],
        "created": stamp(now), "started": None, "finished": None, "error": "", "artifact": None, "candidate": None,
        "mode": "ai", "kind": rec["kind"], "agent": agent, "minutes": cfg.intake_ai_minutes, "intake": rec["id"],
        "limits": {"cpu_minutes": cfg.intake_ai_minutes}, "repo": rec["address"], "package": None, "recipe": None,
        "source": None,
    }


def launch_ai(cfg, st, rec, agent, now):
    """Create the AI build task (workspace.tar + task.json in builds.queue) and point the intake record at it.
    Returns the updated intake record."""
    inputs = os.path.join(st.intake_dir(rec["id"]), "inputs.tar")
    feedback, previous = _round_inputs(st, rec)
    ai = _ai_record(cfg, st, rec, agent, now)
    os.makedirs(cfg.builds_queue, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=FETCH_PREFIX, dir=cfg.builds_queue)
    try:
        working = dict(rec, rounds=rec.get("rounds") or 0)
        working["feedback_file"] = feedback[0] if feedback else None
        intake_workspace.build(cfg, working, inputs, os.path.join(tmp, builds.WORKSPACE_FILE), feedback, previous)
        write_json_atomic(os.path.join(tmp, builds.TASK_FILE), builds.ai_task_document(ai))
        for name in (builds.TASK_FILE, builds.WORKSPACE_FILE):
            os.chmod(os.path.join(tmp, name), 0o640)
        os.chmod(tmp, 0o750)
        with st.lock(wait=LOCK_WAIT):
            current = st.intake(rec["id"])
            if current is None or current.get("status") in intake.TERMINAL:
                return None
            os.rename(tmp, builds.queue_dir(cfg, ai["id"]))
            st.save_build(ai)
            tried = list(dict.fromkeys(list(current.get("agents_tried") or []) + [agent]))
            updated = dict(current, status="ready", ai_task=ai["id"], agent=agent, agents_tried=tried,
                           attempt=current["attempt"] + 1, next_retry=None, error="")
            st.save_intake(updated)
            return updated
    finally:
        if os.path.lexists(tmp):
            shutil.rmtree(tmp, ignore_errors=True)


# submitted

def _prepare(ctx, rec):
    cfg, st = ctx.cfg, ctx.st
    started = _move(st, rec["id"], ("submitted",), status="fetching", started=rec.get("started") or stamp(ctx.now))
    if started is None:
        return
    loaded = ctx.content()
    if loaded is None:
        _move(st, rec["id"], ("fetching",), status="submitted")
        return
    try:
        intake_materials.build_inputs(cfg, st, started, loaded)
        launched = launch_ai(cfg, st, started, started["agent"], ctx.now)
    except StateLocked:
        raise
    except intake_materials.MaterialError as e:
        _fail(st, started, str(e), ctx.rep, ctx.now)
        return
    except (OSError, StateError, ValueError, UpstreamError) as e:
        _fail(st, started, "cannot prepare the task: %s" % (getattr(e, "strerror", None) or e), ctx.rep, ctx.now)
        return
    if launched is not None:
        ctx.rep.add("prepared intake %s: AI task %s (%s)" % (rec["id"], launched["ai_task"], launched["agent"]))


# the AI task

def _ai_failed(ctx, rec, ai):
    cfg, st = ctx.cfg, ctx.st
    if ai.get("class") != "model":
        _fail(st, rec, "the AI task failed: %s" % (ai.get("error") or "no reason given"), ctx.rep, ctx.now)
        return
    tried = rec.get("agents_tried") or []
    others = [a for a in cfg.intake_agents if a not in tried]
    if cfg.intake_switch_agent and others:
        switched = launch_ai(cfg, st, rec, others[0], ctx.now)
        if switched is not None:
            ctx.rep.add("intake %s: the model service of %s failed, trying %s" % (rec["id"], rec["agent"], others[0]))
        return
    retries = (rec.get("retries") or 0) + 1
    if retries > cfg.intake_model_max_retries:
        _fail(st, rec, "the model service stayed unavailable (%d retries)" % cfg.intake_model_max_retries,
              ctx.rep, ctx.now)
        return
    wake = stamp(ctx.now + cfg.intake_model_retry_minutes * 60)
    _move(st, rec["id"], ("ready", "running"), status="paused", retries=retries, next_retry=wake,
          error=intake.clean_error("the model service is unavailable: %s" % (ai.get("error") or "")))
    ctx.rep.add("paused intake %s until %s (retry %d)" % (rec["id"], wake, retries))


def _resume(ctx, rec):
    wake = epoch_of(rec.get("next_retry"))
    if wake is not None and ctx.now < wake:
        return
    launched = launch_ai(ctx.cfg, ctx.st, rec, rec["agent"], ctx.now)
    if launched is not None:
        ctx.rep.add("resumed intake %s: AI task %s" % (rec["id"], launched["ai_task"]))


def _correction(ctx, rec, feedback_name, text, previous):
    """Hand the problem back to the agent (a new AI task), or give up after max_rounds."""
    cfg, st = ctx.cfg, ctx.st
    if (rec.get("rounds") or 0) >= cfg.intake_max_rounds:
        first = text.strip().splitlines()[0] if text.strip() else "no details"
        _fail(st, rec, "not accepted after %d correction rounds: %s" % (cfg.intake_max_rounds, first), ctx.rep, ctx.now)
        return
    _save_round(st, rec, feedback_name, text, previous)
    again = _move(st, rec["id"], ("ready", "running", "verifying"), rounds=(rec.get("rounds") or 0) + 1)
    if again is None:
        return
    launched = launch_ai(cfg, st, again, again["agent"], ctx.now)
    if launched is not None:
        ctx.rep.add("intake %s: correction round %d, AI task %s" % (rec["id"], launched["rounds"], launched["ai_task"]))


def _validation_text(messages):
    return ("The files you delivered were not accepted. Fix every point below and deliver again.\n\n"
            + "\n".join("- %s" % m for m in messages) + "\n")


def _delivered(ctx, rec, ai):
    cfg, st = ctx.cfg, ctx.st
    loaded = ctx.content()
    if loaded is None:
        return
    files = intake_delivery.read_delivery(cfg, st, rec["id"], ai)
    snapshot = {k: v for k, v in files.items() if k != "deb"}
    try:
        checked = intake_delivery.validate(cfg, st, loaded, rec, files, ai_record=ai)
        if checked.is_vendor:
            _vendor_delivered(ctx, rec, checked)
            return
        verify = _start_verify(ctx, rec, checked)
    except intake_delivery.DeliveryError as e:
        _correction(ctx, rec, "validation-error.txt", _validation_text(e.messages), snapshot)
        return
    if verify is not None:
        ctx.rep.add("intake %s: verification build %s for %s %s" % (rec["id"], verify["verify_task"],
                                                                    verify["app"], verify["version"]))


def _vendor_delivered(ctx, rec, checked):
    """The trial run of a vendor.yaml delivery; success moves the task to `review` (DeliveryError: back to the agent)."""
    ai_deb = intake_delivery.load_ai_deb(ctx.st, rec["id"])
    check = intake_vendor.run(ctx.cfg, ctx.st, checked, rec, ai_deb)
    moved = _move(ctx.st, rec["id"], ("ready", "running"), status="review", app=checked.app_id, version=check["version"],
                  tag=None, commit=None, vendor_check=check, error="")
    if moved is not None:
        ctx.rep.add("intake %s is ready for review (%s %s, tracked from the vendor)" % (rec["id"], checked.app_id,
                                                                                         check["version"]))


def _start_verify(ctx, rec, checked):
    """Create the verification build (an ordinary build task with the recipe inline) and move to `verifying`."""
    cfg, st = ctx.cfg, ctx.st
    with st.lock(wait=LOCK_WAIT):
        current = st.intake(rec["id"])
        if current is None or current.get("status") not in ("ready", "running"):
            return None
        existing = [b for b in st.builds(app=checked.app_id)
                    if b.get("upstream_version") == checked.version and b.get("mode") != "ai"]
        try:
            record = builds.build_record(st, checked.app_id, checked.repo, checked.recipe, checked.version,
                                         checked.tag, checked.commit, len(existing) + 1, rec["by"], stamp(ctx.now),
                                         verify=True, intake=rec["id"], allow_hosts=list(rec.get("allow_hosts") or []))
        except builds.BuildError as e:
            raise intake_delivery.DeliveryError("recipe.yaml: %s" % e) from e
        st.save_build(record)
        updated = dict(current, status="verifying", verify_task=record["id"], app=checked.app_id,
                       version=checked.version, tag=checked.tag, commit=checked.commit, error="")
        st.save_intake(updated)
        return updated


def _follow_ai(ctx, rec):
    st = ctx.st
    ai = st.build(rec["ai_task"]) if rec.get("ai_task") else None
    if ai is None:
        _fail(st, rec, "the AI task record is missing", ctx.rep, ctx.now)
        return
    status = ai.get("status")
    if status == "building" and rec["status"] == "ready":
        _move(st, rec["id"], ("ready",), status="running")
    elif status == "failed":
        _ai_failed(ctx, rec, ai)
    elif status == "cancelled":
        _move(st, rec["id"], ("ready", "running"), status="cancelled", finished=stamp(ctx.now),
              error="the AI task was cancelled")
    elif status == "done":
        _delivered(ctx, rec, ai)


# verification

def _verify_failed(ctx, rec, verify):
    cfg, st = ctx.cfg, ctx.st
    if verify.get("class") != "recipe":
        _fail(st, rec, "the verification build failed: %s" % (verify.get("error") or "no reason given"),
              ctx.rep, ctx.now)
        return
    log = intake_materials._tail(os.path.join(builds.out_dir(cfg, verify["id"]), "build.log"),
                                 intake_materials.FAILURE_LOG_BYTES)
    text = "error: %s\n\n%s" % (debinfo.clean_text(verify.get("error") or ""), log)
    _correction(ctx, rec, "replay-failure.log", text, intake_delivery.load_snapshot(st, rec["id"]))


def _follow_verify(ctx, rec):
    st = ctx.st
    verify = st.build(rec["verify_task"]) if rec.get("verify_task") else None
    if verify is None:
        _fail(st, rec, "the verification build record is missing", ctx.rep, ctx.now)
        return
    status = verify.get("status")
    if status == "done":
        if _move(st, rec["id"], ("verifying",), status="review") is not None:
            ctx.rep.add("intake %s is ready for review (%s %s)" % (rec["id"], rec.get("app"), rec.get("version")))
    elif status == "failed":
        _verify_failed(ctx, rec, verify)
    elif status == "cancelled":
        _move(st, rec["id"], ("verifying",), status="cancelled", finished=stamp(ctx.now),
              error="the verification build was cancelled")


# the run

def _process(ctx, rec):
    status = rec["status"]
    if status in ("submitted", "fetching"):
        if status == "fetching":   # a run that died while fetching
            rec = _move(ctx.st, rec["id"], ("fetching",), status="submitted") or rec
        _prepare(ctx, dict(rec, status="submitted"))
    elif status in ("ready", "running"):
        _follow_ai(ctx, rec)
    elif status == "verifying":
        _follow_verify(ctx, rec)
    elif status == "paused":
        _resume(ctx, rec)


def _housekeeping(ctx):
    """Working files of finished tasks go after [intake] keep_days; stale fetch directories after a day."""
    cfg, st = ctx.cfg, ctx.st
    for rec in st.intakes():
        finished = epoch_of(rec.get("finished"))
        if not intake.is_terminal(rec) or finished is None or ctx.now - finished < cfg.intake_keep_days * 86400:
            continue
        path = st.intake_dir(rec["id"])
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
            ctx.rep.add("removed the working files of intake %s" % rec["id"])


def step(cfg, st, now):
    """One intake-step run (the caller holds the `intake-step` lock); `now` is epoch seconds. Returns a Report."""
    rep = builds.Report()
    ctx = Context(cfg, st, rep, now)
    for rec in [r for r in st.intakes() if r.get("status") in ACTIVE]:
        try:
            _process(ctx, rec)
        except StateLocked:
            raise
        except IntakeError as e:
            rep.fail("intake %s: %s" % (rec["id"], e))
        except StateError as e:
            rep.fail("intake %s: %s" % (rec["id"], e))
        except Exception as e:  # noqa: BLE001 - one task must not stop the others
            rep.fail("intake %s: internal error: %s" % (rec["id"], type(e).__name__))
    intake_proxy.update(cfg, st, rep)
    _housekeeping(ctx)
    return rep
