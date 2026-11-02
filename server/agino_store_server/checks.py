# SPDX-License-Identifier: GPL-3.0-or-later
"""Candidate checks: inspect, diff, dependencies (docs/server.md section 7) and `check`."""

import difflib
import hashlib
import os
import re
import shutil
import tempfile
import urllib.parse
from dataclasses import dataclass

from . import aptrepo, debinfo, fetch, state

DOSE_DETAIL_MAX = 4096
SCRIPT_MAX_BYTES = 2 * 1024 * 1024
FLAG_LIMIT = 100
DEP_FIELDS = ("Depends", "Pre-Depends", "Conflicts", "Breaks", "Provides", "Replaces")
FG_FIELDS = ("Package", "Version", "Architecture", "Multi-Arch") + DEP_FIELDS
SYSTEMD_DIRS = ("lib/systemd/system/", "usr/lib/systemd/system/", "etc/systemd/system/")
UDEV_DIRS = ("lib/udev/rules.d/", "usr/lib/udev/rules.d/", "etc/udev/rules.d/")
KEYWORDS = ("sources.list", "apt-key", "trusted.gpg", "chmod 777", "useradd", "adduser",
            "groupadd", "systemctl enable")
_CACHE_NAME_RE = re.compile(r"\A[0-9a-f]{64}\.(Packages|json)\Z")


class CheckError(Exception):
    pass


@dataclass(frozen=True)
class MilkywayIndex:
    release_sha256: str
    packages_path: str
    packages: dict           # name -> {version, size, installed_size}


# inspect

def file_flags(files):
    flags = []
    for e in files:
        path, mode = e["path"], e["mode"]
        if e["type"] == "-" and (mode[3] in "sS"):
            flags.append("setuid %s" % path)
        if e["type"] == "-" and (mode[6] in "sS"):
            flags.append("setgid %s" % path)
        if path.startswith("etc/apt/") and e["type"] != "d":
            flags.append("writes /etc/apt: %s" % path)
        if path.startswith(SYSTEMD_DIRS) and e["type"] != "d":
            flags.append("systemd unit %s" % path)
        if path.startswith("etc/cron.") and e["type"] != "d":
            flags.append("cron %s" % path)
        if path.startswith(("etc/sudoers.d/", "etc/profile.d/")) and e["type"] != "d":
            flags.append("writes /%s" % path)
        if path.startswith(UDEV_DIRS) and e["type"] != "d":
            flags.append("udev rule %s" % path)
    return flags


def script_flags(scripts):
    flags = []
    for name, data in sorted(scripts.items()):
        text = data.decode("utf-8", "replace")
        flags.extend("%s mentions %s" % (name, kw) for kw in KEYWORDS if kw in text)
    return flags


