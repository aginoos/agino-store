# SPDX-License-Identifier: GPL-3.0-or-later
"""Hand-over directory for the sandbox host (docs/server.md section 10).

Every pending candidate whose sandbox check is still missing gets a pair `<app>_<sha12>.deb` +
`<app>_<sha12>.json` in paths.sandbox_queue. The JSON is written after the deb, so a JSON that exists
always has its deb. The directory is group-writable for the sandbox user, so it is only ever touched
through a directory fd, with O_NOFOLLOW, and only entries with exactly these names are removed.
An app with scenes that use demo trees also gets `<app>_<sha12>.demo.tar`, written before the JSON.
"""

import contextlib
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import time

from . import catalogwriter, demo, shotsreq

NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9-]*_[0-9a-f]{12}\.(json|deb|demo\.tar)\Z")
APP_RE = re.compile(r"\A[a-z0-9][a-z0-9-]*\Z")
TMP_PREFIX = ".tmp-sbxq-"
TMP_MAX_AGE = 3600
FILE_MODE = 0o640
CHUNK = 1 << 30


class QueueError(Exception):
    pass


def _open_dir(path):
    try:
        os.makedirs(path, mode=0o750, exist_ok=True)
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        raise QueueError("%s is a symlink or not a usable directory (%s)" % (path, e.strerror)) from e


def _lstat(dir_fd, name):
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _unlink(dir_fd, name):
    with contextlib.suppress(FileNotFoundError):
        os.unlink(name, dir_fd=dir_fd)


def _create_tmp(dir_fd):
    """A fresh exclusive temp file in the queue: (name, fd). Never reuses an existing name."""
    for _ in range(100):
        name = "%s%d-%s" % (TMP_PREFIX, os.getpid(), os.urandom(4).hex())
        try:
            return name, os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE, dir_fd=dir_fd)
        except FileExistsError:
            continue
    raise QueueError("cannot create a temp file in the queue")


