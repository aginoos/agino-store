# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate, sign and publish the schema 1 catalog (docs/server.md section 8)."""

import datetime
import errno
import functools
import json
import os
import re
import shutil
import stat
import struct
from urllib.parse import quote

from . import catalogwriter as cw
from .content import content_digest

CATALOG_DIR_RE = re.compile(r"\Acatalog-(\d{10})\Z")
DEB_FILE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+~-]*\.deb\Z")
SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}")
MAX_RELEASES = 5


class CatalogError(Exception):
    pass


class Skip(Exception):
    """An app cannot go into the catalog; the message is the reason."""


# Debian version comparison (dpkg algorithm)

def _order(ch):
    if ch.isdigit():
        return 0
    if ch.isalpha():
        return ord(ch)
    return -1 if ch == "~" else ord(ch) + 256


def _is_digit(text, i):
    return i < len(text) and text[i] in "0123456789"


def _cmp_part(a, b):
    i = j = 0
    while i < len(a) or j < len(b):
        while (i < len(a) and not _is_digit(a, i)) or (j < len(b) and not _is_digit(b, j)):
            ac = _order(a[i]) if i < len(a) else 0
            bc = _order(b[j]) if j < len(b) else 0
            if ac != bc:
                return ac - bc
            i, j = i + 1, j + 1
        while i < len(a) and a[i] == "0":
            i += 1
        while j < len(b) and b[j] == "0":
            j += 1
        diff = 0
        while _is_digit(a, i) and _is_digit(b, j):
            diff = diff or ord(a[i]) - ord(b[j])
            i, j = i + 1, j + 1
        if _is_digit(a, i):
            return 1
        if _is_digit(b, j):
            return -1
        if diff:
            return diff
    return 0


def _split_version(version):
    epoch, _, rest = version.partition(":") if ":" in version else ("0", "", version)
    upstream, _, revision = rest.rpartition("-") if "-" in rest else (rest, "", "")
    return (int(epoch) if epoch.isdigit() else 0), upstream, revision


def compare_versions(a, b):
    ea, ua, ra = _split_version(a)
    eb, ub, rb = _split_version(b)
    return (ea > eb) - (ea < eb) or _cmp_part(ua, ub) or _cmp_part(ra, rb)


# Image sizes

def image_size(path):
    """(width, height) of a PNG, JPEG or WebP file, read from the headers."""
    if os.stat(path).st_size > cw.MAX_ASSET_BYTES:
        raise CatalogError("image larger than 10 MiB: %s" % os.path.basename(path))
    with open(path, "rb") as f:
        data = f.read(cw.MAX_ASSET_BYTES + 1)
    if len(data) > cw.MAX_ASSET_BYTES:
        raise CatalogError("image larger than 10 MiB: %s" % os.path.basename(path))
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":
        return _jpeg_size(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp_size(data)
    raise CatalogError("unsupported or damaged image: %s" % os.path.basename(path))


def _jpeg_size(data):
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
            i += 1 if marker == 0xFF else 2
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return width, height
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    raise CatalogError("JPEG without frame header")


def _webp_size(data):
    kind = data[12:16]
    if kind == b"VP8X" and len(data) >= 30:
        return (int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1)
    if kind == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if kind == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    raise CatalogError("unsupported WebP")


# Resolving what goes into the catalog

def _day(stamp, what):
    if not isinstance(stamp, str) or not DATE_RE.match(stamp):
        raise Skip("%s is not a timestamp" % what)
    return stamp[:10]


def _deb_info(cfg, st, app, approved):
    """Facts about the approved deb of an app, or Skip."""
    entry = approved.get(app.id)
    if not entry:
        raise Skip("no approved version")
    record = st.find_candidate(app.id, entry.get("version"), entry.get("sha256"))
    if record is None:
        raise Skip("approved candidate record is missing")
    sha = record.get("sha256")
    name = os.path.basename(str(record.get("filename", "")))
    if not SHA256_RE.match(str(sha)) or not DEB_FILE_RE.match(name) or record.get("file") != "%s/%s" % (sha, name):
        raise Skip("candidate record has a bad file name")
    if record.get("status") != "approved":
        raise Skip("candidate is %s, not approved" % record.get("status"))
    if ((record.get("checks") or {}).get("deps") or {}).get("ok") is not True:
        raise Skip("dependency check has not passed")
    path = os.path.join(cfg.downloads, sha, name)
    if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) != record.get("size"):
        raise Skip("deb file is missing or has the wrong size")
    if cw.sha256_file(path) != entry.get("sha256"):
        raise Skip("deb file does not match the approved sha256")
    return {"version": record["version"], "sha256": sha, "size": record["size"], "filename": name,
            "installed_size": int(record.get("installed_size") or 0), "path": path,
            "vendor_url": record.get("vendor_url") or "", "updated": _day(entry.get("approved_at"), "approved_at")}