def read_scripts(control_dir):
    """Maintainer scripts as bytes; symlinks and other non-regular files are ignored."""
    out = {}
    for name in debinfo.SCRIPT_NAMES:
        path = os.path.join(control_dir, name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        with open(path, "rb") as f:
            out[name] = f.read(SCRIPT_MAX_BYTES)
    return out


def _write_files_list(path, files):
    lines = []
    for e in files:
        name = e["path"].replace("\n", "?")
        suffix = " -> %s" % e["target"].replace("\n", "?") if e["target"] else ""
        lines.append("%s %s %d %s%s" % (e["mode"], e["owner"], e["size"], name, suffix))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def inspect_package(deb_path, inspect_dir):
    """Extract control files and the file list into inspect_dir.

    Returns ({"scripts": {name: sha256}, "flags": [...]}, installed_size_bytes).
    """
    os.makedirs(inspect_dir, exist_ok=True)
    control_dir = os.path.join(inspect_dir, "control")
    if os.path.isdir(control_dir) and not os.path.islink(control_dir):
        shutil.rmtree(control_dir)      # exactly the directory an earlier inspect created
    debinfo.extract_control(deb_path, control_dir)
    files = debinfo.file_list(deb_path)
    _write_files_list(os.path.join(inspect_dir, "files.txt"), files)
    scripts = read_scripts(control_dir)
    flags = file_flags(files) + script_flags(scripts)
    inspected = {"scripts": {n: hashlib.sha256(d).hexdigest() for n, d in scripts.items()},
                 "flags": _dedupe(flags)[:FLAG_LIMIT]}
    return inspected, debinfo.installed_size_bytes(_control_fields(inspect_dir), files)


def _dedupe(items):
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# diff

def _control_fields(inspect_dir):
    path = os.path.join(inspect_dir, "control", "control")
    if os.path.islink(path) or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8", errors="replace") as f:
        stanzas = debinfo.parse_stanzas(f.read())
    return stanzas[0] if stanzas else {}


def previous_approved(st, app):
    entry = st.approved().get(app)
    if not entry:
        return None
    return st.find_candidate(app, entry.get("version"), entry.get("sha256"))


def _script_diffs(old_dir, new_dir, names):
    chunks = []
    for name in names:
        old = read_scripts(os.path.join(old_dir, "control")).get(name, b"")
        new = read_scripts(os.path.join(new_dir, "control")).get(name, b"")
        chunks.extend(difflib.unified_diff(
            old.decode("utf-8", "replace").splitlines(keepends=True),
            new.decode("utf-8", "replace").splitlines(keepends=True),
            fromfile="old/" + name, tofile="new/" + name))
    return "".join(chunks)


def diff_against_approved(st, record, inspected):
    """Compare a candidate with the app's currently approved candidate; writes scripts.diff."""
    new_dir = st.inspect_dir(record)
    prev = previous_approved(st, record["app"])
    new_flags = inspected["flags"]
    if prev is None:
        info = {"against": None, "scripts_changed": sorted(inspected["scripts"]),
                "flags_added": list(new_flags), "flags_removed": [], "fields_changed": []}
    else:
        old_dir = st.inspect_dir(prev)
        old_hashes = prev.get("checks", {}).get("inspect", {}).get("scripts", {})
        names = sorted(n for n in set(old_hashes) | set(inspected["scripts"])
                       if old_hashes.get(n) != inspected["scripts"].get(n))
        old_flags = prev.get("checks", {}).get("inspect", {}).get("flags", [])
        old_ctl, new_ctl = _control_fields(old_dir), _control_fields(new_dir)
        info = {"against": prev["version"], "scripts_changed": names,
                "flags_added": [f for f in new_flags if f not in old_flags],
                "flags_removed": [f for f in old_flags if f not in new_flags],
                "fields_changed": [f for f in DEP_FIELDS if old_ctl.get(f) != new_ctl.get(f)]}
        _write_text(os.path.join(new_dir, "scripts.diff"), _script_diffs(old_dir, new_dir, names))
    info["summary"] = _summary(info)
    return info


def _write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _summary(info):
    parts = []
    if info["scripts_changed"]:
        parts.append("scripts changed: " + ", ".join(info["scripts_changed"]))
    if info["flags_added"]:
        parts.append("%d flag(s) added" % len(info["flags_added"]))
    if info["flags_removed"]:
        parts.append("%d flag(s) removed" % len(info["flags_removed"]))
    if info["fields_changed"]:
        parts.append("fields changed: " + ", ".join(info["fields_changed"]))
    return "; ".join(parts) or "no changes"


# milkyway index

def _latest_cached(cache):
    best = None
    for name in os.listdir(cache):
        if name.endswith(".json") and _CACHE_NAME_RE.match(name):
            path = os.path.join(cache, name)
            if best is None or os.path.getmtime(path) > os.path.getmtime(best):
                best = path
    return best[: -len(".json")] if best else None


def _load_cached(cache, key):
    base = os.path.join(cache, key)
    if not (os.path.isfile(base + ".Packages") and os.path.isfile(base + ".json")):
        return None
    return MilkywayIndex(os.path.basename(base), base + ".Packages",
                         state.read_json(base + ".json", {}))


def _summarize(text, packages):
    """Merge Package stanzas of text into packages (new dict); keep the higher version."""
    out = dict(packages)
    for s in debinfo.parse_stanzas(text):
        name, ver = s.get("Package"), s.get("Version")
        if not name or not ver or not debinfo.VERSION_RE.match(ver):
            continue
        if name in out and not debinfo.version_gt(ver, out[name]["version"]):
            continue
        size = int(s["Size"]) if s.get("Size", "").isdigit() else 0
        inst = int(s["Installed-Size"]) * 1024 if s.get("Installed-Size", "").isdigit() else 0
        out[name] = {"version": ver, "size": size, "installed_size": inst}
    return out


def _fetch_milkyway(cfg, fetcher, release):
    texts, packages = [], {}
    for comp in cfg.milkyway_components:
        for arch in (cfg.arch, "all"):
            try:
                text = aptrepo.fetch_packages(fetcher, cfg.milkyway_url, cfg.milkyway_suite,
                                              comp, arch, release, cfg.max_index_bytes)
            except aptrepo.AptError:
                continue
            texts.append(text.rstrip("\n") + "\n\n")
            packages = _summarize(text, packages)
    if not texts:
        raise CheckError("no Packages found in the milkyway Release")
    return "".join(texts), packages


def _write_cache(cache, key, text, packages):
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=cache)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, os.path.join(cache, key + ".Packages"))
    state.write_json_atomic(os.path.join(cache, key + ".json"), packages)
    for name in os.listdir(cache):
        if _CACHE_NAME_RE.match(name) and not name.startswith(key):
            os.unlink(os.path.join(cache, name))      # old index files, exact names we created


