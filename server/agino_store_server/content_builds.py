# SPDX-License-Identifier: GPL-3.0-or-later
"""Strict schema of upstreams/<id>.yaml and apps/<id>/build.yaml (docs/server.md section 14).

Called from content.load with the shared error collector (`chk`); nothing here reads files except
through the `load_yaml` callable it is given. Both files are written by administrators, but every
field is still checked as if it were hostile: the recipe's steps run inside the build VM and the
upstream URL is handed to git.
"""

import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .buildcfg import LIMIT_KEYS, Limits

ID_RE = re.compile(r"\A[a-z0-9][a-z0-9.+-]*\Z")
UPSTREAM_VERSION_RE = re.compile(r"\A[0-9][0-9A-Za-z.+~-]{0,63}\Z")
DEB_PACKAGE_RE = re.compile(r"\A[a-z0-9][a-z0-9+.-]{1,127}\Z")
SECTION_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,31}\Z")
MAINTAINER_RE = re.compile(r"\A[^<>\x00-\x1f\x7f]{1,100} <[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,200}>\Z")
REPO_PATH_RE = re.compile(r"\A(/[A-Za-z0-9._~-]{1,100}){2,6}\Z")
SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
NESTED_QUANTIFIER_RE = re.compile(r"[+*}]\??\)[+*{]")   # same heuristic as the vendor regexes
TRACK_TYPES = ("tag", "release")
SOURCE_KINDS = ("git", "tarball")
BASES = ("debian-trixie",)
MAX_MATCH_LEN = 200
MAX_STEPS = 40
MAX_STEP_LEN = 512
MAX_DEPENDS = 100
RECIPE_FILE = "build.yaml"


@dataclass(frozen=True)
class Upstream:
    app: str
    repo: str
    track_type: str
    match: str
    min_version: str | None
    build: str              # apps/<app>/build.yaml
    auto_task: bool


@dataclass(frozen=True)
class Recipe:
    app: str
    base: str
    source: dict            # {kind, checksum, url}; checksum/url may be None
    build_depends: tuple
    steps: tuple
    package: dict           # {name, section, depends (tuple), maintainer}
    limits: Limits          # effective: the recipe's value, else the global ceiling

    def as_task(self):
        """The recipe as plain JSON data for task.json."""
        return {"base": self.base, "source": dict(self.source), "build_depends": list(self.build_depends),
                "steps": list(self.steps),
                "package": dict(self.package, depends=list(self.package["depends"])),
                "limits": self.limits.as_dict()}


def _one_line(chk, path, value, max_len):
    text = chk.string(path, value, max_len=max_len)
    if text is not None and "\n" in text:
        chk.fail(path, "must be a single line")
        return None
    return text


def _split(text):
    try:
        return urlsplit(text)
    except ValueError:
        return None


def public_url(chk, path, value, policy, allow_insecure, allow_file=False):
    """An https URL on an allowed host, without credentials, query or fragment, path of 2..6 safe segments."""
    text = _one_line(chk, path, value, 300)
    if text is None:
        return None
    parts = _split(text)
    if parts is None:
        chk.fail(path, "not a valid URL")
        return None
    if allow_insecure and allow_file and parts.scheme == "file" and parts.path.startswith("/") and not parts.netloc:
        return text
    schemes = ("https", "http") if allow_insecure else ("https",)
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = -1
    port_ok = port is None or port == 443 or (allow_insecure and port != -1)
    ok = (parts.scheme in schemes and host and not parts.username and not parts.password
          and not parts.query and not parts.fragment and port_ok
          and parts.netloc.lower() in (host, "%s:%s" % (host, port)))
    if not ok:
        chk.fail(path, "must be an %s URL without credentials, query or fragment" % "/".join(schemes))
        return None
    if host not in policy.allowed_hosts:
        chk.fail(path, "host is not in [upstream] allowed_hosts")
        return None
    segments = parts.path.split("/")
    if not REPO_PATH_RE.match(parts.path) or any(s in (".", "..") for s in segments):
        chk.fail(path, "path must be 2..6 segments of [A-Za-z0-9._~-]")
        return None
    return text