def _milkyway_info(app, milkyway):
    entry = milkyway.get(app.package)
    if not entry:
        raise Skip("package not in milkyway")
    if not isinstance(entry.get("version"), str):
        raise Skip("milkyway entry has no version")
    return {"version": entry.get("version"), "updated": _day(entry.get("first_seen"), "first_seen")}


def resolve(cfg, st, content):
    """({app id: facts}, [{"id", "reason"}]) for every app that can be published."""
    approved, milkyway = st.approved(), st.milkyway()
    facts, skipped = {}, []
    for app in content.apps:
        try:
            if app.kind == "deb":
                facts[app.id] = _deb_info(cfg, st, app, approved)
            else:
                facts[app.id] = _milkyway_info(app, milkyway)
        except Skip as e:
            skipped.append({"id": app.id, "reason": str(e)})
    return facts, skipped


def inputs_hash(cfg, content, facts):
    shown = {k: {f: v for f, v in info.items() if f != "path"} for k, info in facts.items()}
    blob = {"content": content_digest(content.root), "facts": shown, "deb_base_url": cfg.deb_base_url}
    return cw.sha256_bytes(json.dumps(blob, sort_keys=True).encode("utf-8"))


# Serial and renewal

def _utc(stamp):
    return datetime.datetime.strptime(stamp, cw.TIME_FORMAT).replace(tzinfo=datetime.timezone.utc)


def next_serial(previous, today):
    base = int(today.strftime("%Y%m%d")) * 100
    serial = max(base + 1, int(previous) + 1)
    if serial % 100 == 0:   # the previous serial was ...99: the day's 01-99 are used up
        raise CatalogError("no serial left for today (previous %s)" % previous)
    return serial


def _free_serial(preview, previous, now):
    """Next serial whose catalog directory does not exist (a crash may have left one behind)."""
    serial = next_serial(previous, now)
    while os.path.lexists(os.path.join(preview, "catalog-%d" % serial)):
        serial = next_serial(serial, now)
    return serial


def _unchanged(cfg, prev, inputs, current_dir, now):
    if prev.get("inputs_sha256") != inputs or not os.path.isdir(current_dir):
        return False
    try:
        age = now - _utc(prev["built_at"])
    except (KeyError, ValueError):
        return False
    return age < datetime.timedelta(hours=cfg.renew_after_hours)


# Catalog documents

def _asset_path(store, app, rel):
    try:
        return store.add(rel, root=app.dir)
    except cw.CatalogWriterError as e:
        raise Skip(str(e)) from e


def _package(cfg, app, info):
    if app.kind == "milkyway":
        return {"kind": "milkyway", "name": app.package}
    deb = {"version": info["version"], "url": cfg.deb_base_url + "%s/%s" % (info["sha256"], quote(info["filename"], safe="+~")),
           "vendor_url": info["vendor_url"], "sha256": info["sha256"], "size": info["size"],
           "installed_size": info["installed_size"]}
    return {"kind": "deb", "name": app.package, "deb": deb}


def _summary(cfg, app, info, icon):
    out = {"id": app.id, "name": app.name, "summary": app.summary, "icon": icon,
           "categories": list(app.categories), "source": app.source, "license": app.license,
           "proprietary": app.proprietary}
    if app.launchable:
        out["launchable"] = app.launchable
    out.update({"updated": info["updated"], "package": _package(cfg, app, info),
                "detail": "apps/%s.json" % app.id})
    return out


def _screenshots(store, app):
    out = {}
    for lang, rels in app.screenshots.items():
        shots = []
        for rel in rels:
            published = _asset_path(store, app, rel)
            try:
                width, height = image_size(os.path.join(app.dir, rel))
            except (CatalogError, OSError, struct.error) as e:
                raise Skip("screenshot %s: %s" % (rel, e)) from e
            shots.append({"image": published, "thumb": published, "width": width, "height": height})
        out[lang] = shots
    return out


