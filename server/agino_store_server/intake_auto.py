# SPDX-License-Identifier: GPL-3.0-or-later
"""Automatic repair tasks (contract ai-intake 10.5, "repair 的自动创建").

build-step calls create_repairs after it has collected results: an ordinary build task of a tracked app that
failed with `class` recipe (a step, a `test` command, the packaging or a source of the recipe failed; not a
timeout, not the VM) opens a repair intake task by `auto` for the AI, at most [intake] max_auto_repairs times
per (app, version). Each failed task is looked at once (`repair_considered`). Adopting the repair re-requests
the build (intake_adopt).
"""

import datetime

from . import content, intake
from .state import StateError

LOCK_WAIT = 60.0


def _eligible(record):
    return (record.get("status") == "failed" and record.get("class") == "recipe" and record.get("mode") != "ai"
            and not record.get("verify") and not record.get("repair_considered"))


def _mark(st, build_id):
    with st.lock(wait=LOCK_WAIT):
        fresh = st.build(build_id)
        if fresh is not None and not fresh.get("repair_considered"):
            st.save_build(dict(fresh, repair_considered=True))


def _open(cfg, st, loaded, record, rep, now):
    app, version = record.get("app"), record.get("upstream_version")
    if loaded.recipe(app) is None or loaded.upstream(app) is None:
        return
    with st.lock(wait=LOCK_WAIT):
        if intake.auto_repairs(st, app, version) >= cfg.intake_max_auto_repairs:
            rep.add("no repair task for %s %s: %d automatic repairs were made already; an administrator can ask "
                    "for another" % (app, version, cfg.intake_max_auto_repairs))
            return
        try:
            made = intake.create_repair(cfg, st, loaded, app, version, intake.AUTO_USER, now=_stamp(now),
                                        failed_task=record)
        except (intake.IntakeError, intake.IntakeUsage, StateError) as e:
            rep.add("no repair task for %s %s: %s" % (app, version, e))
            return
    rep.add("repair task %s opened for %s %s (the build %s failed in its recipe)" % (made["id"], app, version,
                                                                                    record["id"]))


def _stamp(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_repairs(cfg, st, rep, now):
    """Look at every failed build task not looked at yet; `rep` is the build-step Report."""
    if not cfg.intake_auto_repair:
        return
    todo = [r for r in st.builds() if _eligible(r)]
    if not todo:
        return
    try:
        loaded = content.load(cfg.content, allow_insecure=cfg.allow_insecure, policy=cfg.build_policy())
    except content.ContentError:
        rep.add("no repair tasks: store-content is invalid")
        return
    for record in todo:
        _open(cfg, st, loaded, record, rep, now)
        _mark(st, record["id"])
