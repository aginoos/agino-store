# SPDX-License-Identifier: GPL-3.0-or-later
"""Demo data trees of store-content (`demo/<name>/...`, docs/server.md section 3) and their tar export.

The trees are only ever walked with lstat/O_NOFOLLOW; a symlink or any non-regular file anywhere
makes the content invalid. The tar written for the sandbox queue is deterministic.
"""

import io
import os
import re
import stat
import tarfile

NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,40}\Z")
DEMO_DIR = "demo"
MAX_TREE_BYTES = 20 << 20
MAX_FILE_BYTES = 5 << 20
MAX_ENTRIES = 5000
MAX_DEPTH = 16
MAX_NAME_BYTES = 200


class DemoError(Exception):
    pass


def _walk(path, depth=0):
    """Yield (relative path parts, lstat, is_dir) for everything below path, sorted; raises DemoError."""
    if depth >= MAX_DEPTH:
        raise DemoError("nested deeper than %d levels" % MAX_DEPTH)
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        try:
            raw = name.encode("utf-8")
        except UnicodeEncodeError:
            raise DemoError("file name is not valid UTF-8: %r" % name) from None
        if not raw or len(raw) > MAX_NAME_BYTES or any(c < 0x20 or c == 0x7F for c in raw) or "\\" in name:
            raise DemoError("unusable file name %r" % name)
        st = os.lstat(full)
        if stat.S_ISDIR(st.st_mode):
            yield (name,), st, True
            for parts, sub, is_dir in _walk(full, depth + 1):
                yield (name,) + parts, sub, is_dir
        elif stat.S_ISREG(st.st_mode):
            yield (name,), st, False
        else:
            raise DemoError("%s: only regular files and directories are allowed (no symlinks)"
                            % "/".join(os.path.relpath(full, path).split(os.sep)))


def _check_tree(path):
    total = count = 0
    for parts, st, is_dir in _walk(path):
        count += 1
        if count > MAX_ENTRIES:
            raise DemoError("more than %d entries" % MAX_ENTRIES)
        if not is_dir:
            if st.st_size > MAX_FILE_BYTES:
                raise DemoError("%s: file larger than 5 MiB" % "/".join(parts))
            total += st.st_size
    if total > MAX_TREE_BYTES:
        raise DemoError("tree larger than 20 MiB")


def scan(chk, root):
    """Validate every tree below root/demo; returns the set of valid tree names."""
    base = os.path.join(root, DEMO_DIR)
    if not os.path.lexists(base):
        return set()
    if os.path.islink(base) or not os.path.isdir(base):
        chk.fail(DEMO_DIR, "must be a real directory")
        return set()
    names = set()
    for name in sorted(os.listdir(base)):
        where = "%s/%s" % (DEMO_DIR, name)
        path = os.path.join(base, name)
        if not NAME_RE.match(name) or os.path.islink(path) or not os.path.isdir(path):
            chk.fail(where, "must be a real directory named like ^[a-z0-9][a-z0-9-]{0,40}$")
            continue
        try:
            _check_tree(path)
        except (DemoError, OSError) as e:
            chk.fail(where, str(e))
            continue
        names.add(name)
    return names


def _info(name, is_dir):
    info = tarfile.TarInfo(name)
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    info.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
    info.mode = 0o755 if is_dir else 0o644
    return info


def _read(path, size):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size != size:
            raise DemoError("%s changed while reading" % path)
        return f.read(size + 1)[:size]


def build_tar(root, names):
    """Deterministic uncompressed POSIX tar of the named trees, as bytes."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(names):
            top = os.path.join(root, DEMO_DIR, name)
            tar.addfile(_info(name, True))
            for parts, st, is_dir in _walk(top):
                member = "/".join((name,) + parts)
                info = _info(member, is_dir)
                if is_dir:
                    tar.addfile(info)
                else:
                    info.size = st.st_size
                    tar.addfile(info, io.BytesIO(_read(os.path.join(top, *parts), st.st_size)))
    return out.getvalue()
