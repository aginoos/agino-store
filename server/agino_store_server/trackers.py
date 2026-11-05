# SPDX-License-Identifier: GPL-3.0-or-later
"""The five vendor tracking types and manual intake (docs/server.md sections 5 and 6).

Adapter used from the content module (all read-only):
  content.vendors  dict vendor_id -> parsed vendors/<id>.yaml
  content.apps     dict app_id -> parsed app.yaml (package: {kind, name, vendor})
  content.root     path of the store-content checkout
"""

import getpass
import json
import os
import sys
import urllib.parse
from dataclasses import dataclass, field

from . import aptrepo, checks, debinfo, fetch
from .state import StateError

MAX_PAGE_MATCHES = 200
MAX_REGEX_TEXT = 1 << 20
REGEX_TIMEOUT = 10
SAME_FILE_CHECKED = ("json", "page", "redirect")
# Runs vendor-supplied regexes away from the server process (catastrophic backtracking).
_REGEX_SCRIPT = """
import json, re, sys
req = json.load(sys.stdin)
try:
    rx = re.compile(req["pattern"])
except re.error as e:
    print(json.dumps({"error": "bad regex: %s" % e}))
    sys.exit(0)
found = rx.finditer(req["text"]) if req["mode"] == "finditer" else filter(None, [rx.search(req["text"])])
out = []
for m in found:
    d = m.groupdict()
    out.append({"url": d.get("url"), "version": d.get("version")})
    if len(out) >= req["limit"]:
        break
print(json.dumps({"has_version": "version" in rx.groupindex, "matches": out}))
"""


class TrackError(Exception):
    pass


@dataclass(frozen=True)
class Found:
    version: str | None
    url: str
    origin: dict
    sha256: str | None = None
    size: int | None = None
    filename: str | None = None
    apt: aptrepo.AptResult | None = None
    unchanged: bool = False
    memo: dict = field(default_factory=dict)     # fields to merge into the vendor memo on success


@dataclass(frozen=True)
class Target:
    app_id: str
    vendor_id: str
    vendor: dict


def _problem(level, vendor_id, app_id, message):
    return {"level": level, "vendor": vendor_id, "app": app_id, "message": debinfo.clean_text(message)}


# Discovery, one function per track type: (cfg, fetcher, vendor, root, memo) -> Found

def _origin_https(url):
    return {"type": "https", "url": url}


def _dotted(obj, path):
    for part in path.split("."):
        if not isinstance(obj, dict) or part not in obj:
            raise TrackError("missing %r in JSON" % path)
        obj = obj[part]
    return obj


def _checked_version(value):
    if not isinstance(value, str) or not debinfo.VERSION_RE.match(value):
        raise TrackError("bad version string: %r" % (value,))
    return value


def discover_apt(cfg, fetcher, vendor, root, memo):
    res = aptrepo.find_latest(cfg, fetcher, vendor, root, memo)
    origin = {"type": "apt", "signed_by": res.signed_by, "inrelease_sha256": res.inrelease_sha256}
    return Found(res.version, res.deb_url, origin, sha256=res.sha256, size=res.size,
                 filename=res.filename, apt=res, memo={"inrelease_date": res.inrelease_date,
                           "inrelease_sha256": res.inrelease_sha256})


def _parse_json_text(text, strip_js):
    if strip_js:
        start = text.find("{")
        if start < 0:
            raise TrackError("no JSON object in response")
        return json.JSONDecoder().raw_decode(text[start:])[0]
    return json.loads(text)


def discover_json(cfg, fetcher, vendor, root, memo):
    track = vendor["track"]
    text = fetcher.get(track["url"], max_bytes=cfg.max_index_bytes).data.decode("utf-8", "replace")
    try:
        obj = _parse_json_text(text, bool(track.get("strip_js")))
    except ValueError as e:
        raise TrackError("response is not valid JSON") from e
    version = _checked_version(_dotted(obj, track["version_path"]))
    url = _dotted(obj, track["url_path"])
    if not isinstance(url, str):
        raise TrackError("download URL is not a string")
    url = urllib.parse.urljoin(track["url"], url)
    return Found(version, url, _origin_https(url))


