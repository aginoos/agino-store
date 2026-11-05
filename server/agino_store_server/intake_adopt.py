# SPDX-License-Identifier: GPL-3.0-or-later
"""intake-adopt: accept the recipe of a task in `review` (docs/server.md section 15, contract 10.5).

Adoption only ever happens on a person's command. It validates the saved delivery again (against the live
content and the strict host list of server.conf), commits the files to the content repository (apps/<id>/
app.yaml, icon.png, build.yaml and upstreams/<id>.yaml for an import; apps/<id>/build.yaml for a repair),
pushes, and then:

  import   turns the deb of the verification build into the first candidate (origin.type built, build_job =
           the verification task), through the same checks as every candidate. If that step fails after the
           commit, the task is `adopted` without a candidate and running intake-adopt again finishes it.
  vendor   (an import delivered with vendor.yaml) commits app.yaml, icon.png, scenes and vendors/<id>.yaml; the
           first candidate is the deb the trial run fetched (origin.type https, via intake); later versions are
           found by `track` and need no agent.
  repair   requests a new build of the failed version (build-request by `auto`).

The caller holds the global state lock. Revert = revert the content commit.
"""

import os
import re
import urllib.parse

import yaml

from . import build_collect, builds, content, content_edit, content_files, debinfo, fetch, trackers
from . import intake, intake_delivery, intake_vendor
from .intake import IntakeError
from .state import StateError

DUMP = content_edit.DUMP
COMMIT_ERRORS = (content_files.CommitError,)
MODES = ("store", "vendor")
MAX_PAGE = 2048
PAGE_FORBIDDEN_RE = re.compile(r"[\x00-\x20\x7f-\x9f\\<>\"'`{}|^]")
CANDIDATE_ERRORS = build_collect.COLLECT_ERRORS + (trackers.TrackError, debinfo.DebError)


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


def check_page(page):
    """The download page of a vendor distribution: one https URL, no credentials, at most 2048 characters."""
    try:
        parts = urllib.parse.urlsplit(page)
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError as e:
        raise intake.IntakeUsage("bad --download-page: not a valid URL") from e
    if len(page) > MAX_PAGE or PAGE_FORBIDDEN_RE.search(page) or parts.scheme != "https" or not parts.hostname \
            or parts.username or parts.password:
        raise intake.IntakeUsage("bad --download-page: one https URL without credentials, at most %d characters" % MAX_PAGE)
    return page


def distribution_value(mode, page, direct):
    """The `distribution` mapping the options ask for, or None when none was asked. IntakeUsage for a bad mix."""
    if mode is None:
        if page or direct:
            raise intake.IntakeUsage("--download-page and --direct need --distribution vendor")
        return None
    if mode not in MODES:
        raise intake.IntakeUsage("--distribution must be store or vendor")
    if mode == "store":
        if page or direct:
            raise intake.IntakeUsage("--distribution store takes no --download-page or --direct")
        return {"mode": "store"}
    if not page:
        raise intake.IntakeUsage("--distribution vendor needs --download-page URL (the vendor's download page or link)")
    return {"mode": "vendor", "page": check_page(page), "direct": bool(direct)}


def _require_distribution(checked, rec, dist):
    """A proprietary application is never published by accident: the administrator has to choose."""
    if not checked.is_vendor and dist is not None and dist["mode"] == "vendor":
        # The store builds this deb itself from the recipe: what the vendor's page offers is another file than the one
        # that was tested, so sending the user there makes no sense. (content.load refuses it too, after the commit.)
        raise IntakeError("%s is built from a recipe by the store: --distribution vendor does not apply (the user would "
                          "download a different file from the vendor). Use --distribution store, or import the "
                          "vendor's own deb through the vendor path (vendor.yaml)" % checked.app_id)
    if (checked.app_data or {}).get("proprietary") is True and dist is None:
        if checked.is_vendor:
            raise IntakeError("%s is proprietary software: say how it is published with --distribution store (the "
                              "store hosts the deb) or --distribution vendor --download-page URL [--direct] (the user "
                              "downloads it from the vendor)" % checked.app_id)
        raise IntakeError("%s is proprietary software built from a recipe: confirm with --distribution store that the "
                          "store may host the deb it builds" % checked.app_id)


def _page_note(cfg, checked, rec, dist):
    """A note when the page is on a host that neither the vendor tracking nor the submitted address mentions."""
    if not dist or dist["mode"] != "vendor":
        return []
    known = intake_delivery.approved_hosts(cfg, rec) | {h.lower() for h in (checked.tracking or {}).get("allowed_hosts", ())}
    host = intake_vendor.page_host(dist["page"])
    if host in known:
        return []
    return ["note: the download page is on %s, which is neither the submitted address nor a host of the vendor "
            "tracking; check that it is the vendor's own page" % host]


