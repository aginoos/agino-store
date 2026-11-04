# SPDX-License-Identifier: GPL-3.0-or-later
"""Build task files (docs/build-tasks.md section 7.5): strict parsing of `queue/<id>/task.json`.

The queue is written by agino-store; the recipe in it was validated there, and is validated again here
because the build VM runs whatever this module lets through. Anything unexpected makes the task invalid.

task.json:
  {"id": "<16 hex>" (optional, must equal the directory), "app", "version": "<upstream>+agino<N>",
   "source": {"sha256", "size", "file": "source.tar" (optional)},
   "recipe": {"base", "source": {"kind", "checksum"} (optional), "build_depends", "steps", "package", "limits"},
   "limits": {"cpu_minutes", "memory_gib", "disk_gib"} (optional: the effective limits, else the recipe's)}
plus the informational keys upstream_version, tag, commit, attempt, by, created (ignored).
"""

import dataclasses
import json
import os
import re
import stat

from .config import LIMIT_RANGES, BuildLimits
from .util import ID_RE, SHA256_RE, SandboxError

TASK_ID_RE = re.compile(r"\A[0-9a-f]{16}\Z")
TASK_VERSION_RE = re.compile(r"\A[0-9][A-Za-z0-9.+~-]{0,63}\Z")
PKG_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9+.-]{1,99}\Z")
DEPEND_RE = re.compile(r"\A[a-z0-9][a-z0-9+.-]{1,99}(?: \((?:>=|<=|=|>>|<<) [0-9][A-Za-z0-9.+~:-]{0,63}\))?\Z")
SECTION_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,31}\Z")
MAINTAINER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9 ._'-]{0,79} <[A-Za-z0-9._+-]{1,64}@[A-Za-z0-9.-]{1,100}>\Z")
BASE_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,31}\Z")
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

MAX_TASK_BYTES = 1 << 20
MAX_STEPS = 40
MAX_STEP_CHARS = 512
MAX_DEPENDS = 200
TOP_KEYS = {"id", "app", "version", "source", "recipe", "limits", "upstream_version", "tag", "commit", "attempt",
            "by", "created"}
RECIPE_KEYS = {"base", "source", "build_depends", "steps", "package", "limits"}
PACKAGE_KEYS = {"name", "section", "depends", "maintainer"}


class TaskError(SandboxError):
    """A task.json that must not run."""


@dataclasses.dataclass(frozen=True)
class Package:
    name: str
    section: str
    depends: tuple
    maintainer: str


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
    package: Package
    limits: BuildLimits

    @property
    def deb_name(self):
        """The only deb file name we ever write: built from validated fields, never from guest data."""
        return "%s_%s_amd64.deb" % (self.package.name, self.version)


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


def parse_task(task_id, raw, ceiling):
    """The Task for queue directory `task_id` from the bytes of its task.json; TaskError when anything is off.

    `ceiling` is the configured BuildLimits: a recipe may lower them but never raise them."""
    if not TASK_ID_RE.match(task_id):
        raise TaskError("task directory name is not 16 hex digits")
    if len(raw) > MAX_TASK_BYTES:
        raise TaskError("task.json is too large")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise TaskError("task.json is not valid JSON") from None
    doc = _obj(doc, "task", TOP_KEYS, ("app", "version", "source", "recipe"))
    if "id" in doc and doc["id"] != task_id:
        raise TaskError("task id does not match the directory")
    source = _obj(doc["source"], "source", {"sha256", "size", "file"}, ("sha256", "size"))
    if source.get("file", "source.tar") != "source.tar":
        raise TaskError("source.file must be source.tar")
    recipe = _obj(doc["recipe"], "recipe", RECIPE_KEYS, ("build_depends", "steps", "package"))
    if "source" in recipe:
        _obj(recipe["source"], "recipe.source", {"kind", "checksum"})
    depends = tuple(_str(d, "build_depends entry", PKG_NAME_RE, 100)
                    for d in _list(recipe["build_depends"], "build_depends", MAX_DEPENDS))
    return Task(id=task_id, app=_str(doc["app"], "app", ID_RE, 64),
                version=_str(doc["version"], "version", TASK_VERSION_RE, 64),
                source_sha256=_str(source["sha256"], "source.sha256", SHA256_RE, 64),
                source_size=_int(source["size"], "source.size", 1, 1 << 40),
                base=_str(recipe.get("base", "debian-trixie"), "recipe.base", BASE_RE, 32),
                build_depends=depends, steps=_steps(recipe["steps"]), package=_package(recipe["package"]),
                limits=_limits(doc, recipe, ceiling))


def read_task_file(queue, task_id, ceiling):
    """Read and parse queue/<id>/task.json without following a symlink in the last component."""
    if not TASK_ID_RE.match(task_id):
        raise TaskError("task directory name is not 16 hex digits")
    path = os.path.join(queue, task_id, "task.json")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise TaskError("cannot read task.json: %s" % e.strerror) from None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise TaskError("task.json is not a regular file")
        raw = f.read(MAX_TASK_BYTES + 1)
    return parse_task(task_id, raw, ceiling)