def _copy(src_fd, dst_fd):
    """Copy between fds; copy_file_range lets XFS reflink, anything else falls back to a plain copy."""
    try:
        while os.copy_file_range(src_fd, dst_fd, CHUNK):
            pass
        return
    except OSError as e:
        if e.errno not in (errno.EXDEV, errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise
    os.lseek(src_fd, 0, os.SEEK_SET)
    os.lseek(dst_fd, 0, os.SEEK_SET)
    os.ftruncate(dst_fd, 0)
    with os.fdopen(os.dup(src_fd), "rb") as src, os.fdopen(os.dup(dst_fd), "wb") as dst:
        shutil.copyfileobj(src, dst, 1 << 20)


def _open_source(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise QueueError("not a regular file")
    return fd


def _put_deb(dir_fd, name, src_path):
    src_fd = _open_source(src_path)
    tmp, dst_fd = _create_tmp(dir_fd)
    try:
        try:
            _copy(src_fd, dst_fd)
            os.fchmod(dst_fd, FILE_MODE)
            os.fsync(dst_fd)
        finally:
            os.close(dst_fd)
            os.close(src_fd)
        os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        _unlink(dir_fd, tmp)
        raise


def _put_bytes(dir_fd, name, data):
    tmp, fd = _create_tmp(dir_fd)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fchmod(f.fileno(), FILE_MODE)
            os.fsync(f.fileno())
        os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        _unlink(dir_fd, tmp)
        raise


def _same_bytes(dir_fd, name, data):
    """True if name is a regular file in the queue whose content is exactly data."""
    st = _lstat(dir_fd, name)
    if st is None or not stat.S_ISREG(st.st_mode) or st.st_size != len(data):
        return False
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        with os.fdopen(fd, "rb") as f:
            have = hashlib.sha256(f.read(len(data) + 1)).digest()
    except OSError:
        return False
    return have == hashlib.sha256(data).digest()


def _put_json(dir_fd, name, doc):
    tmp, fd = _create_tmp(dir_fd)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fchmod(f.fileno(), FILE_MODE)
            os.fsync(f.fileno())
        os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        _unlink(dir_fd, tmp)
        raise


def _read_json(dir_fd, name):
    """The parsed JSON of a regular file in the queue, or None."""
    st = _lstat(dir_fd, name)
    if st is None or not stat.S_ISREG(st.st_mode) or st.st_size > 1 << 20:
        return None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        with os.fdopen(fd, "rb") as f:
            return json.loads(f.read(1 << 20).decode("utf-8"))
    except (OSError, ValueError):
        return None


def _profile(profile):
    if not profile:
        return None
    return {"debconf": list(profile.get("debconf", ())),
            "default_files": [dict(item) for item in profile.get("default_files", ())]}


def _shots_part(content, entry):
    """(JSON fields, demo tar bytes or None) for an app's shots; nothing without a shots.yaml."""
    if entry is None or entry.shots is None:
        return {}, None
    fields = {"shots": entry.shots}
    names = sorted({n for scene in entry.shots["scenes"] for n in scene["demo"]})
    if not names:
        return fields, None
    try:
        return fields, demo.build_tar(content.root, names)
    except (OSError, demo.DemoError) as e:
        raise QueueError("demo trees of %s: %s" % (entry.id, e)) from e


def _wanted(cfg, st, content):
    """{base name: (record, JSON document without 'queued', demo tar bytes or None)} for the candidates
    that need the sandbox."""
    out = {}
    for record in st.candidates(status="pending"):
        if ((record.get("checks") or {}).get("sandbox") or {}).get("status", "missing") != "missing":
            continue
        app, sha = record.get("app"), record.get("sha256")
        if not record.get("file") or not isinstance(sha, str) or not APP_RE.match(app or ""):
            continue
        entry = content.app(app) if content is not None else None
        base = "%s_%s" % (app, sha[:12])
        extra, tar = _shots_part(content, entry)
        if tar is not None:
            extra.update(demo=base + ".demo.tar", demo_size=len(tar))
        out[base] = (record, dict(extra, **{
            "schema": 1, "app": app, "version": record["version"], "sha256": sha, "size": record.get("size"),
            "package": record.get("package"), "filename": record.get("filename"), "deb": base + ".deb",
            "launchable": entry.launchable if entry else None,
            "install_profile": _profile(entry.install_profile) if entry else None}), tar)
    return out


def _shots_job(cfg, st, content, request):
    """(base, record or None, JSON document, demo tar or None) of a shots request; a string if it
    cannot be queued (the reason)."""
    app = request["app"]
    entry = content.app(app) if content is not None else None
    if entry is None or entry.shots is None:
        return "no valid shots.yaml"
    if entry.kind == "milkyway":
        found = st.milkyway().get(entry.package)
        if not isinstance(found, dict) or not found.get("version"):
            return "package %s is not in milkyway" % entry.package
        base, record = shotsreq.milkyway_base(app, entry.package, found["version"]), None
        ident = {"source": "milkyway", "version": found["version"], "sha256": None, "package": entry.package}
    else:
        record = shotsreq.newest_candidate(st, app)
        if record is None:
            return "no pending or approved candidate"
        base = "%s_%s" % (app, record["sha256"][:12])
        ident = {"source": "deb", "version": record["version"], "sha256": record["sha256"],
                 "size": record.get("size"), "package": record.get("package"),
                 "filename": record.get("filename"), "deb": base + ".deb"}
    extra, tar = _shots_part(content, entry)
    if tar is not None:
        extra.update(demo=base + ".demo.tar", demo_size=len(tar))
    return base, record, dict(extra, **ident, schema=1, mode="shots", app=app, launchable=entry.launchable,
                              install_profile=_profile(entry.install_profile) if record else None), tar


def _shots_wanted(cfg, st, content, checks, messages):
    """{base: (record, doc, tar)} of the open shots requests; closes the ones that are done."""
    out = {}
    for request in st.shots_requests():
        job = _shots_job(cfg, st, content, request)
        if isinstance(job, str):
            messages.append("skipped shots %s: %s" % (request["app"], job))
            continue
        base, record, doc, tar = job
        if shotsreq.is_done(cfg.shots_out, base):
            st.remove_shots_request(request["app"])
            messages.append("closed shots request %s (%s)" % (request["app"], base))
        elif base not in checks:   # a check job of the same candidate takes the shots itself
            out[base] = (record, doc, tar)
    return out


def _add(cfg, dir_fd, base, record, doc, tar):
    """Make base.deb, base.demo.tar and base.json current; returns 'queued', 'updated' or None."""
    deb, js, tar_name = base + ".deb", base + ".json", base + ".demo.tar"
    have = _read_json(dir_fd, js)
    deb_st = _lstat(dir_fd, deb)
    deb_ok = record is None or (deb_st is not None and stat.S_ISREG(deb_st.st_mode)
                                and deb_st.st_size == record.get("size"))
    tar_ok = _same_bytes(dir_fd, tar_name, tar) if tar is not None else _lstat(dir_fd, tar_name) is None
    if deb_ok and tar_ok and isinstance(have, dict) and {k: v for k, v in have.items() if k != "queued"} == doc:
        return None
    if not (deb_ok and tar_ok):
        _unlink(dir_fd, js)
    if not deb_ok:
        _unlink(dir_fd, tar_name)
        _unlink(dir_fd, deb)
        _put_deb(dir_fd, deb, os.path.join(cfg.downloads, record["file"]))
        tar_ok = tar is None
    if not tar_ok:
        if tar is None:
            _unlink(dir_fd, tar_name)
        else:
            _put_bytes(dir_fd, tar_name, tar)
    _put_json(dir_fd, js, dict(doc, queued=catalogwriter.now().strftime(catalogwriter.TIME_FORMAT)))
    return "queued" if have is None else "updated"


def _stale_names(dir_fd, wanted):
    names = []
    with os.scandir(dir_fd) as it:
        for entry in it:
            if NAME_RE.match(entry.name) and entry.name.split(".", 1)[0] not in wanted:
                names.append(entry.name)
            elif entry.name.startswith(TMP_PREFIX):
                st = _lstat(dir_fd, entry.name)
                if st and stat.S_ISREG(st.st_mode) and time.time() - st.st_mtime > TMP_MAX_AGE:
                    names.append(entry.name)
    # JSON first, so no JSON outlives its deb or demo tar
    return sorted(names, key=lambda n: (n.endswith((".deb", ".demo.tar")), n))


def run(cfg, st, content):
    """Bring paths.sandbox_queue in line with the pending candidates; returns one message per change."""
    messages = []
    wanted = _wanted(cfg, st, content)
    wanted.update(_shots_wanted(cfg, st, content, wanted, messages))
    if not wanted and not os.path.lexists(cfg.sandbox_queue):
        return messages
    dir_fd = _open_dir(cfg.sandbox_queue)
    try:
        for name in _stale_names(dir_fd, wanted):
            _unlink(dir_fd, name)
            if NAME_RE.match(name):
                messages.append("removed %s" % name)
        for base in sorted(wanted):
            record, doc, tar = wanted[base]
            try:
                done = _add(cfg, dir_fd, base, record, doc, tar)
            except (OSError, QueueError) as e:
                messages.append("skipped %s %s: %s" % (doc["app"], doc["version"], e))
                continue
            if done:
                messages.append("%s %s %s (%s)" % (done, doc["app"], doc["version"], base))
    finally:
        os.close(dir_fd)
    return messages
