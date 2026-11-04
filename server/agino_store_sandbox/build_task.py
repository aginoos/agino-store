# SPDX-License-Identifier: GPL-3.0-or-later
"""Build task files (docs/build-tasks.md section 7.5): strict parsing of `queue/<id>/task.json`.

The queue is written by agino-store; the recipe in it was validated there, and is validated again here
because the build VM runs whatever this module lets through. Anything unexpected makes the task invalid.

task.json:
  {"id": "<16 hex>" (optional, must equal the directory), "app", "version": "<upstream>+agino<N>",
   "source": {"sha256", "size", "file": "source.tar" (optional)},
   "recipe": {"base", "source": {"kind", "checksum"} (optional), "build_depends", "steps", "package", "limits"},
   "limits": {"cpu_minutes", "memory_gib", "disk_gib"} (optional: the effective limits, else the recipe's)}
plus the informational keys upstream_version, tag, commit, attempt, by, created. The first three become the
environment variables AGINO_VERSION, AGINO_TAG and AGINO_COMMIT of steps and tests (a malformed value is dropped).

Recipe v2 (docs/sandbox.md section 10.2): recipe may also carry sources, files, test, evidence, recipe_version and
derived_from, and `source` and `sources` exclude each other. `"mode": "ai"` (default "build") plus an "ai" object
{"agent", "kind", "minutes"} makes an AI task: it needs queue/<id>/workspace.tar, and app, version, source, recipe
are optional and not used.
"""

import dataclasses
import json
import os
import re
import stat

from .config import LIMIT_RANGES, AiLimits, BuildLimits
from .util import ID_RE, SHA256_RE, SandboxError

TASK_ID_RE = re.compile(r"\A[0-9a-f]{16}\Z")
TASK_VERSION_RE = re.compile(r"\A[0-9][A-Za-z0-9.+~-]{0,63}\Z")
PKG_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9+.-]{1,99}\Z")
DEPEND_RE = re.compile(r"\A[a-z0-9][a-z0-9+.-]{1,99}(?: \((?:>=|<=|=|>>|<<) [0-9][A-Za-z0-9.+~:-]{0,63}\))?\Z")
SECTION_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,31}\Z")
MAINTAINER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9 ._'-]{0,79} <[A-Za-z0-9._+-]{1,64}@[A-Za-z0-9.-]{1,100}>\Z")
BASE_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,31}\Z")
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

UPSTREAM_VERSION_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+~:-]{0,99}\Z")
TAG_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+~/@-]{0,199}\Z")
COMMIT_RE = re.compile(r"\A[0-9a-f]{7,64}\Z")
AGINO_SUFFIX_RE = re.compile(r"\+agino[0-9]+\Z")
SOURCE_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9_.-]{0,31}\Z")
SOURCE_URL_RE = re.compile(r"\Ahttps://[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?(?:/[^\s\x00-\x1f\x7f\"'<>\\`]*)?\Z")
SOURCE_REF_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/+~@^${}-]{0,199}\Z")
FILE_COMPONENT_RE = re.compile(r"\A(?!\.\.?\Z)[A-Za-z0-9._@+][A-Za-z0-9._+@-]{0,99}\Z")
LINE_CONTROL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")  # a single-line text: only the tab is allowed
ALL_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
AI_AGENTS = ("claude", "codex")
AI_KINDS = ("import", "repair")
FILE_MODES = ("0644", "0755")
RESERVED_NAMES = ("pkgroot",)

MAX_TASK_BYTES = 4 << 20  # v2 recipes carry up to 20 files of 64 KiB (JSON-escaped), so more than the v1 limit
MAX_STEPS = 40
MAX_STEP_CHARS = 512
MAX_DEPENDS = 200
MAX_SOURCES = 8
MAX_FILES = 20
MAX_FILE_BYTES = 64 * 1024
MAX_FILE_PATH = 200
MAX_FILE_DEPTH = 8
MAX_TESTS = 10
MAX_TEST_CHARS = 512
MAX_EVIDENCE = 20
MAX_URL_CHARS = 2048
TOP_KEYS = {"id", "app", "version", "source", "recipe", "limits", "upstream_version", "tag", "commit", "attempt",
            "by", "created", "mode", "ai"}
RECIPE_KEYS = {"base", "source", "build_depends", "steps", "package", "limits", "sources", "files", "test",
               "evidence", "recipe_version", "derived_from"}
PACKAGE_KEYS = {"name", "section", "depends", "maintainer"}
SOURCE_KEYS = {"name", "kind", "url", "ref", "sha256"}
FILE_KEYS = {"path", "mode", "content"}
EVIDENCE_KEYS = {"claim", "url"}


class TaskError(SandboxError):
    """A task.json that must not run."""



@dataclasses.dataclass(frozen=True)
class Package:
    name: str
    section: str
    depends: tuple
    maintainer: str