def _match_regex(chk, path, value):
    text = _one_line(chk, path, value, MAX_MATCH_LEN)
    if text is None:
        return None
    if NESTED_QUANTIFIER_RE.search(text):
        chk.fail(path, "nested quantifiers (catastrophic backtracking) are not allowed")
        return None
    try:
        groups = re.compile(text).groups
    except re.error as e:
        chk.fail(path, "invalid regular expression: %s" % e)
        return None
    if not 1 <= groups <= 6:
        chk.fail(path, "needs 1..6 capture groups (they are joined with '.' into the version)")
        return None
    return text


def _version(chk, path, value):
    if isinstance(value, float):
        chk.fail(path, "must be a quoted string like \"3.0.0\"")
        return None
    text = chk.string(path, value, UPSTREAM_VERSION_RE, 64)
    return text


def _track(chk, path, track):
    if not chk.mapping(path, track, ("type", "match"), ("min_version",)):
        return None
    kind = track["type"]
    if kind not in TRACK_TYPES:
        chk.fail(path + ".type", "must be one of %s" % ", ".join(TRACK_TYPES))
        kind = None
    match = _match_regex(chk, path + ".match", track["match"])
    minimum = _version(chk, path + ".min_version", track["min_version"]) if "min_version" in track else None
    bad_min = "min_version" in track and minimum is None
    return (kind, match, minimum) if kind and match and not bad_min else None


def upstream(chk, rel, data, app_id, has_recipe, policy, allow_insecure):
    """Validate one upstreams/<id>.yaml; returns Upstream or None."""
    before = len(chk.errors)
    if not chk.mapping(rel, data, ("app", "repo", "track", "build"), ("auto_task",)):
        return None
    if data["app"] != app_id:
        chk.fail(rel + ".app", "must be %r (the file name)" % app_id)
    repo = public_url(chk, rel + ".repo", data["repo"], policy, allow_insecure, allow_file=True)
    track = _track(chk, rel + ".track", data["track"])
    expected = "apps/%s/%s" % (app_id, RECIPE_FILE)
    if data["build"] != expected:
        chk.fail(rel + ".build", "must be %r" % expected)
    elif not has_recipe:
        chk.fail(rel + ".build", "%s is missing or invalid" % expected)
    auto = chk.boolean(rel + ".auto_task", data.get("auto_task", False))
    if len(chk.errors) != before or not repo or not track or auto is None:
        return None
    return Upstream(app_id, repo, track[0], track[1], track[2], expected, auto)


# apps/<id>/build.yaml

def _packages(chk, path, value, min_len, max_len):
    items = chk.sequence(path, value, min_len, max_len)
    names = [chk.string("%s[%d]" % (path, i), n, DEB_PACKAGE_RE, 128) for i, n in enumerate(items or [])]
    if items is None or None in names:
        return None
    if len(set(names)) != len(names):
        chk.fail(path, "duplicate package")
        return None
    return tuple(names)


def _source(chk, path, source, policy, allow_insecure):
    if not chk.mapping(path, source, ("kind",), ("checksum", "url")):
        return None
    kind = source["kind"]
    if kind not in SOURCE_KINDS:
        chk.fail(path + ".kind", "must be one of %s" % ", ".join(SOURCE_KINDS))
        return None
    checksum, url = source.get("checksum"), source.get("url")
    if kind == "git":
        if url is not None or (checksum is not None and checksum != "required"):
            chk.fail(path, "kind git takes no url and only 'checksum: required' (the commit is pinned by the task)")
            return None
        return {"kind": "git", "checksum": "required", "url": None}
    checksum = chk.string(path + ".checksum", checksum, SHA256_RE, 64) if "checksum" in source else None
    if "checksum" not in source:
        chk.fail(path + ".checksum", "kind tarball needs the sha256 of the tarball")
    url = public_url(chk, path + ".url", url, policy, allow_insecure) if "url" in source else None
    if "url" not in source:
        chk.fail(path + ".url", "kind tarball needs the https url of the tarball")
    return {"kind": "tarball", "checksum": checksum, "url": url} if checksum and url else None