def _old_release_date(st, app, version):
    record = st.find_candidate(app.id, version) if app.kind == "deb" else None
    stamp = (record or {}).get("found")
    return stamp[:10] if isinstance(stamp, str) and DATE_RE.match(stamp) else None


def _releases(st, app, info):
    """Current version plus older versions that have notes and a known date, newest first, max 5."""
    empty = {"zh_CN": [], "en_US": []}
    notes = app.release_notes.get(info["version"])
    items = [(info["version"], info["updated"], {k: list(v) for k, v in (notes or empty).items()})]
    for version, text in app.release_notes.items():
        date = _old_release_date(st, app, version)
        if version != info["version"] and date:
            items.append((version, date, {k: list(v) for k, v in text.items()}))
    items.sort(key=functools.cmp_to_key(lambda x, y: compare_versions(y[0], x[0])))
    return [{"version": v, "date": d, "notes": n} for v, d, n in items[:MAX_RELEASES]]


def _install_profile(app):
    if not app.install_profile:
        return None
    return {"debconf": list(app.install_profile["debconf"]),
            "default_files": [dict(f) for f in app.install_profile["default_files"]]}


def _detail(st, store, app, info):
    maintainer = app.maintainer
    out = {"id": app.id, "description": {k: list(v) for k, v in app.description.items()},
           "developer": app.developer, "homepage": app.homepage,
           "maintainer": "%s (%s)" % (maintainer["name"], maintainer["contact"]) if maintainer else None,
           "screenshots": _screenshots(store, app), "releases": _releases(st, app, info)}
    profile = _install_profile(app)
    if profile:
        out["install_profile"] = profile
    return out


def build_files(cfg, st, content, facts):
    """(files for the tree, [included app ids], [{"id", "reason"}] for apps dropped here)."""
    store = cw.AssetStore()
    files, apps, skipped = {}, [], []
    for app in content.apps:
        if app.id not in facts:
            continue
        try:
            icon = _asset_path(store, app, app.icon)
            detail = _detail(st, store, app, facts[app.id])
            apps.append(_summary(cfg, app, facts[app.id], icon))
            files["apps/%s.json" % app.id] = cw.dump_json(detail)
        except Skip as e:
            skipped.append({"id": app.id, "reason": str(e)})
    included = {a["id"] for a in apps}
    catalog = {"schema": 1, "categories": _categories(content), "featured": _featured(content, included),
               "apps": apps}
    files["catalog.json"] = cw.dump_json(catalog)
    files.update(store.files)
    return files, sorted(included), skipped


def _categories(content):
    return [{"id": c.id, "name": c.name, "order": i} for i, c in enumerate(content.categories, 1)]


def _featured(content, included):
    out = []
    for f in content.featured:
        apps = [a for a in f.apps if a in included]
        if not apps:
            continue
        entry = {"id": f.id, "title": f.title}
        if f.subtitle:
            entry["subtitle"] = f.subtitle
        entry.update({"apps": apps, "banner": f.banner})
        out.append(entry)
    return out


# Publishing

def _place_deb(src, dest, sha):
    """Hard link (or copy) src to dest; an existing dest is kept only if it is src or has the same hash."""
    try:
        st = os.lstat(dest)
    except FileNotFoundError:
        st = None
    if st is not None:
        source = os.stat(src)
        same = (st.st_ino, st.st_dev) == (source.st_ino, source.st_dev)
        if stat.S_ISREG(st.st_mode) and (same or cw.sha256_file(dest) == sha):
            return
        os.unlink(dest)
    try:
        os.link(src, dest)
    except OSError as e:
        if e.errno not in (errno.EXDEV, errno.EPERM):
            raise
        shutil.copyfile(src, dest)


def _link_debs(cfg, facts, included):
    for app_id in included:
        info = facts[app_id]
        if "path" not in info:
            continue
        directory = os.path.join(cfg.preview, "debs", info["sha256"])
        os.makedirs(directory, exist_ok=True)
        _place_deb(info["path"], os.path.join(directory, info["filename"]), info["sha256"])


def verify_tree(path):
    """Check manifest.json against the files on disk and that every app detail exists."""
    with open(os.path.join(path, "manifest.json"), "rb") as f:
        manifest = json.load(f)
    for entry in manifest["files"]:
        full = os.path.join(path, entry["path"])
        if cw.sha256_file(full) != entry["sha256"] or os.path.getsize(full) != entry["size"]:
            raise CatalogError("generated file does not match the manifest: %s" % entry["path"])
    with open(os.path.join(path, "catalog.json"), "rb") as f:
        catalog = json.load(f)
    for app in catalog["apps"]:
        if not os.path.isfile(os.path.join(path, app["detail"])):
            raise CatalogError("missing detail file for %s" % app["id"])


