# SPDX-License-Identifier: GPL-3.0-or-later
"""intake-adopt: accept the recipe of a task in `review` (docs/server.md section 15, contract 10.5).

Adoption only ever happens on a person's command. It validates the saved delivery again (against the live
content and the strict host list of server.conf), commits the files to the content repository (apps/<id>/
app.yaml, icon.png, build.yaml and upstreams/<id>.yaml for an import; apps/<id>/build.yaml for a repair),
pushes, and then:

  import   turns the deb of the verification build into the first candidate (origin.type built, build_job =
           the verification task), through the same checks as every candidate. If that step fails after the
           commit, the task is `adopted` without a candidate and running intake-adopt again finishes it.
  repair   requests a new build of the failed version (build-request by `auto`).

The caller holds the global state lock. Revert = revert the content commit.
"""

import os

import yaml

from . import build_collect, builds, content, content_edit, content_files, debinfo, fetch
from . import intake, intake_delivery
from .intake import IntakeError
from .state import StateError

DUMP = content_edit.DUMP
COMMIT_ERRORS = (content_files.CommitError,)


def _dump(data):
    return yaml.safe_dump(data, **DUMP).encode("utf-8")


def _load_strict(cfg):
    try:
        return content.load(cfg.content, allow_insecure=cfg.allow_insecure, policy=cfg.build_policy())
    except content.ContentError as e:
        raise IntakeError("store-content is invalid:\n%s" % e) from e


def _upstream_file(cfg, app_id):
    path = os.path.join(cfg.content, "upstreams", app_id + ".yaml")
    try:
        with open(path, "rb") as f:
            data = yaml.safe_load(f.read(1 << 20))
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def files_to_commit(cfg, rec, checked, auto_task):
    """{relative path: bytes} of the adoption, and a list of notes for the person."""
    app = checked.app_id
    files, notes = {"apps/%s/build.yaml" % app: _dump(checked.recipe_data)}, []
    if rec["kind"] == "import":
        files["apps/%s/app.yaml" % app] = _dump(checked.app_data)
        files["apps/%s/icon.png" % app] = intake_delivery.placeholder_icon()
        if checked.shots_data is not None:
            files["apps/%s/shots.yaml" % app] = _dump(checked.shots_data)
        if checked.upstream_data is not None:
            files["upstreams/%s.yaml" % app] = _dump(dict(checked.upstream_data, auto_task=bool(auto_task)))
        elif auto_task:
            notes.append("--auto-task ignored: the recipe has no tracking file (upstream.yaml)")
    elif auto_task:
        current = _upstream_file(cfg, app)
        if current is not None:
            files["upstreams/%s.yaml" % app] = _dump(dict(current, auto_task=True))
    return files, notes


def _verify_artifact(cfg, st, rec):
    """(verification task record, stored deb path) of an import; IntakeError when the deb is not there."""
    verify = st.build(rec["verify_task"]) if rec.get("verify_task") else None
    art = (verify or {}).get("artifact")
    if verify is None or verify.get("status") != "done" or not isinstance(art, dict):
        raise IntakeError("the verification build has no deb to take in")
    path = fetch.existing_download(cfg.downloads, art.get("sha256"), art.get("file"))
    if path is None:
        raise IntakeError("the deb of the verification build is missing from the downloads")
    return verify, path, art


def make_candidate(cfg, st, rec, now):
    """The first candidate of an adopted import, from the saved deb of the verification build."""
    verify, path, art = _verify_artifact(cfg, st, rec)
    try:
        artifact, name = build_collect.candidate_from_deb(cfg, st, verify, path, art["sha256"], art["size"])
    except build_collect.COLLECT_ERRORS as e:
        raise IntakeError("the candidate could not be made: %s" % builds.clean_error(e)) from e
    st.save_build(dict(verify, candidate=name, artifact=artifact))
    st.save_intake(dict(st.intake(rec["id"]), candidate=name, error=""))
    return name


def _finish_import(cfg, st, rec, now):
    """(line, ok): the first candidate of an adopted import, or why it could not be made."""
    try:
        name = make_candidate(cfg, st, rec, now)
    except (IntakeError, StateError) as e:
        text = intake.clean_error(e)
        st.save_intake(dict(st.intake(rec["id"]), error="no candidate: %s" % text))
        return "error: %s; the recipe is adopted, run intake-adopt again to retry the candidate" % text, False
    return "candidate %s of %s is pending (sandbox test and approval follow)" % (name, rec["app"]), True


def _rebuild(cfg, st, rec, now):
    """After a repair: build the failed version again with the adopted recipe."""
    of = rec["repair_of"]
    try:
        made = builds.request(cfg, st, _load_strict(cfg), of["app"], of["version"], intake.AUTO_USER, now=now)
    except (builds.BuildError, builds.BuildUsage, StateError, IntakeError) as e:
        return "no new build was requested: %s (request it with build-request)" % debinfo.clean_text(e)
    return "build task %s requested for %s %s" % (made["id"], of["app"], of["version"])


def adopt(cfg, st, intake_id, by, auto_task=False, now=None):
    """intake-adopt. Returns (lines to print, ok); ok is False when the recipe is adopted but the candidate could
    not be made. IntakeError / IntakeUsage when nothing could be done."""
    builds.check_by(by)
    intake.check_id(intake_id)
    rec = st.intake(intake_id)
    if rec is None:
        raise intake.IntakeUsage("unknown intake task %s" % intake_id)
    now = now or debinfo.utc_now()
    if rec["status"] == "adopted" and rec["kind"] == "import" and not rec.get("candidate"):
        line, ok = _finish_import(cfg, st, rec, now)
        return ["finishing the adoption of %s" % intake_id, line], ok
    if rec["status"] != "review":
        raise IntakeError("intake task %s is %s; only a task in review can be adopted" % (intake_id, rec["status"]))
    loaded = _load_strict(cfg)
    pinned = {"version": rec.get("version"), "tag": rec.get("tag"), "commit": rec.get("commit")}
    try:
        checked = intake_delivery.validate(cfg, st, loaded, rec, intake_delivery.load_snapshot(st, intake_id),
                                           pinned=pinned)
    except intake_delivery.DeliveryError as e:
        raise IntakeError("the delivery no longer validates:\n%s" % "\n".join(e.messages)) from e
    if rec["kind"] == "import":
        _verify_artifact(cfg, st, rec)   # fail before anything is committed
    files, notes = files_to_commit(cfg, rec, checked, auto_task)
    subject = ("add %s" % checked.app_id) if rec["kind"] == "import" else (
        "recipe %d of %s" % (checked.recipe_data["recipe_version"], checked.app_id))
    try:
        warnings = content_files.commit_files(cfg, files, "intake(%s): %s by %s" % (checked.app_id, subject, by))
    except COMMIT_ERRORS as e:
        raise IntakeError(str(e)) from e
    lines = ["adopted %s: %s" % (intake_id, ", ".join(sorted(files)))] + notes + ["warning: %s" % w for w in warnings]
    adopted = dict(rec, status="adopted", finished=now, app=checked.app_id, error="", adopted_by=by)
    st.save_intake(adopted)
    if rec["kind"] == "import":
        line, ok = _finish_import(cfg, st, adopted, now)
    else:
        line, ok = _rebuild(cfg, st, adopted, now), True
    return lines + [line], ok