def files_to_commit(cfg, rec, checked, auto_task, dist=None):
    """{relative path: bytes} of the adoption, and a list of notes for the person. `dist` is the distribution
    mapping to write into app.yaml (None: leave the key out, the store hosts the deb)."""
    app = checked.app_id
    notes = []
    if checked.is_vendor:
        if auto_task:
            notes.append("--auto-task ignored: a vendor application is always tracked")
        return _vendor_files(checked, dist), notes
    files = {"apps/%s/build.yaml" % app: _dump(checked.recipe_data)}
    if rec["kind"] == "import":
        files["apps/%s/app.yaml" % app] = _dump(_with_distribution(checked.app_data, dist))
        files["apps/%s/icon.png" % app] = intake_delivery.placeholder_icon()
        if checked.shots_data is not None:
            files["apps/%s/shots.yaml" % app] = _dump(checked.shots_data)
        files.update(checked.demo_files or {})
        if checked.upstream_data is not None:
            files["upstreams/%s.yaml" % app] = _dump(dict(checked.upstream_data, auto_task=bool(auto_task)))
        elif auto_task:
            notes.append("--auto-task ignored: the recipe has no tracking file (upstream.yaml)")
    elif auto_task:
        current = _upstream_file(cfg, app)
        if current is not None:
            files["upstreams/%s.yaml" % app] = _dump(dict(current, auto_task=True))
    return files, notes


def _with_distribution(app_data, dist):
    return dict(app_data, distribution=dict(dist)) if dist else app_data


def _vendor_files(checked, dist):
    app = checked.app_id
    files = {"apps/%s/app.yaml" % app: _dump(_with_distribution(checked.app_data, dist)),
             "apps/%s/icon.png" % app: intake_delivery.placeholder_icon(),
             "vendors/%s.yaml" % app: _dump(checked.vendor_data)}
    if checked.shots_data is not None:
        files["apps/%s/shots.yaml" % app] = _dump(checked.shots_data)
    files.update(checked.demo_files or {})
    return files


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


def _trial_deb(cfg, rec):
    """(vendor_check, stored deb path) of a vendor import; IntakeError when the deb is not there."""
    check = rec.get("vendor_check")
    if not isinstance(check, dict):
        raise IntakeError("the task has no trial run of the vendor tracking")
    path = fetch.existing_download(cfg.downloads, check.get("sha256"), check.get("filename"))
    if path is None:
        raise IntakeError("the deb of the trial run is missing from the downloads")
    return check, path


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


def make_vendor_candidate(cfg, st, rec, now):
    """The first candidate of an adopted vendor import: the deb the trial run fetched, through trackers.ingest
    (inspect, diff, dependency check). It carries origin.via == intake, which approve holds to the strict rules.
    A candidate that `track` made meanwhile for the same file gets the mark too."""
    check, path = _trial_deb(cfg, rec)
    view = content.tracking_view(_load_strict(cfg))
    try:
        target = trackers.target_for(view, rec["app"])
        dl = fetch.Download(check["url"], path, check["sha256"], check["size"], check["filename"], None, None)
        origin = {"type": "https", "url": check["url"], "via": "intake", "intake": rec["id"]}
        candidate = trackers.ingest(cfg, st, target, dl, check["url"], origin, check["version"])
        if (candidate.get("origin") or {}).get("via") != "intake" and candidate.get("status") == "pending":
            candidate = st.update_candidate(candidate, {"origin": dict(candidate.get("origin") or {}, via="intake",
                                                                       intake=rec["id"])})
    except CANDIDATE_ERRORS as e:
        raise IntakeError("the candidate could not be made: %s" % builds.clean_error(e)) from e
    name = os.path.basename(candidate["_path"])[:-5]
    st.save_intake(dict(st.intake(rec["id"]), candidate=name, error=""))
    return name


def _finish_import(cfg, st, rec, now):
    """(line, ok): the first candidate of an adopted import, or why it could not be made."""
    try:
        name = (make_vendor_candidate if rec.get("vendor_check") else make_candidate)(cfg, st, rec, now)
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


def _check_options(rec, distribution, download_page, direct):
    """The distribution mapping the options ask for (None: none given); IntakeUsage for bad options."""
    dist = distribution_value(distribution, download_page, direct)
    if dist is not None and rec["kind"] != "import":
        raise intake.IntakeUsage("--distribution applies to an import, not to a repair")
    return dist


def adopt(cfg, st, intake_id, by, auto_task=False, now=None, distribution=None, download_page=None, direct=False):
    """intake-adopt. Returns (lines to print, ok); ok is False when the recipe is adopted but the candidate could
    not be made. IntakeError / IntakeUsage when nothing could be done. `distribution` ("store"/"vendor"),
    `download_page` and `direct` say how the application is published (docs/server.md 15.5); a proprietary
    application cannot be adopted without a choice."""
    builds.check_by(by)
    intake.check_id(intake_id)
    rec = st.intake(intake_id)
    if rec is None:
        raise intake.IntakeUsage("unknown intake task %s" % intake_id)
    now = now or debinfo.utc_now()
    dist = _check_options(rec, distribution, download_page, direct)
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
        _require_distribution(checked, rec, dist)
        # fail before anything is committed
        if checked.is_vendor:
            _trial_deb(cfg, rec)
        else:
            _verify_artifact(cfg, st, rec)
    files, notes = files_to_commit(cfg, rec, checked, auto_task, dist)
    notes = notes + _page_note(cfg, checked, rec, dist)
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