@dataclasses.dataclass(frozen=True)
class AiSpec:
    agent: str
    kind: str
    minutes: int


@dataclasses.dataclass(frozen=True)
class SourceSpec:
    """One entry of recipe v2 `sources`. The server already fetched it into source.tar/<name>/; the runner only
    validates the entry (it is part of the reviewed recipe) and never fetches anything."""
    name: str
    kind: str
    url: str
    ref: str
    sha256: str


@dataclasses.dataclass(frozen=True)
class RecipeFile:
    path: str
    mode: str
    content: str


@dataclasses.dataclass(frozen=True)
class Task:
    id: str
    app: str
    version: str
    source_sha256: str
    source_size: int
    base: str
    build_depends: tuple
    steps: tuple
    package: object  # Package; None for AI tasks
    limits: BuildLimits
    mode: str = "build"
    ai: object = None  # AiSpec for AI tasks
    upstream_version: str = ""
    tag: str = ""
    commit: str = ""
    recipe_version: object = None
    derived_from: object = None
    sources: tuple = ()
    files: tuple = ()
    tests: tuple = ()
    evidence: tuple = ()

    @property
    def deb_name(self):
        """The only deb file name we ever write: built from validated fields, never from guest data."""
        return "%s_%s_amd64.deb" % (self.package.name, self.version)

    @property
    def agino_env(self):
        """AGINO_VERSION / AGINO_TAG / AGINO_COMMIT as `NAME=value` items (empty when the task has no value)."""
        return ["AGINO_VERSION=" + self.upstream_version, "AGINO_TAG=" + self.tag, "AGINO_COMMIT=" + self.commit]


def _obj(value, what, keys, required=()):
    if not isinstance(value, dict):
        raise TaskError("%s must be an object" % what)
    extra = set(value) - keys
    if extra:
        raise TaskError("%s has unknown keys: %s" % (what, ", ".join(sorted(str(k)[:30] for k in extra))))
    missing = [k for k in required if k not in value]
    if missing:
        raise TaskError("%s lacks: %s" % (what, ", ".join(missing)))
    return value


def _str(value, what, regex, limit=200):
    if not isinstance(value, str) or len(value) > limit or not regex.match(value):
        raise TaskError("%s is not valid" % what)
    return value


