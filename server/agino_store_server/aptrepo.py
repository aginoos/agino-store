# SPDX-License-Identifier: GPL-3.0-or-later
"""Vendor apt repositories: verified InRelease, Packages by hash (docs/server.md section 6)."""

import datetime
import email.utils
import hashlib
import lzma
import os
import re
import tempfile
import zlib
from dataclasses import dataclass

from . import debinfo

_FILENAME_RE = re.compile(r"\A[A-Za-z0-9._+~/-]+\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_INDEX_SUFFIXES = (".gz", ".xz", "")
MAX_RELEASE_AGE = datetime.timedelta(days=30)


class AptError(Exception):
    pass


@dataclass(frozen=True)
class Release:
    date: datetime.datetime
    sha256: dict            # relative path -> (hash, size)
    suite: str = ""
    codename: str = ""
    valid_until: datetime.datetime | None = None


@dataclass(frozen=True)
class AptResult:
    version: str
    filename: str           # basename of the deb
    deb_url: str
    sha256: str
    size: int
    signed_by: str
    inrelease_sha256: str
    inrelease_date: str     # ISO, UTC


def fmt_date(dt):
    return dt.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_fpr(value):
    return re.sub(r"\s+", "", str(value)).upper()


# Keyring and signature

def resolve_key(content_root, key):
    """Key files live under <content>/vendors/keys/*.asc; refuse anything else."""
    vendors = os.path.realpath(os.path.join(content_root, "vendors"))
    path = os.path.realpath(os.path.join(vendors, key))
    keys_dir = os.path.join(vendors, "keys") + os.sep
    if not path.startswith(keys_dir) or not path.endswith(".asc") or not os.path.isfile(path):
        raise AptError("bad key file: %s" % key)
    return path


def build_keyring(content_root, keys, workdir):
    """Dearmor each .asc into one binary keyring inside workdir. Returns its path."""
    home = os.path.join(workdir, "gnupg")
    os.makedirs(home, mode=0o700, exist_ok=True)
    keyring = os.path.join(workdir, "keyring.gpg")
    blobs = []
    for i, key in enumerate(keys):
        out = os.path.join(workdir, "key%d.gpg" % i)
        src = resolve_key(content_root, key)
        proc = debinfo.run_tool(["gpg", "--homedir", home, "--batch", "--yes",
                                 "--dearmor", "--output", out, src], timeout=60)
        if proc.returncode != 0 or not os.path.isfile(out):
            raise AptError("cannot read key %s" % key)
        with open(out, "rb") as f:
            blobs.append(f.read())
    if not blobs:
        raise AptError("no keys configured")
    with open(keyring, "wb") as f:
        f.write(b"".join(blobs))
    return keyring


def verify_inrelease(data, keyring, fingerprints, workdir):
    """Verify a clearsigned InRelease. Returns (signed content bytes, primary fingerprint).

    The signed content comes from gpgv itself (--output file); the unsigned file is never parsed.
    The status stream goes to stdout, the data to a file, so hostile content cannot forge status lines.
    """
    os.makedirs(os.path.join(workdir, "gnupg"), mode=0o700, exist_ok=True)
    src = os.path.join(workdir, "InRelease")
    out = os.path.join(workdir, "Release.signed")
    with open(src, "wb") as f:
        f.write(data)
    proc = debinfo.run_tool(["gpgv", "--homedir", os.path.join(workdir, "gnupg"), "--status-fd", "1",
                             "--keyring", keyring, "--output", out, src], timeout=60)
    status = proc.stdout.decode("utf-8", "replace").split("\n")
    bad = [s for s in status if re.match(r"\[GNUPG:\] (BADSIG|EXPSIG|EXPKEYSIG|REVKEYSIG)", s)]
    if proc.returncode != 0 or bad:
        raise AptError("InRelease signature check failed")
    primaries = [s.split()[-1] for s in status if s.startswith("[GNUPG:] VALIDSIG ")]
    allowed = {_norm_fpr(x) for x in fingerprints}
    signer = next((_norm_fpr(p) for p in primaries if _norm_fpr(p) in allowed), None)
    if signer is None:
        raise AptError("InRelease is not signed by an expected key")
    with open(out, "rb") as f:
        return f.read(), signer


# Release and Packages

def _parse_date(value, what):
    try:
        date = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError) as e:
        raise AptError("bad Release %s" % what) from e
    return date if date.tzinfo is not None else date.replace(tzinfo=datetime.UTC)


def parse_release(content):
    stanzas = debinfo.parse_stanzas(content.decode("utf-8", "replace"))
    if not stanzas or "Date" not in stanzas[0]:
        raise AptError("Release has no Date")
    top = stanzas[0]
    date = _parse_date(top["Date"], "Date")
    valid_until = _parse_date(top["Valid-Until"], "Valid-Until") if top.get("Valid-Until") else None
    hashes = {}
    for line in top.get("SHA256", "").split("\n"):
        parts = line.split()
        if len(parts) == 3 and _SHA256_RE.match(parts[0]) and parts[1].isdigit():
            hashes[parts[2]] = (parts[0], int(parts[1]))
    return Release(date, hashes, top.get("Suite", ""), top.get("Codename", ""), valid_until)


