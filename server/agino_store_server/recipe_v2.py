# SPDX-License-Identifier: GPL-3.0-or-later
"""Recipe v2 additions to apps/<id>/build.yaml (docs/server.md section 14.7, contract ai-intake 10.1).

New optional keys on top of the v1 recipe: recipe_version, derived_from, sources, files, test, evidence.
Everything here is validated as if it were hostile (an AI wrote it, or a person after reading the page of an
unknown vendor): strict keys, strict character sets, https only, hosts on the allow list, sha256 for every
download. The placeholders ${VERSION}, ${TAG} and ${COMMIT} are text-substituted by the server, and only
inside sources[].url and sources[].ref; everything else reads the environment variables AGINO_VERSION,
AGINO_TAG and AGINO_COMMIT at build time.
"""

import posixpath
import re
from urllib.parse import urlsplit

SOURCE_KINDS = ("git", "tarball", "file")
SOURCE_NAME_RE = re.compile(r"\A[a-z][a-z0-9_-]{0,31}\Z")
SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
COMMIT_RE = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
REF_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+/-]{0,199}\Z")
PLACEHOLDER_RE = re.compile(r"\$\{(VERSION|TAG|COMMIT)\}")
DUMMY_VALUES = {"VERSION": "0", "TAG": "v0", "COMMIT": "0" * 40}
FILE_SEGMENT_RE = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9._+@-]{0,99}\Z")
DOWNLOAD_SEGMENT_RE = re.compile(r"\A[A-Za-z0-9._~+-]{1,150}\Z")
MODES = {"0644": "0644", "0755": "0755", 0o644: "0644", 0o755: "0755"}   # unquoted 0755 is an int in YAML
V2_KEYS = ("recipe_version", "derived_from", "sources", "files", "test", "evidence")
MAX_SOURCES = 8
MAX_FILES = 20
MAX_FILE_BYTES = 64 << 10
RESERVED_NAMES = ("pkgroot",)   # the runner builds the package from ./pkgroot
MAX_TESTS = 10
MAX_TEST_LEN = 512
MAX_EVIDENCE = 20
MAX_CLAIM_LEN = 200
MAX_URL_LEN = 400
MAX_VERSION_NUMBER = 1_000_000
FILE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
DEL_RE = re.compile(r"\x7f")
CLAIM_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# What the build runner accepts as a URL in a task (build_task.SOURCE_URL_RE); evidence links must fit it too.
RUNNER_URL_RE = re.compile(r"\Ahttps://[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?(?:/[^\s\x00-\x1f\x7f\"'<>\\`]*)?\Z")


class ExpandError(ValueError):
    """The recipe cannot be turned into a task for these values (the message names the field)."""


def _integer(chk, path, value, low=1):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= MAX_VERSION_NUMBER:
        chk.fail(path, "must be an integer between %d and %d" % (low, MAX_VERSION_NUMBER))
        return None
    return value


def _single_line(chk, path, value, max_len):
    text = chk.string(path, value, max_len=max_len)
    if text is not None and ("\n" in text or DEL_RE.search(text)):
        chk.fail(path, "must be a single line without control characters")
        return None
    return text


def has_placeholder(text):
    return isinstance(text, str) and PLACEHOLDER_RE.search(text) is not None


def substitute(text, values):
    """Replace ${VERSION}, ${TAG}, ${COMMIT}; ExpandError when a needed value is missing."""
    def one(match):
        value = values.get(match.group(1))
        if not value:
            raise ExpandError("the recipe uses ${%s} but this task has no such value" % match.group(1))
        return value
    return PLACEHOLDER_RE.sub(one, text)


# sources

def _authority_clean(url):
    """The part between // and the first / must not hold a placeholder (it could change the host)."""
    rest = url.split("//", 1)[1] if "//" in url else ""
    return "$" not in rest.split("/", 1)[0]