def _int(value, what, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise TaskError("%s must be an integer between %d and %d" % (what, low, high))
    return value


def _list(value, what, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise TaskError("%s must be a list of at most %d entries" % (what, maximum))
    return value


def _limits(task, recipe, ceiling):
    raw = task.get("limits", recipe.get("limits", {}))
    raw = _obj(raw, "limits", set(LIMIT_RANGES))
    out = {}
    for key, (low, _high) in LIMIT_RANGES.items():
        cap = getattr(ceiling, key)
        out[key] = cap if key not in raw else _int(raw[key], "limits.%s" % key, low, cap)
    return BuildLimits(**out)


def _package(raw):
    pkg = _obj(raw, "package", PACKAGE_KEYS, ("name", "section", "maintainer"))
    depends = tuple(_str(d, "package.depends entry", DEPEND_RE, 200)
                    for d in _list(pkg.get("depends", []), "package.depends", MAX_DEPENDS))
    return Package(_str(pkg["name"], "package.name", PKG_NAME_RE), _str(pkg["section"], "package.section", SECTION_RE),
                   depends, _str(pkg["maintainer"], "package.maintainer", MAINTAINER_RE, 200))


def _steps(raw):
    steps = _list(raw, "steps", MAX_STEPS)
    if not steps:
        raise TaskError("steps must not be empty")
    for step in steps:
        if not isinstance(step, str) or not 1 <= len(step) <= MAX_STEP_CHARS or CONTROL_CHARS_RE.search(step):
            raise TaskError("a step is empty, too long or has control characters")
    return tuple(steps)




def _ai_limits(task, ai_minutes, ceiling):
    """VM limits of an AI task: memory/disk may be lowered by `limits`, the run time is ai.minutes."""
    raw = _obj(task.get("limits", {}), "limits", set(LIMIT_RANGES))
    mem, disk = ceiling.memory_gib, ceiling.disk_gib
    if "memory_gib" in raw:
        mem = _int(raw["memory_gib"], "limits.memory_gib", 1, ceiling.memory_gib)
    if "disk_gib" in raw:
        disk = _int(raw["disk_gib"], "limits.disk_gib", 1, ceiling.disk_gib)
    if "cpu_minutes" in raw:
        _int(raw["cpu_minutes"], "limits.cpu_minutes", 1, 24 * 60)  # shape only: ai.minutes bounds an AI task
    return BuildLimits(cpu_minutes=ai_minutes, memory_gib=mem, disk_gib=disk)


def _url(value, what):
    return _str(value, what, SOURCE_URL_RE, MAX_URL_CHARS)


def _sources(raw):
    items = _list(raw, "recipe.sources", MAX_SOURCES)
    if not items:
        raise TaskError("recipe.sources must not be empty")
    out, names = [], set()
    for i, item in enumerate(items, 1):
        what = "recipe.sources[%d]" % i
        item = _obj(item, what, SOURCE_KEYS, ("name", "kind", "url"))
        name = _str(item["name"], what + ".name", SOURCE_NAME_RE, 32)
        if name in names or name in RESERVED_NAMES:
            raise TaskError("%s.name is reserved or used twice" % what)
        names.add(name)
        kind = item["kind"]
        if kind not in ("git", "tarball", "file"):
            raise TaskError("%s.kind must be git, tarball or file" % what)
        ref = sha256 = ""
        if kind == "git":
            if "sha256" in item:
                raise TaskError("%s: a git source has no sha256" % what)
            if "ref" in item:
                ref = _str(item["ref"], what + ".ref", SOURCE_REF_RE, 200)
        else:
            if "ref" in item or "sha256" not in item:
                raise TaskError("%s: a %s source needs sha256 and has no ref" % (what, kind))
            sha256 = _str(item["sha256"], what + ".sha256", SHA256_RE, 64)
        out.append(SourceSpec(name, kind, _url(item["url"], what + ".url"), ref, sha256))
    return tuple(out)


def _file_path(value, what):
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_FILE_PATH:
        raise TaskError("%s is not valid" % what)
    parts = value.split("/")
    if len(parts) > MAX_FILE_DEPTH or not all(FILE_COMPONENT_RE.match(p) for p in parts):
        raise TaskError("%s must be a relative path without .., empty parts or odd characters" % what)
    return value


def _files(raw, source_names):
    items = _list(raw, "recipe.files", MAX_FILES)
    out, seen = [], set()
    for i, item in enumerate(items, 1):
        what = "recipe.files[%d]" % i
        item = _obj(item, what, FILE_KEYS, ("path", "mode", "content"))
        path = _file_path(item["path"], what + ".path")
        top = path.split("/")[0]
        if top in source_names or top in RESERVED_NAMES:
            raise TaskError("%s.path would overwrite a source directory" % what)
        if item["mode"] not in FILE_MODES:
            raise TaskError("%s.mode must be 0644 or 0755" % what)
        content = item["content"]
        if not isinstance(content, str) or CONTROL_CHARS_RE.search(content):
            raise TaskError("%s.content must be text without control characters" % what)
        if len(content.encode("utf-8", "replace")) > MAX_FILE_BYTES:
            raise TaskError("%s.content is larger than %d KiB" % (what, MAX_FILE_BYTES >> 10))
        out.append(RecipeFile(path, item["mode"], content))
        seen.add(path)
    paths = [f.path for f in out]
    if len(set(paths)) != len(paths):
        raise TaskError("recipe.files lists a path twice")
    for path in paths:  # no file may also be the directory of another
        parts = path.split("/")
        if any("/".join(parts[:n]) in seen for n in range(1, len(parts))):
            raise TaskError("recipe.files has a path below another file")
    return tuple(out)


def _tests(raw):
    items = _list(raw, "recipe.test", MAX_TESTS)
    for item in items:
        if not isinstance(item, str) or not 1 <= len(item) <= MAX_TEST_CHARS or LINE_CONTROL_RE.search(item):
            raise TaskError("a test command is empty, too long, has several lines or control characters")
    return tuple(items)


def _evidence(raw):
    items = _list(raw, "recipe.evidence", MAX_EVIDENCE)
    out = []
    for i, item in enumerate(items, 1):
        what = "recipe.evidence[%d]" % i
        item = _obj(item, what, EVIDENCE_KEYS, ("claim", "url"))
        claim = item["claim"]
        if not isinstance(claim, str) or not 1 <= len(claim) <= 200 or ALL_CONTROL_RE.search(claim):
            raise TaskError("%s.claim is not valid" % what)
        out.append((claim, _url(item["url"], what + ".url")))
    return tuple(out)


def _info(doc, key, regex):
    """An informational task.json value; anything malformed counts as absent (it only feeds environment variables)."""
    value = doc.get(key)
    return value if isinstance(value, str) and regex.match(value) else ""


def _info_version(doc, version):
    found = _info(doc, "upstream_version", UPSTREAM_VERSION_RE)
    return found or AGINO_SUFFIX_RE.sub("", version)


def _parse_ai_spec(raw, ceiling):
    ai = _obj(raw, "ai", {"agent", "kind", "minutes"}, ("agent", "kind"))
    if ai["agent"] not in AI_AGENTS or ai["kind"] not in AI_KINDS:
        raise TaskError("ai.agent must be claude or codex and ai.kind import or repair")
    minutes = ceiling.minutes if "minutes" not in ai else _int(ai["minutes"], "ai.minutes", 1, ceiling.minutes)
    return AiSpec(ai["agent"], ai["kind"], minutes)


def _parse_ai_task(task_id, doc, ceiling):
    if "ai" not in doc:
        raise TaskError("an AI task needs an ai object")
    ai = _parse_ai_spec(doc["ai"], ceiling)
    app = doc.get("app", "")
    if app != "":
        _str(app, "app", ID_RE, 64)
    version = doc.get("version", "")
    if version != "":
        _str(version, "version", TASK_VERSION_RE, 64)
    if "source" in doc:
        _obj(doc["source"], "source", {"sha256", "size", "file"})
    if "recipe" in doc:
        _obj(doc["recipe"], "recipe", RECIPE_KEYS)
    return Task(id=task_id, app=app, version=version, source_sha256="", source_size=0, base="debian-trixie",
                build_depends=(), steps=(), package=None, limits=_ai_limits(doc, ai.minutes, ceiling), mode="ai",
                ai=ai)


def _parse_build_task(task_id, doc, ceiling):
    for key in ("app", "version", "source", "recipe"):
        if key not in doc:
            raise TaskError("task lacks: %s" % key)
    if "ai" in doc:
        raise TaskError("only an AI task has an ai object")
    source = _obj(doc["source"], "source", {"sha256", "size", "file"}, ("sha256", "size"))
    if source.get("file", "source.tar") != "source.tar":
        raise TaskError("source.file must be source.tar")
    recipe = _obj(doc["recipe"], "recipe", RECIPE_KEYS, ("build_depends", "steps", "package"))
    if "source" in recipe and "sources" in recipe:
        raise TaskError("recipe has source and sources: use one")
    if "source" in recipe:
        _obj(recipe["source"], "recipe.source", {"kind", "checksum"})
    sources = _sources(recipe["sources"]) if "sources" in recipe else ()
    depends = tuple(_str(d, "build_depends entry", PKG_NAME_RE, 100)
                    for d in _list(recipe["build_depends"], "build_depends", MAX_DEPENDS))
    version = _str(doc["version"], "version", TASK_VERSION_RE, 64)
    versions = {k: None if k not in recipe else _int(recipe[k], "recipe." + k, 1, 10 ** 6)
                for k in ("recipe_version", "derived_from")}
    return Task(id=task_id, app=_str(doc["app"], "app", ID_RE, 64), version=version,
                source_sha256=_str(source["sha256"], "source.sha256", SHA256_RE, 64),
                source_size=_int(source["size"], "source.size", 1, 1 << 40),
                base=_str(recipe.get("base", "debian-trixie"), "recipe.base", BASE_RE, 32),
                build_depends=depends, steps=_steps(recipe["steps"]), package=_package(recipe["package"]),
                limits=_limits(doc, recipe, ceiling), upstream_version=_info_version(doc, version),
                tag=_info(doc, "tag", TAG_RE), commit=_info(doc, "commit", COMMIT_RE),
                sources=sources, files=_files(recipe.get("files", []), {s.name for s in sources}),
                tests=_tests(recipe.get("test", [])), evidence=_evidence(recipe.get("evidence", [])), **versions)


def parse_task(task_id, raw, ceiling, ai_ceiling=None):
    """The Task for queue directory `task_id` from the bytes of its task.json; TaskError when anything is off.

    `ceiling` is the configured BuildLimits and `ai_ceiling` the AiLimits: a task may lower them but never raise
    them."""
    if not TASK_ID_RE.match(task_id):
        raise TaskError("task directory name is not 16 hex digits")
    if len(raw) > MAX_TASK_BYTES:
        raise TaskError("task.json is too large")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise TaskError("task.json is not valid JSON") from None
    doc = _obj(doc, "task", TOP_KEYS)
    if "id" in doc and doc["id"] != task_id:
        raise TaskError("task id does not match the directory")
    mode = doc.get("mode", "build")
    if mode == "ai":
        return _parse_ai_task(task_id, doc, ai_ceiling or AiLimits())
    if mode != "build":
        raise TaskError("mode must be build or ai")
    return _parse_build_task(task_id, doc, ceiling)


def read_task_file(queue, task_id, ceiling, ai_ceiling=None):
    """Read and parse queue/<id>/task.json without following a symlink in the last component."""
    if not TASK_ID_RE.match(task_id):
        raise TaskError("task directory name is not 16 hex digits")
    path = os.path.join(queue, task_id, "task.json")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise TaskError("cannot read task.json: %s" % e.strerror) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):  # before fdopen: it refuses directories with an exception
            raise TaskError("task.json is not a regular file")
        with os.fdopen(fd, "rb") as f:
            fd = -1
            raw = f.read(MAX_TASK_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    return parse_task(task_id, raw, ceiling, ai_ceiling)