def _package(chk, path, package):
    if not chk.mapping(path, package, ("name", "section", "maintainer"), ("depends",)):
        return None
    name = chk.string(path + ".name", package["name"], ID_RE, 128)
    section = chk.string(path + ".section", package["section"], SECTION_RE, 32)
    maintainer = chk.string(path + ".maintainer", package["maintainer"], MAINTAINER_RE, 320)
    depends = _packages(chk, path + ".depends", package.get("depends", []), 0, 50)
    if None in (name, section, maintainer, depends):
        return None
    return {"name": name, "section": section, "depends": depends, "maintainer": maintainer}


def _steps(chk, path, value):
    items = chk.sequence(path, value, 1, MAX_STEPS)
    steps = [_one_line(chk, "%s[%d]" % (path, i), s, MAX_STEP_LEN) for i, s in enumerate(items or [])]
    return tuple(steps) if items and None not in steps else None


def _limits(chk, path, value, ceiling):
    if value is None:
        return ceiling
    if not chk.mapping(path, value, (), LIMIT_KEYS):
        return None
    out, ok = ceiling.as_dict(), True
    for key in value:
        number = value[key]
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= getattr(ceiling, key):
            chk.fail("%s.%s" % (path, key), "must be an integer between 1 and the global limit %d" % getattr(ceiling, key))
            ok = False
        else:
            out[key] = number
    return Limits(**out) if ok else None


def recipe(chk, rel, data, app_id, policy, allow_insecure):
    """Validate one apps/<id>/build.yaml; returns Recipe or None."""
    before = len(chk.errors)
    required = ("base", "source", "build_depends", "steps", "package")
    if not chk.mapping(rel, data, required, ("limits",)):
        return None
    if data["base"] not in BASES:
        chk.fail(rel + ".base", "must be one of %s" % ", ".join(BASES))
    source = _source(chk, rel + ".source", data["source"], policy, allow_insecure)
    depends = _packages(chk, rel + ".build_depends", data["build_depends"], 0, MAX_DEPENDS)
    steps = _steps(chk, rel + ".steps", data["steps"])
    package = _package(chk, rel + ".package", data["package"])
    limits = _limits(chk, rel + ".limits", data.get("limits"), policy.limits)
    if len(chk.errors) != before or None in (source, depends, steps, package, limits):
        return None
    return Recipe(app_id, data["base"], source, depends, steps, package, limits)


# Directory scans

def _scan(chk, root, directory, app_ids):
    """(app id, relative path) of every file below `directory` (one level); stray names are errors."""
    base = os.path.join(root, directory)
    out = []
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        app_id = name[:-5] if name.endswith(".yaml") else name
        if not name.endswith(".yaml") or not ID_RE.match(app_id) or app_id not in app_ids:
            chk.fail("%s/%s" % (directory, name), "must be <app-id>.yaml of an existing app")
            continue
        out.append((app_id, "%s/%s" % (directory, name)))
    return out


def load_recipes(chk, root, app_ids, policy, allow_insecure, load_yaml):
    """{app id: Recipe} for every apps/<id>/build.yaml that exists and is valid."""
    out = {}
    for app_id in app_ids:
        rel = "apps/%s/%s" % (app_id, RECIPE_FILE)
        if not os.path.lexists(os.path.join(root, rel)):
            continue
        data = load_yaml(chk, root, rel)
        found = recipe(chk, rel, data, app_id, policy, allow_insecure) if data is not None else None
        if found is not None:
            out[app_id] = found
    return out


def load_upstreams(chk, root, app_ids, recipes, policy, allow_insecure, load_yaml):
    """{app id: Upstream} for every upstreams/<id>.yaml."""
    out = {}
    for app_id, rel in _scan(chk, root, "upstreams", set(app_ids)):
        data = load_yaml(chk, root, rel)
        found = (upstream(chk, rel, data, app_id, app_id in recipes, policy, allow_insecure)
                 if data is not None else None)
        if found is not None:
            out[app_id] = found
    return out