def check_release(release, suite, now=None):
    """Suite/Codename must match the tracked suite; Valid-Until and Date must be current."""
    now = now or datetime.datetime.now(datetime.UTC)
    if suite not in (release.suite, release.codename):
        raise AptError("Release is for suite %r, not %r" % (debinfo.clean_text(release.suite or release.codename), suite))
    if release.valid_until is not None and release.valid_until < now:
        raise AptError("Release expired (Valid-Until %s)" % fmt_date(release.valid_until))
    if now - release.date > MAX_RELEASE_AGE:
        raise AptError("Release is older than %d days (Date %s)" % (MAX_RELEASE_AGE.days, fmt_date(release.date)))


def check_rollback(date, previous_iso, sha256=None, previous_sha256=None):
    if not previous_iso:
        return
    previous = datetime.datetime.fromisoformat(previous_iso)
    if date < previous:
        raise AptError("InRelease Date %s is older than the recorded %s (rollback?)"
                       % (fmt_date(date), previous_iso))
    if date == previous and previous_sha256 and sha256 and sha256 != previous_sha256:
        raise AptError("InRelease Date %s unchanged but the content differs (replay?)" % previous_iso)


def _decompress(name, data, limit):
    if name.endswith(".gz"):
        d = zlib.decompressobj(31)
    elif name.endswith(".xz"):
        d = lzma.LZMADecompressor()
    else:
        if len(data) > limit:
            raise AptError("index too large")
        return data
    try:
        out = d.decompress(data, limit + 1)
    except (zlib.error, lzma.LZMAError) as e:
        raise AptError("corrupt index %s" % name) from e
    if len(out) > limit:
        raise AptError("index %s too large when decompressed" % name)
    if not d.eof:
        raise AptError("truncated index %s" % name)
    return out


def fetch_packages(fetcher, base_url, suite, component, arch, release, limit):
    """Packages text for one component/arch, verified against the Release hash list."""
    rel = "%s/binary-%s/Packages" % (component, arch)
    for suffix in _INDEX_SUFFIXES:
        entry = release.sha256.get(rel + suffix)
        if entry is None:
            continue
        digest, size = entry
        if size > limit:
            raise AptError("%s too large" % (rel + suffix))
        url = "%s/dists/%s/%s" % (base_url.rstrip("/"), suite, rel + suffix)
        data = fetcher.get(url, max_bytes=limit).data
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise AptError("hash mismatch for %s" % (rel + suffix))
        return _decompress(rel + suffix, data, limit).decode("utf-8", "replace")
    raise AptError("%s is not listed in Release" % rel)


def _valid_stanza(s, package, arch):
    return (s.get("Package") == package and s.get("Architecture") in (arch, "all")
            and debinfo.VERSION_RE.match(s.get("Version", ""))
            and _FILENAME_RE.match(s.get("Filename", "")) and ".." not in s["Filename"].split("/")
            and not s["Filename"].startswith("/")
            and _SHA256_RE.match(s.get("SHA256", "")) and s.get("Size", "").isdigit())


def pick_latest(stanzas, package, arch):
    best = None
    for s in stanzas:
        if _valid_stanza(s, package, arch) and (best is None or debinfo.version_gt(s["Version"], best["Version"])):
            best = s
    return best


def find_latest(cfg, fetcher, vendor, content_root, memo):
    """Verify the vendor's InRelease and return the highest version of the package (AptResult)."""
    track = vendor["track"]
    base = track["url"].rstrip("/")
    inrelease = fetcher.get("%s/dists/%s/InRelease" % (base, track["suite"]), max_bytes=cfg.max_index_bytes)
    with tempfile.TemporaryDirectory(prefix="apt-verify-") as work:
        keyring = build_keyring(content_root, track["keys"], work)
        content, signer = verify_inrelease(inrelease.data, keyring, track["fingerprints"], work)
    release = parse_release(content)
    check_release(release, track["suite"])
    release_sha = hashlib.sha256(inrelease.data).hexdigest()
    check_rollback(release.date, memo.get("inrelease_date"), release_sha, memo.get("inrelease_sha256"))
    text = fetch_packages(fetcher, base, track["suite"], track["component"], vendor["arch"],
                          release, cfg.max_index_bytes)
    best = pick_latest(debinfo.parse_stanzas(text), vendor["package"], vendor["arch"])
    if best is None:
        raise AptError("package %s not found in %s" % (vendor["package"], base))
    return AptResult(
        version=best["Version"], filename=os.path.basename(best["Filename"]),
        deb_url="%s/%s" % (base, best["Filename"]), sha256=best["SHA256"], size=int(best["Size"]),
        signed_by=signer, inrelease_sha256=release_sha,
        inrelease_date=fmt_date(release.date),
    )


def download_deb(cfg, fetcher, result):
    """Download the deb named by an AptResult; hash and size must match the signed Packages entry."""
    return fetcher.download(result.deb_url, cfg.downloads, filename=result.filename,
                            max_bytes=cfg.max_deb_bytes, expected_sha256=result.sha256,
                            expected_size=result.size)