def _download_url(chk, path, url, policy, allow_insecure):
    """An https URL on an allowed host for a tarball or file source (placeholders already replaced by dummies)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        chk.fail(path, "not a valid URL")
        return False
    host = parts.hostname or ""
    schemes = ("https", "http") if allow_insecure else ("https",)
    ok = (parts.scheme in schemes and host and not parts.username and not parts.password
          and not parts.query and not parts.fragment and (port is None or port == 443 or allow_insecure)
          and parts.netloc.lower() in (host, "%s:%s" % (host, port)))
    if not ok:
        chk.fail(path, "must be an %s URL without credentials, query or fragment" % "/".join(schemes))
        return False
    if host not in policy.allowed_hosts:
        chk.fail(path, "host is not in [upstream] allowed_hosts")
        return False
    segments = parts.path.split("/")[1:]
    if not 1 <= len(segments) <= 12 or not all(DOWNLOAD_SEGMENT_RE.match(s) and s not in (".", "..") for s in segments):
        chk.fail(path, "path must be 1..12 segments of [A-Za-z0-9._~+-]")
        return False
    return True


def _source(chk, path, item, policy, allow_insecure, public_url):
    if not chk.mapping(path, item, ("name", "kind", "url"), ("ref", "sha256")):
        return None
    before = len(chk.errors)
    name = chk.string(path + ".name", item["name"], SOURCE_NAME_RE, 32)
    if name in RESERVED_NAMES:
        chk.fail(path + ".name", "%r is reserved" % name)
        name = None
    kind = item["kind"]
    if kind not in SOURCE_KINDS:
        chk.fail(path + ".kind", "must be one of %s" % ", ".join(SOURCE_KINDS))
        return None
    url = _single_line(chk, path + ".url", item["url"], MAX_URL_LEN)
    if url is not None and not _authority_clean(url):
        chk.fail(path + ".url", "placeholders are not allowed in the host part")
        url = None
    dummy = substitute(url, DUMMY_VALUES) if url is not None else None
    if dummy is not None:
        if kind == "git":
            if public_url(chk, path + ".url", dummy, policy, allow_insecure) is None:
                url = None
        elif not _download_url(chk, path + ".url", dummy, policy, allow_insecure):
            url = None
    out = {"name": name, "kind": kind, "url": url}
    if kind == "git":
        out["ref"] = _ref(chk, path, item)
        if "sha256" in item:
            chk.fail(path + ".sha256", "not allowed for kind git (the commit is pinned by the task)")
    else:
        out["sha256"] = chk.string(path + ".sha256", item.get("sha256"), SHA256_RE, 64) if "sha256" in item else None
        if "sha256" not in item:
            chk.fail(path + ".sha256", "kind %s needs the sha256 of the download" % kind)
        if "ref" in item:
            chk.fail(path + ".ref", "only allowed for kind git")
    if len(chk.errors) != before or None in (name, url) or (kind == "git" and not out["ref"]):
        return None
    return {k: v for k, v in out.items() if v is not None}


def _ref(chk, path, item):
    if "ref" not in item:
        chk.fail(path + ".ref", "kind git needs a ref (a tag or a full commit id)")
        return None
    text = _single_line(chk, path + ".ref", item["ref"], 200)
    if text is None:
        return None
    dummy = substitute(text, DUMMY_VALUES)
    if not (REF_RE.match(dummy) and ".." not in dummy and "//" not in dummy and not dummy.endswith(("/", ".lock"))):
        chk.fail(path + ".ref", "must be a tag name or a full commit id (after placeholders: %s)" % REF_RE.pattern)
        return None
    return text


def _sources(chk, path, value, policy, allow_insecure, public_url):
    items = chk.sequence(path, value, 1, MAX_SOURCES)
    parsed = [_source(chk, "%s[%d]" % (path, i), s, policy, allow_insecure, public_url)
              for i, s in enumerate(items or [])]
    if items is None or None in parsed:
        return None
    names = [s["name"] for s in parsed]
    if len(set(names)) != len(names):
        chk.fail(path, "duplicate source name")
        return None
    return tuple(parsed)


# files

def _file_path(chk, path, value):
    text = _single_line(chk, path, value, 200)
    if text is None:
        return None
    parts = text.split("/")
    if (text.startswith("/") or len(parts) > 8 or not all(FILE_SEGMENT_RE.match(p) for p in parts)
            or posixpath.normpath(text) != text):
        chk.fail(path, "must be a relative path of at most 8 segments of [A-Za-z0-9._+@-] (no .., no leading dot)")
        return None
    return text


def _file(chk, path, item):
    if not chk.mapping(path, item, ("path", "content"), ("mode",)):
        return None
    rel = _file_path(chk, path + ".path", item["path"])
    mode = MODES.get(item.get("mode", "0644")) if not isinstance(item.get("mode", "0644"), bool) else None
    if mode is None:
        chk.fail(path + ".mode", 'must be "0644" or "0755"')
    content = item["content"]
    if not isinstance(content, str) or not content or FILE_CONTROL_RE.search(content):
        chk.fail(path + ".content", "must be non-empty text without control characters")
        content = None
    elif len(content.encode("utf-8")) > MAX_FILE_BYTES:
        chk.fail(path + ".content", "must be at most %d KiB" % (MAX_FILE_BYTES >> 10))
        content = None
    if None in (rel, mode, content):
        return None
    return {"path": rel, "mode": mode, "content": content}


def _files(chk, path, value, source_names):
    items = chk.sequence(path, value, 0, MAX_FILES)
    parsed = [_file(chk, "%s[%d]" % (path, i), f) for i, f in enumerate(items or [])]
    if items is None or None in parsed:
        return None
    paths = [f["path"] for f in parsed]
    ok = True
    if len(set(paths)) != len(paths):
        chk.fail(path, "duplicate file path")
        ok = False
    for i, rel in enumerate(paths):
        if rel.split("/")[0] in source_names or rel.split("/")[0] in RESERVED_NAMES:
            chk.fail("%s[%d].path" % (path, i), "must not lie inside a source directory")
            ok = False
        if any(other.startswith(rel + "/") for other in paths):
            chk.fail("%s[%d].path" % (path, i), "is also used as a directory by another file")
            ok = False
    return tuple(parsed) if ok else None


# test and evidence

def _test(chk, path, value):
    items = chk.sequence(path, value, 0, MAX_TESTS)
    lines = [_single_line(chk, "%s[%d]" % (path, i), s, MAX_TEST_LEN) for i, s in enumerate(items or [])]
    return tuple(lines) if items is not None and None not in lines else None


def _evidence(chk, path, value, url_check):
    items = chk.sequence(path, value, 0, MAX_EVIDENCE)
    out = []
    for i, item in enumerate(items or []):
        where = "%s[%d]" % (path, i)
        if not chk.mapping(where, item, ("claim", "url")):
            out.append(None)
            continue
        claim = chk.display(where + ".claim", item["claim"], MAX_CLAIM_LEN)
        if claim is not None and CLAIM_CONTROL_RE.search(claim):
            chk.fail(where + ".claim", "must be a single line without control characters")
            claim = None
        url = url_check(where + ".url", item["url"])
        out.append({"claim": claim, "url": url} if claim and url else None)
    return tuple(out) if items is not None and None not in out else None


def _evidence_url(chk, path, value, allow_insecure):
    url = chk.url(path, value, allow_insecure)
    if url is not None and not allow_insecure and not RUNNER_URL_RE.match(url):
        chk.fail(path, "must be a plain https URL (letters, digits, dots and dashes in the host; no quotes, "
                 "spaces or angle brackets)")
        return None
    return url


def _placeholders_outside_sources(chk, rel, data):
    """${VERSION} and friends only belong in sources[].url and sources[].ref."""
    def walk(value, path):
        if isinstance(value, str):
            if has_placeholder(value):
                chk.fail(path, "placeholders ${VERSION}, ${TAG}, ${COMMIT} are only allowed in sources[].url and "
                         "sources[].ref (read $AGINO_VERSION, $AGINO_TAG, $AGINO_COMMIT instead)")
        elif isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, "%s[%d]" % (path, i))
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(item, "%s.%s" % (path, key))
    for key, value in data.items():
        if key != "sources":
            walk(value, "%s.%s" % (rel, key))


def parse(chk, rel, data, policy, allow_insecure, public_url):
    """Validate the v2 keys present in `data`; returns the dict of parsed values (keys absent in `data` are
    absent here), or None when something is wrong (the errors are in `chk`)."""
    before = len(chk.errors)
    out = {}
    _placeholders_outside_sources(chk, rel, data)
    if "recipe_version" in data:
        out["recipe_version"] = _integer(chk, rel + ".recipe_version", data["recipe_version"])
    if "derived_from" in data:
        out["derived_from"] = _integer(chk, rel + ".derived_from", data["derived_from"])
    version, derived = out.get("recipe_version"), out.get("derived_from")
    if derived is not None and version is not None and derived >= version:
        chk.fail(rel + ".derived_from", "must be lower than recipe_version")
    if derived is not None and "recipe_version" not in data:
        chk.fail(rel + ".derived_from", "needs recipe_version")
    if "sources" in data:
        out["sources"] = _sources(chk, rel + ".sources", data["sources"], policy, allow_insecure, public_url)
    names = {s["name"] for s in out.get("sources") or ()}
    if "files" in data:
        if "sources" not in data:
            chk.fail(rel + ".files", "only allowed together with sources")
        else:
            out["files"] = _files(chk, rel + ".files", data["files"], names)
    if "test" in data:
        out["test"] = _test(chk, rel + ".test", data["test"])
    if "evidence" in data:
        out["evidence"] = _evidence(chk, rel + ".evidence", data["evidence"],
                                    lambda p, v: _evidence_url(chk, p, v, allow_insecure))
    if len(chk.errors) != before or any(v is None for v in out.values()):
        return None
    return out


# Task creation: expansion of the placeholders

_URL_AFTER = re.compile(r"\A[A-Za-z0-9._~+/-]*\Z")


def expand_source(source, values):
    """A copy of one parsed source with the placeholders replaced; ExpandError for a value that breaks the shape."""
    out = dict(source)
    for key in ("url", "ref"):
        if key in out:
            out[key] = substitute(out[key], values)
    path = urlsplit(out["url"]).path
    if not _URL_AFTER.match(path) or ".." in path.split("/") or "//" in path or urlsplit(out["url"]).query:
        raise ExpandError("source %s: the URL is not valid after substituting the placeholders" % out["name"])
    if "ref" in out and not (REF_RE.match(out["ref"]) and ".." not in out["ref"] and "//" not in out["ref"]):
        raise ExpandError("source %s: the ref is not valid after substituting the placeholders" % out["name"])
    return out


def expand_sources(sources, values):
    return tuple(expand_source(s, values) for s in sources)