def _same_validators(memo, resp):
    pairs = [(memo.get("etag"), resp.etag), (memo.get("last_modified"), resp.last_modified),
             (memo.get("content_length"), resp.content_length)]
    present = [(old, new) for old, new in pairs if new is not None]
    return bool(present) and all(old == new for old, new in present)


def discover_head(cfg, fetcher, vendor, root, memo):
    resp = fetcher.head(vendor["track"]["url"])
    validators = {"etag": resp.etag, "last_modified": resp.last_modified,
                  "content_length": resp.content_length}
    return Found(None, resp.url, _origin_https(resp.url), unchanged=_same_validators(memo, resp),
                 memo=validators)


def regex_matches(pattern, text, mode, limit=MAX_PAGE_MATCHES):
    """Run a vendor regex over at most 1 MiB of text in a child process with a 10 s timeout.

    Returns {"has_version": bool, "matches": [{"url": str|None, "version": str|None}]}.
    """
    if not isinstance(pattern, str):
        raise TrackError("regex must be a string")
    request = json.dumps({"pattern": pattern, "text": text[:MAX_REGEX_TEXT], "mode": mode, "limit": limit})
    try:
        proc = debinfo.run_tool([sys.executable, "-I", "-c", _REGEX_SCRIPT], timeout=REGEX_TIMEOUT,
                                stdin=request.encode("utf-8"))
    except debinfo.DebError as e:
        raise TrackError("regex did not finish: %s" % e) from e
    try:
        result = json.loads(proc.stdout)
    except ValueError as e:
        raise TrackError("regex helper failed") from e
    if proc.returncode != 0 or not isinstance(result, dict):
        raise TrackError("regex helper failed")
    if result.get("error"):
        raise TrackError(str(result["error"]))
    return result


def _best_match(matches, has_version):
    if not has_version:
        return matches[0]
    best = None
    for m in matches:
        v = _checked_version(m["version"])
        if best is None or debinfo.version_gt(v, best["version"]):
            best = m
    return best


def discover_page(cfg, fetcher, vendor, root, memo):
    track = vendor["track"]
    text = fetcher.get(track["url"], max_bytes=cfg.max_index_bytes).data.decode("utf-8", "replace")
    result = regex_matches(track["link_regex"], text, "finditer")
    matches = [m for m in result["matches"] if isinstance(m.get("url"), str)]
    if not matches:
        raise TrackError("link_regex matched nothing on the page")
    has_version = bool(result["has_version"])
    best = _best_match(matches, has_version)
    url = urllib.parse.urljoin(track["url"], best["url"])
    version = _checked_version(best["version"]) if has_version else None
    return Found(version, url, _origin_https(url))


def discover_redirect(cfg, fetcher, vendor, root, memo):
    track = vendor["track"]
    final = fetcher.head(track["url"]).url
    result = regex_matches(track["version_regex"], final, "search", limit=1)
    if not result["matches"] or result["matches"][0].get("version") is None:
        raise TrackError("version_regex did not match the final URL")
    return Found(_checked_version(result["matches"][0]["version"]), final, _origin_https(final))


DISCOVER = {"apt": discover_apt, "json": discover_json, "head": discover_head,
            "page": discover_page, "redirect": discover_redirect}


# Decisions

def classify(st, app_id, version, sha256, approved):
    """Return ("skip"|"new"|"alert", message)."""
    same = [r for r in st.candidates(app=app_id) if r.get("version") == version]
    if same:
        known = {r.get("sha256") for r in same} - {None}
        if sha256 and known and sha256 not in known:
            return "alert", ("version %s of %s appeared again with a different sha256 (%s)"
                             % (version, app_id, sha256[:12]))
        return "skip", ""
    if approved and not debinfo.version_gt(version, approved["version"]):
        return "skip", ""
    return "new", ""


