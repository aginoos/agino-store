# SPDX-License-Identifier: GPL-3.0-or-later
"""Turn a list of upstream tags into versions with the recipe's `track.match` (contract 7.3).

The regular expression comes from the content repository (an administrator wrote it, but it is still
run in a child process with a time limit, like the vendor regexes in trackers.py).
"""

import json
import sys

from . import debinfo
from .content_builds import UPSTREAM_VERSION_RE

TIMEOUT = 20
MAX_OUTPUT = 8 << 20
_SCRIPT = """
import json, re, sys
req = json.load(sys.stdin)
rx = re.compile(req["pattern"])
out = {}
for tag in req["tags"]:
    m = rx.fullmatch(tag)
    if m:
        out[tag] = ".".join(g for g in m.groups() if g)
print(json.dumps(out))
"""


class TagError(Exception):
    pass


def versions_of(match, tags):
    """{tag: version} for the tags that match completely and give a valid version string."""
    request = json.dumps({"pattern": match, "tags": sorted(tags)}).encode("utf-8")
    try:
        proc = debinfo.run_tool([sys.executable, "-I", "-c", _SCRIPT], timeout=TIMEOUT, stdin=request,
                                max_stdout=MAX_OUTPUT)
    except debinfo.DebError as e:
        raise TagError("track.match did not finish: %s" % debinfo.clean_text(e)) from e
    try:
        found = json.loads(proc.stdout)
    except ValueError as e:
        raise TagError("track.match helper failed") from e
    if proc.returncode != 0 or not isinstance(found, dict):
        raise TagError("track.match helper failed")
    return {tag: v for tag, v in found.items() if isinstance(v, str) and UPSTREAM_VERSION_RE.match(v)}


def at_least(version, minimum):
    """True when `version` >= `minimum` (Debian ordering); no minimum accepts everything."""
    return minimum is None or version == minimum or debinfo.version_gt(version, minimum)


def newest(versions):
    """The highest of an iterable of version strings, or None."""
    best = None
    for v in versions:
        if best is None or debinfo.version_gt(v, best):
            best = v
    return best
