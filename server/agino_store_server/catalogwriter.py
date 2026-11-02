# SPDX-License-Identifier: GPL-3.0-or-later
"""Write a signed catalog tree (docs/design.md section 4).

Shared by tools/make-catalog.py and the server's catalog builder: content-addressed
assets, the tree itself, manifest.json and its Ed25519 signature (openssl).
"""

import base64
import datetime
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile

ASSET_EXTS = {".svg", ".png", ".webp", ".jpg", ".jpeg"}
KNOWN_OUTPUTS = ("catalog.json", "apps", "stats.json", "assets", "manifest.json", "manifest.json.sig")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
MAX_ASSET_BYTES = 10 << 20


class CatalogWriterError(Exception):
    pass


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def dump_json(obj):
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class AssetStore:
    """Maps source files to content-addressed paths assets/<sha256>.<ext>."""

    def __init__(self, default_root=None):
        self.default_root = default_root
        self.files = {}        # published path -> ("file", source path)
        self._by_source = {}   # real source path -> published path

    def add(self, name, root=None):
        base = os.path.realpath(root or self.default_root)
        src = os.path.realpath(os.path.join(base, name))
        if src in self._by_source:
            return self._by_source[src]
        if os.path.commonpath([src, base]) != base or not os.path.isfile(src):
            raise CatalogWriterError("asset not found or outside assets dir: %s" % name)
        ext = os.path.splitext(src)[1].lower()
        if ext not in ASSET_EXTS:
            raise CatalogWriterError("unsupported asset type: %s" % name)
        if os.path.getsize(src) > MAX_ASSET_BYTES:
            raise CatalogWriterError("asset larger than 10 MiB: %s" % name)
        published = "assets/%s%s" % (sha256_file(src), ext)
        self.files[published] = ("file", src)
        self._by_source[src] = published
        return published


def _clean_outputs(out):
    for name in KNOWN_OUTPUTS:
        p = os.path.join(out, name)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        elif os.path.lexists(p):
            os.remove(p)


def _write_files(out, files):
    entries = []
    for path in sorted(files):
        data = files[path]
        dest = os.path.join(out, path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if isinstance(data, tuple):
            shutil.copyfile(data[1], dest)
        else:
            with open(dest, "wb") as f:
                f.write(data)
        entries.append({"path": path, "sha256": sha256_file(dest), "size": os.path.getsize(dest)})
    return entries


def now():
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        stamp = datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc)
    else:
        stamp = datetime.datetime.now(datetime.timezone.utc)
    return stamp.replace(microsecond=0)


def write_tree(out, files, serial, revision, valid_days, key, when=None):
    """Write files (path -> bytes or ("file", src)), manifest.json and manifest.json.sig into out.

    Returns the manifest dict. `when` (aware datetime) overrides SOURCE_DATE_EPOCH and the clock.
    """
    out = os.path.abspath(out)
    os.makedirs(out, exist_ok=True)
    _clean_outputs(out)
    entries = _write_files(out, files)
    created = (when or now()).replace(microsecond=0)
    manifest = {
        "schema": 1,
        "serial": serial,
        "revision": revision,
        "created": created.strftime(TIME_FORMAT),
        "expires": (created + datetime.timedelta(days=valid_days)).strftime(TIME_FORMAT),
        "files": entries,
    }
    manifest_path = os.path.join(out, "manifest.json")
    with open(manifest_path, "wb") as f:
        f.write(dump_json(manifest))
    sign(manifest_path, key)
    return manifest


def _check_key_file(key):
    """The signing key must be a regular file of the current user without group/other access."""
    try:
        st = os.stat(key)
    except OSError:
        raise CatalogWriterError("signing failed: key file is not readable") from None
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid():
        raise CatalogWriterError("signing failed: key file must be a regular file owned by the current user "
                                 "with mode 0600")


def sign(manifest_path, key):
    """Write <manifest_path>.sig: base64 of the Ed25519 signature over the raw bytes."""
    _check_key_file(key)
    with tempfile.TemporaryDirectory() as tmp:
        sig_path = os.path.join(tmp, "sig.bin")
        try:
            subprocess.run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", key,
                            "-in", manifest_path, "-out", sig_path], check=True, capture_output=True)
        except (subprocess.CalledProcessError, OSError):
            raise CatalogWriterError("signing failed") from None
        with open(sig_path, "rb") as f:
            sig = f.read()
    if len(sig) != 64:
        raise CatalogWriterError("unexpected signature length %d" % len(sig))
    with open(manifest_path + ".sig", "w") as f:
        f.write(base64.b64encode(sig).decode("ascii") + "\n")
