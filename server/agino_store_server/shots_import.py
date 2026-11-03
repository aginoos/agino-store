# SPDX-License-Identifier: GPL-3.0-or-later
"""shots-import: take the newest sandbox screenshots of an app in as drafts (docs/server.md section 12)."""

import hashlib
import os
import re
import stat

from . import shots_store as S
from .shots_cmds import MAX_SOURCE_BYTES, check_app
from .status_io import ReadError, read_bytes, read_json

JOB_LIMIT = 1 << 20
MAX_SCENES = 20


def _job_stamp(shots_out, name):
    """mtime of shots_out/<name>/shots.json when <name> is a real directory holding a regular one."""
    try:
        if not stat.S_ISDIR(os.lstat(os.path.join(shots_out, name)).st_mode):
            return None
        info = os.lstat(os.path.join(shots_out, name, "shots.json"))
    except OSError:
        return None
    return info.st_mtime_ns if stat.S_ISREG(info.st_mode) else None


def newest_job(cfg, app):
    """Directory name of the newest finished job of app in paths.shots_out."""
    try:
        names = os.listdir(cfg.shots_out)
    except OSError as e:
        raise S.ShotsError("shots output directory %s: %s" % (cfg.shots_out, e.strerror)) from e
    pattern = re.compile(r"\A%s_[0-9a-f]{12}\Z" % re.escape(app))
    found = [(stamp, name) for name in names if pattern.match(name)
             for stamp in [_job_stamp(cfg.shots_out, name)] if stamp is not None]
    if not found:
        raise S.ShotsError("no finished sandbox screenshots for %s in %s" % (app, cfg.shots_out))
    return max(found)[1]


def _entries(job, doc, app):
    """[(n, scene, lang, name, declared sha256)] from shots.json plus the problems met."""
    if not isinstance(doc, dict) or doc.get("schema") != 1 or doc.get("app") != app \
            or not isinstance(doc.get("scenes"), list):
        raise S.ShotsError("%s/shots.json is not a schema 1 document for %s" % (job, app))
    out, problems = [], []
    for scene in doc["scenes"][:MAX_SCENES]:
        ident, n = (scene.get("id"), scene.get("n")) if isinstance(scene, dict) else (None, None)
        files = scene.get("files") if isinstance(scene, dict) else None
        if not (isinstance(ident, str) and S.SCENE_RE.match(ident) and isinstance(n, int)
                and not isinstance(n, bool) and 1 <= n <= 99 and isinstance(files, dict)):
            problems.append("bad scene entry in shots.json")
            continue
        for lang in S.LANGS:
            info = files.get(lang)
            if info is None:
                continue
            name = "%d-%s.png" % (n, ident)
            if not (isinstance(info, dict) and info.get("name") == name):
                problems.append("%s %s: unexpected file name in shots.json" % (lang, ident))
                continue
            out.append((n, ident, lang, name, info.get("sha256")))
    return sorted(out), problems


def _load(cfg, job, lang, name, declared):
    """Stripped bytes of one sandbox image (ShotsError when it must not be taken in)."""
    path = os.path.join(cfg.shots_out, job, lang, name)
    try:
        raw = read_bytes(path, MAX_SOURCE_BYTES)
    except ReadError as e:
        raise S.ShotsError(str(e)) from e
    if declared is not None and hashlib.sha256(raw).hexdigest() != declared:
        raise S.ShotsError("does not match the sha256 in shots.json")
    return S.check_png(cfg, raw)


def _import_one(cfg, album, job, entry):
    """('imported'|'skipped', text); ShotsError means a problem."""
    n, scene, lang, name, declared = entry
    payload, width, height = _load(cfg, job, lang, name, declared)
    sha = hashlib.sha256(payload).hexdigest()
    twin = album.duplicate(lang, sha, rejected_too=True)
    if twin is not None:
        return "skipped", "%s/%s: identical to %s" % (lang, name, twin["file"])
    same = [i for i in S.active(album.images, lang) if i["scene"] == scene]
    if same and (same[0]["status"] == "approved" or same[0]["source"] != "sandbox"):
        return "skipped", "%s/%s: scene %s already has %s (%s)" % (lang, name, scene, same[0]["file"], same[0]["status"])
    if not same and len(S.active(album.images, lang)) >= cfg.shots_max_per_lang:
        return "skipped", "%s/%s: %s already has %d images" % (lang, name, lang, cfg.shots_max_per_lang)
    record = album.store(lang, scene, payload, width, height, source="sandbox", status="draft", reviewer=None,
                         origin=job, replace=same[0]["file"] if same else None, prefer=n)
    return "imported", "%s/%s" % (lang, record["file"])


def cmd_import(cfg, args):
    app = check_app(cfg, args.app)
    job = newest_job(cfg, app)
    try:
        doc = read_json(os.path.join(cfg.shots_out, job, "shots.json"), JOB_LIMIT)
    except ReadError as e:
        raise S.ShotsError(str(e)) from e
    entries, problems = _entries(job, doc, app)
    counts = {"imported": 0, "skipped": 0}
    with S.locked(cfg, app) as album:
        try:
            for entry in entries:
                try:
                    kind, text = _import_one(cfg, album, job, entry)
                except S.ShotsError as e:
                    problems.append("%s/%s: %s" % (entry[2], entry[3], e))
                    continue
                counts[kind] += 1
                print("%s %s" % (kind, text))
        finally:
            if counts["imported"]:
                album.save()
    for line in problems:
        print("problem %s" % line)
    print("job %s: imported %d, skipped %d, problems %d" % (job, counts["imported"], counts["skipped"], len(problems)))
    return 1 if problems else 0