def _swap_current(preview, name):
    tmp = os.path.join(preview, ".current-%s" % name)
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(name, tmp)
    os.replace(tmp, os.path.join(preview, "current"))


def prune(preview, keep):
    """Delete old catalog-<serial> directories beyond `keep`, never the one current points to."""
    names = sorted((n for n in os.listdir(preview) if CATALOG_DIR_RE.match(n)), reverse=True)
    current = os.path.realpath(os.path.join(preview, "current"))
    for name in names[keep:]:
        path = os.path.join(preview, name)
        if os.path.realpath(path) != current and os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)


def _referenced_shas(preview):
    """sha256 values of the debs listed in any catalog-<serial> directory that still exists."""
    shas = set()
    for name in os.listdir(preview):
        path = os.path.join(preview, name)
        if not CATALOG_DIR_RE.match(name) or os.path.islink(path):
            continue
        try:
            with open(os.path.join(path, "catalog.json"), encoding="utf-8") as f:
                apps = json.load(f)["apps"]
        except (OSError, ValueError, KeyError):
            continue
        shas.update(str(((a.get("package") or {}).get("deb") or {}).get("sha256")) for a in apps)
    return shas


def prune_debs(preview, valid_days, now):
    """Delete preview/debs/<sha>/ not used by a kept catalog and older than valid_days + 7 days."""
    debs = os.path.join(preview, "debs")
    if not os.path.isdir(debs):
        return
    keep = _referenced_shas(preview)
    cutoff = now.timestamp() - (valid_days + 7) * 86400
    for sha in os.listdir(debs):
        path = os.path.join(debs, sha)
        if sha in keep or not SHA256_RE.match(sha) or os.path.islink(path) or not os.path.isdir(path):
            continue
        if os.lstat(path).st_mtime >= cutoff:
            continue
        try:
            for entry in os.listdir(path):
                os.unlink(os.path.join(path, entry))
            os.rmdir(path)
        except OSError:
            continue    # something unexpected in there: leave it for a human


def build(cfg, st, content, if_changed=False):
    """Build and publish the catalog; returns a summary dict (docs/server.md section 8)."""
    facts, skipped = resolve(cfg, st, content)
    inputs = inputs_hash(cfg, content, facts)
    now = cw.now()
    prev = st.catalog_state()
    current_dir = os.path.join(cfg.preview, "current")
    if if_changed and _unchanged(cfg, prev, inputs, current_dir, now):
        return {"serial": prev["serial"], "revision": prev.get("revision"), "apps": prev.get("apps", 0),
                "skipped": skipped, "changed": False}
    renewed = prev.get("inputs_sha256") == inputs
    os.makedirs(cfg.preview, exist_ok=True)
    serial = _free_serial(cfg.preview, prev.get("serial", 0), now)
    revision = "r%d-%s" % (serial, inputs[:12])
    files, included, dropped = build_files(cfg, st, content, facts)
    name = "catalog-%d" % serial
    _publish(cfg, files, serial, revision, name, now, facts, included)
    st.save_catalog_state({"serial": serial, "inputs_sha256": inputs, "revision": revision, "apps": len(included),
                           "built_at": now.strftime(cw.TIME_FORMAT)})
    prune(cfg.preview, cfg.keep_catalogs)
    prune_debs(cfg.preview, cfg.valid_days, now)
    return {"serial": serial, "revision": revision, "apps": len(included), "skipped": skipped + dropped,
            "changed": True, "renewed": renewed}


def _publish(cfg, files, serial, revision, name, now, facts, included):
    out = os.path.join(cfg.preview, name)
    if os.path.lexists(out):
        raise CatalogError("%s already exists" % name)
    try:
        cw.write_tree(out, files, serial, revision, cfg.valid_days, cfg.key, when=now)
        verify_tree(out)
        _link_debs(cfg, facts, included)
    except BaseException as e:
        shutil.rmtree(out, ignore_errors=True)
        if isinstance(e, cw.CatalogWriterError):
            raise CatalogError(str(e)) from None
        raise
    _swap_current(cfg.preview, name)
