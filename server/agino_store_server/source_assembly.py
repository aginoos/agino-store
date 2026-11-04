# SPDX-License-Identifier: GPL-3.0-or-later
"""The source.tar of a recipe v2 task (docs/server.md section 14.7, contract ai-intake 10.1).

Every entry of `sources` is fetched on the host by the controlled fetcher (upstream_fetch / fetch): a git
source is pinned to a commit, a tarball or file source must match its sha256 or the task fails. The result is
one plain tar whose top level holds one directory per source, `<name>/`:

  git      the tree of the pinned commit
  tarball  the unpacked archive (the single top-level directory removed)
  file     the file as downloaded, stored as `<name>/<file name>`

The `files` of the recipe are not part of this tar: they travel in task.json and the runner writes them next
to the source directories. Nothing is extracted or executed here; every part goes through the same tar
hardening as a v1 source (upstream_fetch.normalise_tar).
"""

import dataclasses
import os
import re
import shutil
import tarfile
import urllib.parse

from . import fetch, upstream_fetch
from .upstream_fetch import UpstreamError

HOST_RE = re.compile(r"\A[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?\Z")


def hosts_for(cfg, record):
    """The allowed hosts for one task: [upstream] allowed_hosts plus the task-level extra hosts."""
    extra = [h for h in record.get("allow_hosts") or [] if isinstance(h, str) and HOST_RE.match(h)]
    return tuple(dict.fromkeys(tuple(cfg.upstream_allowed_hosts) + tuple(extra)))


def _dir_info(name):
    info = tarfile.TarInfo(name)
    info.type, info.mode, info.uid, info.gid = tarfile.DIRTYPE, 0o755, 0, 0
    return info


def _add_tar(out, src_tar, name):
    """Copy every member of the normalised tar `src_tar` below the directory `name`."""
    out.addfile(_dir_info(name))
    with tarfile.open(src_tar, "r:*") as tar:
        for member in tar:
            out.addfile(member.replace(name="%s/%s" % (name, member.name), deep=False),
                        tar.extractfile(member) if member.isreg() else None)


def _add_file(out, path, name, file_name):
    out.addfile(_dir_info(name))
    info = tarfile.TarInfo("%s/%s" % (name, file_name))
    info.type, info.mode, info.uid, info.gid = tarfile.REGTYPE, 0o644, 0, 0
    info.size = os.path.getsize(path)
    with open(path, "rb") as f:
        out.addfile(info, f)


def _file_name(url):
    return fetch.sanitize_filename(os.path.basename(urllib.parse.urlsplit(url).path) or "download")


def _one(cfg, source, pin, work, remaining):
    """(kind of part, path, info) for one source; the part is a normalised tar or a downloaded file."""
    name, kind = source["name"], source["kind"]
    part = os.path.join(work, name)
    os.mkdir(part, 0o700)
    if kind == "git":
        raw, tar = os.path.join(part, "raw.tar"), os.path.join(part, "part.tar")
        commit = upstream_fetch.git_checkout_tar(cfg, source["url"], source["ref"], pin, part, raw, remaining)
        upstream_fetch.normalise_tar(raw, tar, remaining)
        os.unlink(raw)
        return "tar", tar, {"kind": "git", "commit": commit}
    path = upstream_fetch.download_verified(cfg, source["url"], source["sha256"], _file_name(source["url"]),
                                            part, remaining)
    info = {"kind": kind, "sha256": source["sha256"]}
    if kind == "tarball":
        tar = os.path.join(part, "part.tar")
        upstream_fetch.normalise_tar(path, tar, remaining, strip_top=True)
        return "tar", tar, info
    return "file", path, info


def fetch_sources(cfg, record, workdir, dest):
    """Fetch every source of the task record's recipe into the plain tar file `dest`.

    `record["recipe"]["sources"]` has its placeholders replaced already; `record["pins"]` maps a git source
    name to the commit the task is pinned to (absent: the commit found now is taken). `workdir` is an existing
    private directory (everything created in it is removed again). Returns (sha256, size, fetched) where
    `fetched` is {name: {"kind", "commit" or "sha256"}}.
    """
    sources = (record.get("recipe") or {}).get("sources") or []
    if not sources:
        raise UpstreamError("the recipe has no sources")
    limited = dataclasses.replace(cfg, upstream_allowed_hosts=hosts_for(cfg, record))
    max_bytes = cfg.builds_max_source_mib << 20
    work = os.path.join(workdir, "work")
    os.mkdir(work, 0o700)
    fetched, used = {}, 0
    try:
        with tarfile.open(dest, "w", format=tarfile.PAX_FORMAT) as out:
            for source in sources:
                kind, path, info = _one(limited, source, (record.get("pins") or {}).get(source["name"]), work,
                                        max(max_bytes - used, 1))
                if kind == "tar":
                    _add_tar(out, path, source["name"])
                else:
                    _add_file(out, path, source["name"], os.path.basename(path))
                used += os.path.getsize(path)
                if used > max_bytes:
                    raise UpstreamError("the sources exceed the size limit")
                fetched[source["name"]] = info
        upstream_fetch.verify_tar(dest, max_bytes)
        if os.path.getsize(dest) > max_bytes:
            raise UpstreamError("the sources exceed the size limit")
    except (tarfile.TarError, UnicodeError, ValueError) as e:
        _discard(dest)
        raise UpstreamError("the source archive is not usable (%s)" % type(e).__name__) from e
    except BaseException:
        _discard(dest)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return fetch.file_sha256(dest), os.path.getsize(dest), fetched


def _discard(path):
    if os.path.lexists(path):
        os.unlink(path)


def pin_map(recipe_sources, repo, tag, commit):
    """{source name: commit} for the git sources that are the tracked upstream at its tag (or a full commit id)."""
    pins = {}
    for source in recipe_sources:
        if source.get("kind") != "git":
            continue
        ref = source.get("ref") or ""
        if upstream_fetch.COMMIT_RE.match(ref):
            pins[source["name"]] = ref
        elif repo and commit and source.get("url") == repo and ref == tag:
            pins[source["name"]] = commit
    return pins
