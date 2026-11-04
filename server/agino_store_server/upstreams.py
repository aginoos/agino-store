# SPDX-License-Identifier: GPL-3.0-or-later
"""upstream-check: follow the tags of every tracked upstream (docs/server.md section 14, contract 7.3).

state/upstreams/<app>.json holds {last_check, last_ok, last_error, latest_version,
seen: {<version>: {tag, commit, found}}, tag_moved: [{version, tag, old_commit, new_commit, at}]}.
The commit first seen for a version is the pin: a tag that later points elsewhere is only recorded.
"""

import functools
import tempfile
from dataclasses import dataclass, field

from . import builds, debinfo, upstream_fetch, upstream_tags
from .state import StateError

MAX_SEEN = 300
MAX_MOVED = 50
AUTO_USER = "auto"
EXPECTED_ERRORS = (upstream_fetch.UpstreamError, upstream_tags.TagError, debinfo.DebError)


class UnknownUpstream(LookupError):
    pass


@dataclass
class Outcome:
    line: str
    problem: bool = False
    new_versions: list = field(default_factory=list)
    new_moves: list = field(default_factory=list)


def _compare(a, b):
    if a == b:
        return 0
    return 1 if debinfo.version_gt(a, b) else -1


def _newest_first(versions):
    return sorted(versions, key=functools.cmp_to_key(_compare), reverse=True)


def pick_versions(up, tags, seen):
    """{version: (tag, commit)} for the tags the upstream's match accepts (and min_version allows).

    When several tags give one version, the tag already recorded for it wins, else the first by name.
    """
    by_tag = upstream_tags.versions_of(up.match, tags)
    chosen = {}
    for tag in sorted(by_tag):
        version = by_tag[tag]
        if not upstream_tags.at_least(version, up.min_version):
            continue
        recorded = (seen.get(version) or {}).get("tag")
        if version not in chosen or tag == recorded:
            chosen[version] = tag
    return {v: (t, tags[t]) for v, t in chosen.items()}


def merge(memo, found, now):
    """(seen, tag_moved, new_versions, new_moves) after folding the tags found now into the memo."""
    seen = {v: e for v, e in (memo.get("seen") or {}).items() if isinstance(e, dict)}
    moved = [m for m in (memo.get("tag_moved") or []) if isinstance(m, dict)]
    new_versions, new_moves = [], []
    for version, (tag, commit) in found.items():
        entry = seen.get(version)
        if entry is None:
            seen[version] = {"tag": tag, "commit": commit, "found": now}
            new_versions.append(version)
        elif entry.get("tag") == tag and entry.get("commit") != commit:
            known = any(m.get("version") == version and m.get("new_commit") == commit for m in moved)
            if not known:
                move = {"version": version, "tag": tag, "old_commit": entry.get("commit"), "new_commit": commit,
                        "at": now}
                moved.append(move)
                new_moves.append(move)
    keep = set(_newest_first(seen)[:MAX_SEEN])
    return ({v: e for v, e in seen.items() if v in keep}, moved[-MAX_MOVED:],
            [v for v in new_versions if v in keep], new_moves)


def _latest(seen):
    ordered = _newest_first(seen)
    return ordered[0] if ordered else None


def _describe(app, memo, outcome):
    latest = memo.get("latest_version") or "-"
    text = "%s: latest %s, %d new" % (app, latest, len(outcome.new_versions))
    for move in outcome.new_moves:
        text += "; TAG MOVED %s (%s) %s -> %s" % (move["version"], move["tag"], str(move["old_commit"])[:12],
                                                  move["new_commit"][:12])
    return debinfo.clean_text(text)


def check_one(cfg, st, up, now):
    """Check one upstream, update its memo. Never raises for upstream trouble (it lands in last_error)."""
    memo = st.upstream_memo(up.app)
    new = dict(memo, last_check=now)
    outcome = Outcome("")
    try:
        if up.track_type != "tag":
            raise upstream_fetch.UpstreamError("track type %s is not implemented yet" % up.track_type)
        with tempfile.TemporaryDirectory(prefix="agino-ls-") as scratch:
            tags = upstream_fetch.list_tags(cfg, up.repo, scratch)
        found = pick_versions(up, tags, memo.get("seen") or {})
        seen, moved, outcome.new_versions, outcome.new_moves = merge(memo, found, now)
        new.update(last_ok=now, last_error=None, seen=seen, tag_moved=moved, latest_version=_latest(seen))
        outcome.problem = bool(outcome.new_moves)
    except EXPECTED_ERRORS as e:
        message = debinfo.clean_text("%s: %s" % (type(e).__name__, e))
        new["last_error"] = message
        outcome = Outcome("%s: error: %s" % (up.app, message), problem=True)
    st.save_upstream_memo(up.app, new)
    if not outcome.line:
        outcome.line = _describe(up.app, new, outcome)
    return outcome, memo, new


def _auto_candidate(up, before, outcome):
    """The one version to build automatically: the newest new one above everything seen before."""
    if not up.auto_task or not before.get("last_ok") or not outcome.new_versions:
        return None   # the first successful check only records the baseline
    previous = before.get("latest_version")
    fresh = [v for v in outcome.new_versions if previous is None or debinfo.version_gt(v, previous)]
    return _newest_first(fresh)[0] if fresh else None


def _auto_request(cfg, st, content, up, version, now):
    try:
        record = builds.request(cfg, st, content, up.app, version, AUTO_USER, now=now)
    except (builds.BuildError, StateError) as e:
        return " (no build task: %s)" % debinfo.clean_text(e)
    return " (build task %s requested)" % record["id"]


def check_all(cfg, st, content, app=None, now=None):
    """`upstream-check`: (lines, problem). `app` limits the check to one upstream; UnknownUpstream if it has none."""
    now = now or debinfo.utc_now()
    chosen = sorted(content.upstreams, key=lambda u: u.app)
    if app is not None:
        chosen = [u for u in chosen if u.app == app]
        if not chosen:
            raise UnknownUpstream("%s has no upstreams/%s.yaml" % (app, app))
    lines, problem = [], False
    for up in chosen:
        outcome, before, after = check_one(cfg, st, up, now)
        candidate = _auto_candidate(up, before, outcome)
        if candidate:
            outcome.line += _auto_request(cfg, st, content, up, candidate, now)
        lines.append(outcome.line)
        problem = problem or outcome.problem
    return lines, problem
