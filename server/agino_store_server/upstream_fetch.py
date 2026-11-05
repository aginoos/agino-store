# SPDX-License-Identifier: GPL-3.0-or-later
"""Controlled fetching of upstream sources (docs/server.md section 14, contract 7.4).

Everything that comes from an upstream is hostile. This module only ever lists tags with
`git ls-remote`, downloads one pinned commit (or one tarball with a known sha256) into a private
temporary directory, and repacks it into a normalised `source.tar` through the tarfile module
without extracting anything. The host never runs a program that came from the source: git runs with
a scrubbed environment, no hooks, no filters, no submodules, https only, no redirects and rlimits.
"""

import dataclasses
import os
import re
import shutil
import signal
import tarfile
import urllib.parse

from . import debinfo, fetch

GIT = "git"
SAFE_PATH = "/usr/bin:/bin"
MIN_HOST_INTERVAL = 2.0
LS_REMOTE_TIMEOUT = 120
FETCH_TIMEOUT = 900
ARCHIVE_TIMEOUT = 300
LS_REMOTE_MAX_BYTES = 8 << 20
LS_REMOTE_FSIZE = 1 << 20
MAX_TAGS = 50000
MAX_MEMBERS = 500_000
MAX_NAME_BYTES = 4096
MAX_COMPONENT_BYTES = 255
MAX_SYMLINK_HOPS = 40
MAX_MESSAGE = 300
TAG_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+/-]{0,199}\Z")
LS_LINE_RE = re.compile(r"\A([0-9a-f]{40}|[0-9a-f]{64})\trefs/tags/([^\x00-\x20\x7f-\x9f]+?)(\^\{\})?\Z")
COMMIT_RE = re.compile(r"\A([0-9a-f]{40}|[0-9a-f]{64})\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class UpstreamError(Exception):
    """A fetch failed; the message is already cleaned of control characters.

    `klass` tells build-step whose fault it is: "infra" (network, the upstream host) or "recipe" (what the
    recipe points at is gone or no longer matches its sha256), see RecipeSourceError.
    """

    klass = "infra"


class TooLarge(UpstreamError):
    """What was asked for is bigger than the size limit of this fetch (not a failure of the network or the host)."""


class RecipeSourceError(UpstreamError):
    """The recipe's own source is missing or does not match the pinned checksum: a repair can fix it."""

    klass = "recipe"


def _clean(value):
    return debinfo.clean_text(value).strip()[:MAX_MESSAGE]


# git

def _git_config(cfg):
    protocols = ["protocol.allow=never", "protocol.https.allow=always"]
    if cfg.allow_insecure:   # tests only: local repositories and plain http
        protocols += ["protocol.file.allow=always", "protocol.http.allow=always"]
    return protocols + [
        "core.fsmonitor=false", "core.hooksPath=/dev/null", "http.followRedirects=false", "http.sslVerify=true",
        "http.userAgent=%s" % cfg.user_agent, "credential.helper=", "transfer.fsckObjects=true",
        "fetch.fsckObjects=true", "fetch.recurseSubmodules=false", "submodule.recurse=false", "gc.auto=0",
        "core.askPass=/bin/false"]


def _git_env(cfg, home):
    return {"PATH": SAFE_PATH, "HOME": home, "XDG_CONFIG_HOME": home, "LANG": "C", "LC_ALL": "C",
            "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_ATTR_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0",
            "GIT_ALLOW_PROTOCOL": "https:file:http" if cfg.allow_insecure else "https"}


def _git(cfg, args, cwd, home, timeout, max_stdout=1 << 16, fsize=1 << 20):
    argv = [GIT]
    for item in _git_config(cfg):
        argv += ["-c", item]
    try:
        proc = debinfo.run_tool(argv + list(args), timeout=timeout, max_stdout=max_stdout, env=_git_env(cfg, home),
                                cwd=cwd, fsize=fsize)
    except debinfo.DebError as e:
        raise UpstreamError("git %s: %s" % (args[0], _clean(e))) from e
    if proc.returncode == -signal.SIGXFSZ:    # the file size limit (fsize) stopped git: the output is too big
        raise TooLarge("git %s: the output is larger than the limit" % args[0])
    if proc.returncode != 0:
        lines = proc.stderr.decode("utf-8", "replace").splitlines()
        detail = _clean(lines[-1]) if lines else ""
        if _hit_size_limit(cwd, fsize):
            raise TooLarge("git %s: the repository is larger than the limit" % args[0])
        raise UpstreamError("git %s failed (exit %d)%s" % (args[0], proc.returncode, ": " + detail if detail else ""))
    return proc.stdout.decode("utf-8", "replace")


def _hit_size_limit(cwd, fsize):
    """True when a pack file git left in `cwd` is as big as the file size limit it ran under: git's child process
    (index-pack) was stopped by SIGXFSZ and git itself only reports "index-pack failed" (exit 128). The size of the
    leftover file is the evidence; the wording of the message is not used."""
    pack_dir = os.path.join(cwd, ".git", "objects", "pack")
    try:
        names = os.listdir(pack_dir)
    except OSError:
        return False
    for name in names:
        try:
            if os.lstat(os.path.join(pack_dir, name)).st_size >= fsize:
                return True
        except OSError:
            continue
    return False


def check_repo(cfg, repo):
    """The repo URL must be https on an allowed host (or local, in tests); paces requests to the host."""
    try:
        fetch.check_url(repo, frozenset(cfg.upstream_allowed_hosts), cfg.allow_insecure)
    except fetch.FetchError as e:
        raise UpstreamError(_clean(e)) from e
    interval = 0.0 if cfg.allow_insecure else max(cfg.min_interval_seconds, MIN_HOST_INTERVAL)
    fetch.throttle(urllib.parse.urlsplit(repo).hostname, interval)


def parse_ls_remote(text):
    """{tag: commit} from `git ls-remote --tags` output; annotated tags give the peeled commit. Odd lines are skipped."""
    direct, peeled = {}, {}
    for line in text.splitlines():
        found = LS_LINE_RE.match(line)
        if not found:
            continue
        commit, tag, is_peeled = found.group(1), found.group(2), bool(found.group(3))
        if TAG_RE.match(tag) and ".." not in tag and "//" not in tag:
            (peeled if is_peeled else direct)[tag] = commit
    if len(direct) > MAX_TAGS:
        raise UpstreamError("the repository has more than %d tags" % MAX_TAGS)
    return {tag: peeled.get(tag, commit) for tag, commit in direct.items()}


def list_tags(cfg, repo, workdir):
    """{tag: commit} of an upstream repository. `workdir` is an existing private directory."""
    check_repo(cfg, repo)
    out = _git(cfg, ["ls-remote", "--tags", repo], workdir, workdir, LS_REMOTE_TIMEOUT,
               max_stdout=LS_REMOTE_MAX_BYTES, fsize=LS_REMOTE_FSIZE)
    return parse_ls_remote(out)


def git_checkout_tar(cfg, repo, ref, pin, work, raw_tar, max_bytes):
    """Shallow-fetch `ref` (a tag name or a full commit id) of `repo` and write `git archive` of it to `raw_tar`.

    `pin` is the commit the task is fixed to (None: not known yet, the commit found is accepted). Returns the
    commit that was archived. UpstreamError if the tag moved; RecipeSourceError if the ref does not exist.
    """
    if COMMIT_RE.match(ref or ""):
        refspec, rev = ref, ref + "^{commit}"
    elif TAG_RE.match(ref or "") and ".." not in ref:
        refspec, rev = "+refs/tags/%s:refs/tags/%s" % (ref, ref), "refs/tags/%s^{commit}" % ref
    else:
        raise UpstreamError("bad tag or commit in the task")
    if pin is not None and not COMMIT_RE.match(pin):
        raise UpstreamError("bad tag or commit in the task")
    check_repo(cfg, repo)
    work_repo = os.path.join(work, "repo")
    _git(cfg, ["init", "--quiet", "--template=", work_repo], work, work, 60)
    try:
        _git(cfg, ["fetch", "--quiet", "--depth", "1", "--no-tags", "--no-recurse-submodules", repo, refspec],
             work_repo, work, FETCH_TIMEOUT, fsize=max_bytes)
    except UpstreamError as e:
        if "couldn't find remote ref" in str(e) or "not our ref" in str(e):
            raise RecipeSourceError(str(e)) from e
        raise
    head = _git(cfg, ["rev-parse", "--verify", "--quiet", rev], work_repo, work, 60).strip()
    if pin is not None and head != pin:
        raise UpstreamError("tag %s now points to %s, the task is pinned to %s" % (ref, head[:12], pin[:12]))
    _git(cfg, ["archive", "--format=tar", "--output=" + raw_tar, head], work_repo, work, ARCHIVE_TIMEOUT,
         fsize=max_bytes * 2)
    return head


def git_head_tar(cfg, repo, work, raw_tar, max_bytes):
    """Shallow-fetch the default branch of `repo` and write `git archive` of it to `raw_tar`; returns the commit.
    Reading material for an AI agent only: nothing is built from it."""
    check_repo(cfg, repo)
    work_repo = os.path.join(work, "repo")
    _git(cfg, ["init", "--quiet", "--template=", work_repo], work, work, 60)
    _git(cfg, ["fetch", "--quiet", "--depth", "1", "--no-tags", "--no-recurse-submodules", repo, "HEAD"],
         work_repo, work, FETCH_TIMEOUT, fsize=max_bytes)
    head = _git(cfg, ["rev-parse", "--verify", "--quiet", "FETCH_HEAD^{commit}"], work_repo, work, 60).strip()
    if not COMMIT_RE.match(head):
        raise UpstreamError("the repository has no default branch to read")
    _git(cfg, ["archive", "--format=tar", "--output=" + raw_tar, head], work_repo, work, ARCHIVE_TIMEOUT,
         fsize=max_bytes * 2)
    return head


def git_tag_diffstat(cfg, repo, old_tag, new_tag, work, max_bytes=1 << 20):
    """`git diff --stat` between two tags of `repo` (both fetched shallow; the trees are compared). Text."""
    for tag in (old_tag, new_tag):
        if not TAG_RE.match(tag or "") or ".." in tag:
            raise UpstreamError("bad tag")
    check_repo(cfg, repo)
    work_repo = os.path.join(work, "diff-repo")
    _git(cfg, ["init", "--quiet", "--template=", work_repo], work, work, 60)
    for tag in (old_tag, new_tag):
        _git(cfg, ["fetch", "--quiet", "--depth", "1", "--no-tags", "--no-recurse-submodules", repo,
                   "+refs/tags/%s:refs/tags/%s" % (tag, tag)], work_repo, work, FETCH_TIMEOUT, fsize=64 << 20)
    return _git(cfg, ["diff", "--stat", "--no-color", "--no-ext-diff", "refs/tags/%s" % old_tag,
                      "refs/tags/%s" % new_tag], work_repo, work, 120, max_stdout=max_bytes)


def _git_source(cfg, repo, tag, commit, work, raw_tar, max_bytes):
    if not TAG_RE.match(tag or "") or not COMMIT_RE.match(commit or ""):
        raise UpstreamError("bad tag or commit in the task")
    git_checkout_tar(cfg, repo, tag, commit, work, raw_tar, max_bytes)


def download_verified(cfg, url, sha256, filename, work, max_bytes):
    """Download `url` (https, every hop on an allowed host) into a private directory below `work` and check its
    sha256. Returns the path. RecipeSourceError when the content is not the pinned one or has gone."""
    interval = 0.0 if cfg.allow_insecure else max(cfg.min_interval_seconds, MIN_HOST_INTERVAL)
    fetcher = fetch.Fetcher(dataclasses.replace(cfg, min_interval_seconds=interval), cfg.upstream_allowed_hosts)
    try:
        got = fetcher.download(url, os.path.join(work, "download-" + sha256[:12]), filename=filename,
                               max_bytes=max_bytes, expected_sha256=sha256)
    except fetch.FetchError as e:
        text = _clean(e)
        if "sha256 mismatch" in text or text.startswith(("HTTP 404", "HTTP 410")):
            raise RecipeSourceError(text) from e
        raise UpstreamError(text) from e
    return got.path


def _tarball(cfg, source, work, max_bytes):
    interval = 0.0 if cfg.allow_insecure else max(cfg.min_interval_seconds, MIN_HOST_INTERVAL)
    fetcher = fetch.Fetcher(dataclasses.replace(cfg, min_interval_seconds=interval), cfg.upstream_allowed_hosts)
    try:
        got = fetcher.download(source["url"], os.path.join(work, "download"), filename="source",
                               max_bytes=max_bytes, expected_sha256=source["checksum"])
    except fetch.FetchError as e:
        raise UpstreamError(_clean(e)) from e
    return got.path


# Normalised source.tar

def _member_name(raw):
    """Clean relative path of a tar member, or None for the root entry; UpstreamError for anything unsafe."""
    if not isinstance(raw, str) or not raw or raw.startswith("/") or _CONTROL.search(raw):
        raise UpstreamError("unsafe path in the source archive: %s" % _clean(repr(raw)))
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts:
        return None
    if ".." in parts or any(len(p.encode("utf-8", "surrogateescape")) > MAX_COMPONENT_BYTES for p in parts):
        raise UpstreamError("unsafe path in the source archive: %s" % _clean(repr(raw)))
    name = "/".join(parts)
    if len(name.encode("utf-8", "surrogateescape")) > MAX_NAME_BYTES:
        raise UpstreamError("path too long in the source archive")
    return name


def _check_link_target(raw):
    if not isinstance(raw, str) or not raw or raw.startswith("/") or _CONTROL.search(raw) or len(raw) > MAX_NAME_BYTES:
        raise UpstreamError("unsafe symbolic link target in the source archive: %s" % _clean(repr(raw)))


def _resolves_inside(name, target, links):
    """Follow the symlink `name` -> `target` through the other symlinks of the tree; False if it leaves the tree."""
    stack = name.split("/")[:-1]
    pending = target.split("/")
    hops = 0
    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if not stack:
                return False
            stack.pop()
            continue
        stack.append(part)
        path = "/".join(stack)
        if path in links:
            hops += 1
            if hops > MAX_SYMLINK_HOPS:
                return False
            stack.pop()
            pending = links[path].split("/") + pending
    return True


def _check_tree(entries):
    """entries: {name: (kind, link target or None)}. Reject files below symlinks or files, and escaping symlinks."""
    links = {n: t for n, (k, t) in entries.items() if k == "link"}
    for name in entries:
        parts = name.split("/")
        for i in range(1, len(parts)):
            parent = entries.get("/".join(parts[:i]))
            if parent is not None and parent[0] != "dir":
                raise UpstreamError("unsafe layout in the source archive: %s lies below a %s"
                                    % (_clean(name), "symbolic link" if parent[0] == "link" else "file"))
    for name, target in links.items():
        if not _resolves_inside(name, target, links):
            raise UpstreamError("symbolic link %s points outside the source tree" % _clean(name))


def _scan(tar, max_bytes):
    entries, total, count = {}, 0, 0
    for m in tar:
        count += 1
        if count > MAX_MEMBERS:
            raise TooLarge("the source archive has too many entries")
        name = _member_name(m.name)
        if name is None:
            continue
        if name in entries:
            raise UpstreamError("duplicate entry in the source archive: %s" % _clean(name))
        if m.isreg():
            entries[name] = ("file", None)
            total += (m.size + 511) // 512 * 512 + 1024
        elif m.isdir():
            entries[name] = ("dir", None)
            total += 1024
        elif m.issym():
            _check_link_target(m.linkname)
            entries[name] = ("link", m.linkname)
            total += 1024
        else:
            raise UpstreamError("special file or hard link in the source archive: %s" % _clean(name))
        if total > max_bytes:
            raise TooLarge("the source exceeds the size limit")
    _check_tree(entries)
    return entries


def verify_tar(path, max_bytes):
    """Check a finished tar with the same rules normalise_tar applies (UpstreamError when it breaks one)."""
    try:
        with tarfile.open(path, "r:*") as tar:
            return len(_scan(tar, max_bytes))
    except (tarfile.TarError, EOFError, OSError, UnicodeError, ValueError) as e:
        raise UpstreamError("the source archive is not usable (%s)" % type(e).__name__) from e


def _strip_prefix(entries):
    """The single top-level directory shared by every entry, or None."""
    tops = {name.split("/")[0] for name in entries}
    if len(tops) != 1:
        return None
    top = next(iter(tops))
    if entries.get(top, ("dir",))[0] != "dir":
        return None
    return top if all(name == top or name.startswith(top + "/") for name in entries) else None


def _info(name, kind, member, target):
    info = tarfile.TarInfo(name)
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = max(0, int(member.mtime or 0))
    if kind == "dir":
        info.type, info.mode = tarfile.DIRTYPE, 0o755
    elif kind == "link":
        info.type, info.mode, info.linkname = tarfile.SYMTYPE, 0o777, target
    else:
        info.type, info.size = tarfile.REGTYPE, member.size
        info.mode = 0o755 if member.mode & 0o100 else 0o644
    return info


def normalise_tar(src, dst, max_bytes, strip_top=False):
    """Repack the tar archive `src` (any compression) into the plain tar `dst`.

    Only regular files, directories and symbolic links that stay inside the tree survive; owners, groups
    and special mode bits are dropped. Nothing is extracted. Returns the number of entries written.
    """
    try:
        with tarfile.open(src, "r:*") as tar:
            entries = _scan(tar, max_bytes)
        prefix = _strip_prefix(entries) if strip_top else None
        written = 0
        with tarfile.open(src, "r:*") as tar, tarfile.open(dst, "w", format=tarfile.PAX_FORMAT) as out:
            for m in tar:
                name = _member_name(m.name)
                if name is None or name == prefix:
                    continue
                kind, target = entries[name]
                info = _info(name[len(prefix) + 1:] if prefix else name, kind, m, target)
                out.addfile(info, tar.extractfile(m) if kind == "file" else None)
                written += 1
    except (tarfile.TarError, EOFError, OSError, UnicodeError, ValueError) as e:
        raise UpstreamError("the source archive is not usable (%s)" % type(e).__name__) from e
    if os.path.getsize(dst) > max_bytes:
        raise TooLarge("the source exceeds the size limit")
    return written


def _remove(path):
    """Delete exactly `path` (a directory this module created)."""
    shutil.rmtree(path, ignore_errors=True)


def fetch_source(cfg, source, repo, tag, commit, workdir, dest):
    """Fetch the pinned source of a task into the plain tar file `dest`. Returns (sha256, size).

    `source` is the recipe's source mapping ({kind, checksum, url}); `workdir` an existing private
    directory on a disk with room for the source (everything created in it is removed again).
    """
    max_bytes = cfg.builds_max_source_mib << 20
    work = os.path.join(workdir, "work")
    os.mkdir(work, 0o700)
    try:
        if source["kind"] == "git":
            raw = os.path.join(work, "raw.tar")
            _git_source(cfg, repo, tag, commit, work, raw, max_bytes)
            normalise_tar(raw, dest, max_bytes)
        elif source["kind"] == "tarball":
            normalise_tar(_tarball(cfg, source, work, max_bytes), dest, max_bytes, strip_top=True)
        else:
            raise UpstreamError("unknown source kind")
    except BaseException:
        if os.path.lexists(dest):
            os.unlink(dest)
        raise
    finally:
        _remove(work)
    return fetch.file_sha256(dest), os.path.getsize(dest)