def verify_fields(vendor, fields, expected_version):
    if fields.get("Package") != vendor["package"]:
        raise TrackError("deb Package %r is not %r" % (fields.get("Package"), vendor["package"]))
    if fields.get("Architecture") not in (vendor["arch"], "all"):
        raise TrackError("deb Architecture %r does not match %r" % (fields.get("Architecture"), vendor["arch"]))
    _checked_version(fields.get("Version"))
    if expected_version is not None and fields["Version"] != expected_version:
        raise TrackError("deb Version %s differs from tracked version %s" % (fields["Version"], expected_version))


# Candidates

def _record(target, dl, fields, vendor_url, origin, found_at):
    return {
        "app": target.app_id, "vendor": target.vendor_id, "package": target.vendor["package"],
        "arch": target.vendor["arch"], "version": fields["Version"], "sha256": dl.sha256,
        "size": dl.size, "installed_size": None, "filename": dl.filename,
        "file": "%s/%s" % (dl.sha256, dl.filename), "vendor_url": vendor_url, "origin": origin,
        "found": found_at, "status": "pending", "checks": {}, "decided": None,
    }


def ingest(cfg, st, target, dl, vendor_url, origin, expected_version, awaiting=None):
    """Inspect, diff and dependency-check a downloaded deb, then store it as a pending candidate."""
    fields = debinfo.control_fields(dl.path)
    verify_fields(target.vendor, fields, expected_version)
    record = _record(target, dl, fields, vendor_url, origin, debinfo.utc_now())
    inspected, installed = checks.inspect_package(dl.path, st.inspect_dir(record))
    record = dict(record, installed_size=installed)
    diff = checks.diff_against_approved(st, record, inspected)
    deps = checks.check_deps(cfg, st, fields)
    all_checks = {"deps": deps, "inspect": inspected, "diff": diff, "sandbox": {"status": "missing"}}
    record = dict(record, checks=all_checks)
    if awaiting is not None:
        return st.update_candidate(awaiting, {k: v for k, v in record.items() if k != "found"})
    return st.add_candidate(record)


def _awaiting_record(target, found):
    return {
        "app": target.app_id, "vendor": target.vendor_id, "package": target.vendor["package"],
        "arch": target.vendor["arch"], "version": found.version, "sha256": None, "size": None,
        "installed_size": None, "filename": None, "file": None, "vendor_url": found.url,
        "origin": found.origin, "found": debinfo.utc_now(), "status": "awaiting-upload",
        "checks": {"sandbox": {"status": "missing"}}, "decided": None,
    }


def _download(cfg, fetcher, found, memo, vendor):
    if found.apt is not None:
        return aptrepo.download_deb(cfg, fetcher, found.apt)
    conditional = vendor["track"]["type"] == "head"
    name = fetch.sanitize_filename(urllib.parse.urlsplit(found.url).path)
    if not name.endswith(".deb"):
        name += ".deb"
    return fetcher.download(found.url, cfg.downloads, filename=name,
                            etag=memo.get("etag") if conditional else None,
                            last_modified=memo.get("last_modified") if conditional else None)


def _after_download(cfg, st, target, found, dl, approved):
    fields = debinfo.control_fields(dl.path)
    verify_fields(target.vendor, fields, found.version)
    version = fields["Version"]
    if found.version is None:
        verdict, message = classify(st, target.app_id, version, dl.sha256, approved)
        if verdict == "alert":
            return [_problem("alert", target.vendor_id, target.app_id, message)], version
        if verdict == "skip":
            return [], version
    ingest(cfg, st, target, dl, found.url, found.origin, found.version)
    msg = "new candidate %s %s" % (target.app_id, version)
    return [_problem("info", target.vendor_id, target.app_id, msg)], version