def milkyway_index(cfg, st):
    """Packages of the milkyway repo, cached by the SHA256 of its Release file.

    The Release file is fetched over https and not signature-checked (see report).
    """
    cache = st.cache_dir("milkyway-cache")
    fetcher = fetch.Fetcher(cfg, [urllib.parse.urlsplit(cfg.milkyway_url).hostname or ""])
    url = "%s/dists/%s/Release" % (cfg.milkyway_url, cfg.milkyway_suite)
    try:
        data = fetcher.get(url, max_bytes=cfg.max_index_bytes).data
    except fetch.FetchError as e:
        fallback = _latest_cached(cache)
        if fallback is None:
            raise CheckError("cannot fetch milkyway Release: %s" % e) from e
        return _load_cached(cache, os.path.basename(fallback))
    key = hashlib.sha256(data).hexdigest()
    cached = _load_cached(cache, key)
    if cached:
        return cached
    try:
        text, packages = _fetch_milkyway(cfg, fetcher, aptrepo.parse_release(data))
    except (aptrepo.AptError, fetch.FetchError, debinfo.DebError) as e:
        raise CheckError("cannot build milkyway index: %s" % e) from e
    _write_cache(cache, key, text, packages)
    return _load_cached(cache, key)


# dependencies

def fg_stanza(fields):
    for need in ("Package", "Version", "Architecture"):
        if not fields.get(need):
            raise CheckError("deb control has no %s" % need)
    lines = []
    for name in FG_FIELDS:
        if fields.get(name):
            lines.append("%s: %s" % (name, " ".join(fields[name].split())))
    return "\n".join(lines) + "\n"


def check_deps(cfg, st, fields, index=None):
    """dose-debcheck of one candidate against milkyway. Never raises: failures become ok=False."""
    try:
        index = index or milkyway_index(cfg, st)
        return _run_dose(cfg, fields, index)
    except (CheckError, debinfo.DebError) as e:
        return {"ok": False, "detail": ("dependency check could not run: %s" % e)[:DOSE_DETAIL_MAX],
                "milkyway": getattr(index, "release_sha256", None)}


def _run_dose(cfg, fields, index):
    with tempfile.TemporaryDirectory(prefix="dose-") as work:
        fg = os.path.join(work, "fg.Packages")
        _write_text(fg, fg_stanza(fields))
        proc = debinfo.run_tool(["dose-debcheck", "--deb-native-arch=" + cfg.arch, "--explain",
                                 "--failures", "--bg", index.packages_path, "--fg", fg], timeout=900)
    out = proc.stdout.decode("utf-8", "replace")
    if proc.returncode not in (0, 1):
        err = proc.stderr.decode("utf-8", "replace")
        raise CheckError("dose-debcheck failed (%d): %s" % (proc.returncode, err[:500]))
    detail = "" if proc.returncode == 0 else out[:DOSE_DETAIL_MAX]
    return {"ok": proc.returncode == 0, "detail": detail, "milkyway": index.release_sha256}


# run_check

def _problem(app, message, level="error"):
    return {"level": level, "app": app, "message": message}


def update_milkyway(st, apps, index, now):
    """Refresh st.milkyway() for kind: milkyway apps. Returns problems."""
    data = dict(st.milkyway())
    problems = []
    for app_id, app in sorted(apps.items()):
        pkg = app.get("package", {})
        if pkg.get("kind") != "milkyway":
            continue
        name = pkg.get("name")
        found = index.packages.get(name)
        if found is None:
            data.pop(name, None)
            problems.append(_problem(app_id, "package %s is not in milkyway" % name))
            continue
        old = data.get(name, {})
        first = old.get("first_seen") if old.get("version") == found["version"] else None
        data[name] = {"version": found["version"], "first_seen": first or now,
                      "size": found["size"], "installed_size": found["installed_size"]}
    st.save_milkyway(data)
    return problems


def _candidate_fields(cfg, st, rec):
    rel = rec.get("file")
    if rel:
        root = os.path.realpath(cfg.downloads)
        path = os.path.realpath(os.path.join(root, rel))
        if path.startswith(root + os.sep) and os.path.isfile(path):
            return debinfo.control_fields(path)
    fields = _control_fields(st.inspect_dir(rec))
    if not fields:
        raise CheckError("no deb or control data for %s %s" % (rec["app"], rec["version"]))
    return fields


def recheck_candidates(cfg, st, index, app):
    problems = []
    for status in ("pending", "approved"):
        for rec in st.candidates(app=app, status=status):
            try:
                deps = check_deps(cfg, st, _candidate_fields(cfg, st, rec), index)
            except (CheckError, debinfo.DebError) as e:
                problems.append(_problem(rec["app"], "%s %s: %s" % (rec["app"], rec["version"], e)))
                continue
            checks = dict(rec.get("checks", {}), deps=deps)
            st.update_candidate(rec, {"checks": checks})
            if not deps["ok"]:
                problems.append(_problem(rec["app"], "%s %s (%s): dependencies not installable"
                                         % (rec["app"], rec["version"], status)))
    return problems


def run_check(cfg, st, content, app=None):
    """`check` command: refresh milkyway versions, recheck dependencies. Returns problems."""
    apps = dict(content.apps)
    if app is not None:
        if app not in apps:
            return [_problem(app, "unknown app %s" % app)]
        apps = {app: apps[app]}
    try:
        index = milkyway_index(cfg, st)
    except CheckError as e:
        return [_problem(app, str(e))]
    problems = update_milkyway(st, apps, index, debinfo.utc_now())
    return problems + recheck_candidates(cfg, st, index, app)