def same_file_alert(st, fetcher, target, found):
    """Same version again: the download must still have the size of the candidate we hold."""
    if target.vendor["track"]["type"] not in SAME_FILE_CHECKED:
        return []
    known = st.find_candidate(target.app_id, found.version)
    if known is None or known.get("size") is None:
        return []
    try:
        length = fetcher.head(found.url).content_length
    except fetch.FetchError as e:
        return [_problem("info", target.vendor_id, target.app_id,
                         "cannot HEAD %s to compare sizes: %s" % (found.url, e))]
    if length is None or length == known["size"]:
        return []
    return [_problem("alert", target.vendor_id, target.app_id,
                     "version %s of %s is now %d bytes, the candidate has %d (same version, different file)"
                     % (found.version, target.app_id, length, known["size"]))]


def process_app(cfg, st, fetcher, target, found, memo):
    """Returns (problems, seen_version)."""
    if found.unchanged:
        return [], memo.get("seen_version")
    approved = st.approved().get(target.app_id)
    if found.version is not None:
        verdict, message = classify(st, target.app_id, found.version, found.sha256, approved)
        if verdict == "alert":
            return [_problem("alert", target.vendor_id, target.app_id, message)], found.version
        if verdict == "skip":
            return same_file_alert(st, fetcher, target, found), found.version
        if target.vendor["fetch"] == "manual":
            st.add_candidate(_awaiting_record(target, found))
            msg = "new version %s of %s awaits manual upload (%s)" % (found.version, target.app_id, found.url)
            return [_problem("info", target.vendor_id, target.app_id, msg)], found.version
    dl = _download(cfg, fetcher, found, memo, target.vendor)
    if dl.not_modified:
        return [], memo.get("seen_version")
    return _after_download(cfg, st, target, found, dl, approved)


# Trial run (AI intake of a vendor application: does this tracking file really find and fetch the deb?)

@dataclass(frozen=True)
class Trial:
    version: str              # Version of the deb that was downloaded
    url: str                  # where it was found
    origin: dict              # the origin a candidate of it would get (https type)
    download: fetch.Download  # the file, in the content-addressed downloads directory
    inspected: dict           # checks.inspect_package result
    deps: dict                # checks.check_deps result


def trial_run(cfg, st, target, workdir):
    """Find, download and check the newest deb of `target` once, exactly as `track` would, but record nothing: no
    candidate, no memo, and the inspect files go to `workdir`. The deb itself lands in cfg.downloads (content
    addressed, so a later `track` reuses it). `st` is only used for the cached milkyway index of the dependency
    check. Raises TrackError, fetch.FetchError or debinfo.DebError when the tracking file does not work."""
    vendor = target.vendor
    _check_manual(vendor)
    fetcher = fetch.Fetcher(cfg, vendor["allowed_hosts"])
    found = DISCOVER[vendor["track"]["type"]](cfg, fetcher, vendor, "", {})
    dl = _download(cfg, fetcher, found, {}, vendor)
    fields = debinfo.control_fields(dl.path)
    verify_fields(vendor, fields, found.version)
    inspected, _ = checks.inspect_package(dl.path, os.path.join(workdir, "inspect"))
    return Trial(fields["Version"], found.url, found.origin, dl, inspected, checks.check_deps(cfg, st, fields))


# Per vendor / all vendors

def target_for(content, vendor_id):
    vendor = content.vendors[vendor_id]
    apps = sorted(a for a, app in content.apps.items()
                  if app.get("package", {}).get("kind") == "deb"
                  and app["package"].get("vendor") == vendor_id)
    if len(apps) != 1:
        raise TrackError("vendor %s must be used by exactly one app (found %d)" % (vendor_id, len(apps)))
    return Target(apps[0], vendor_id, vendor)


def _check_manual(vendor):
    if vendor["fetch"] == "manual" and vendor["track"]["type"] == "head":
        raise TrackError("fetch: manual cannot be used with track type head")


def track_vendor(cfg, st, content, vendor_id):
    """Track one vendor; always updates its memo and never raises."""
    now = debinfo.utc_now()
    memo = st.vendor_memo(vendor_id)
    new_memo = dict(memo, last_check=now)
    try:
        target = target_for(content, vendor_id)
        _check_manual(target.vendor)
        fetcher = fetch.Fetcher(cfg, target.vendor["allowed_hosts"])
        found = DISCOVER[target.vendor["track"]["type"]](cfg, fetcher, target.vendor, content.root, memo)
        problems, seen = process_app(cfg, st, fetcher, target, found, memo)
        new_memo.update(found.memo)
        new_memo["seen_version"] = seen
        alerts = [p for p in problems if p["level"] == "alert"]
        if alerts:
            new_memo["last_error"] = alerts[0]["message"]
        else:
            new_memo.update(last_ok=now, last_error=None)
    except Exception as e:  # one vendor failing must not stop the others
        message = "%s: %s" % (type(e).__name__, e)
        problems = [_problem("error", vendor_id, None, message)]
        new_memo["last_error"] = message
    st.save_vendor_memo(vendor_id, new_memo)
    return problems


def run_track(cfg, st, content, vendor=None):
    """`track` command. Returns events/problems: dicts with level info|alert|error.

    The CLI should exit 1 if any entry has level alert or error.
    """
    if vendor is not None:
        if vendor not in content.vendors:
            return [_problem("error", vendor, None, "unknown vendor %s" % vendor)]
        ids = [vendor]
    else:
        ids = sorted(content.vendors)
    out = []
    for vid in ids:
        out.extend(track_vendor(cfg, st, content, vid))
    return out


# Manual intake

def _current_user():
    try:
        return os.environ.get("SUDO_USER") or getpass.getuser()
    except (KeyError, OSError):
        return "unknown"


def _intake_target(st, target, version):
    awaiting = st.candidates(app=target.app_id, status="awaiting-upload")
    match = next((r for r in awaiting if r["version"] == version), None)
    if match is not None:
        return match
    if awaiting:
        raise TrackError("awaiting version is %s, the file is %s" % (awaiting[0]["version"], version))
    approved = st.approved().get(target.app_id)
    if approved and not debinfo.version_gt(version, approved["version"]):
        raise TrackError("version %s is not higher than the approved %s" % (version, approved["version"]))
    return None


def check_intake_url(url, vendor):
    """The recorded download URL must be None or an https URL on one of the vendor's hosts."""
    if url is None:
        return
    parts = urllib.parse.urlsplit(str(url))
    allowed = {h.lower() for h in vendor.get("allowed_hosts", [])}
    if (parts.scheme != "https" or parts.username is not None or parts.password is not None
            or (parts.hostname or "").lower() not in allowed):
        raise TrackError("intake url must be https on one of the vendor's allowed_hosts")


def intake(cfg, st, content, vendor_id, path, url):
    """Take a manually downloaded deb into the candidate list. Returns the candidate record."""
    if vendor_id not in content.vendors:
        raise TrackError("unknown vendor %s" % vendor_id)
    target = target_for(content, vendor_id)
    check_intake_url(url, target.vendor)
    if os.path.islink(path) or not os.path.isfile(path):
        raise TrackError("not a regular file: %s" % path)
    if os.path.getsize(path) > cfg.max_deb_bytes:
        raise TrackError("file exceeds the size limit")
    fields = debinfo.control_fields(path)
    verify_fields(target.vendor, fields, None)
    version = fields["Version"]
    awaiting = _intake_target(st, target, version)
    stored, sha, size = fetch.store_file(path, cfg.downloads, os.path.basename(path))
    same = st.find_candidate(target.app_id, version)
    if same is not None and awaiting is None:
        if same.get("sha256") == sha:
            return same
        raise TrackError("version %s already has a candidate with different content" % version)
    dl = fetch.Download(url or "", stored, sha, size, os.path.basename(stored), None, None)
    origin = {"type": "manual", "by": _current_user(), "url": url}
    try:
        return ingest(cfg, st, target, dl, url or "", origin, None, awaiting=awaiting)
    except StateError as e:
        raise TrackError(str(e)) from e
