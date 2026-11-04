# SPDX-License-Identifier: GPL-3.0-or-later
"""Build tasks: build-request and build-cancel (docs/server.md section 14, contract 7.1 and 7.3).

A task record lives in state/builds/<id>.json. The recipe is copied into the record when the task is
created, so a later change of apps/<id>/build.yaml never changes a task that is already queued.
Callers hold the state lock.
"""

import os
import re
import tempfile

from . import debinfo, recipe_v2, source_assembly
from .content_builds import UPSTREAM_VERSION_RE
from .state import StateError

TERMINAL = ("done", "failed", "cancelled")
BY_RE = re.compile(r"\A[A-Za-z0-9._-]{1,32}\Z")
CANCEL_FILE = "cancel"
TASK_FILE = "task.json"
SOURCE_FILE = "source.tar"
WORKSPACE_FILE = "workspace.tar"
MAX_ERROR = 500


class BuildError(Exception):
    """The request cannot be carried out (exit code 1)."""


class BuildUsage(ValueError):
    """A bad argument (exit code 2)."""


class Report:
    """What one build-step run did: lines to print and how many tasks it failed."""

    def __init__(self):
        self.lines = []
        self.failed = 0

    def add(self, line):
        self.lines.append(debinfo.clean_text(line))

    def fail(self, line):
        self.failed += 1
        self.add(line)


def is_terminal(record):
    return record.get("status") in TERMINAL


def clean_error(value):
    return debinfo.clean_text(value).strip()[:MAX_ERROR]


def queue_dir(cfg, build_id):
    """builds.queue/<id> for a validated task id (the state module checks the id shape when it loads the record)."""
    return os.path.join(cfg.builds_queue, build_id)


def out_dir(cfg, build_id):
    return os.path.join(cfg.builds_out, build_id)


def check_by(by):
    if not isinstance(by, str) or not BY_RE.match(by):
        raise BuildUsage("bad --by: %r" % (by,))
    return by


def check_version(version):
    if not isinstance(version, str) or not UPSTREAM_VERSION_RE.match(version):
        raise BuildUsage("bad version: %r" % (version,))
    return version


def new_record(st, up, recipe, version, entry, attempt, by, now):
    return build_record(st, up.app, up.repo, recipe, version, entry["tag"], entry["commit"], attempt, by, now)


def build_record(st, app, repo, recipe, version, tag, commit, attempt, by, now, **extra):
    """A queued task record. The recipe is stored as the plain task data (placeholders of a v2 recipe replaced);
    BuildError when a placeholder cannot be replaced. `extra` adds fields (verification tasks of intake)."""
    try:
        task_recipe = recipe.as_task(version, tag, commit)
    except recipe_v2.ExpandError as e:
        raise BuildError(str(e)) from e
    record = {
        "id": st.new_build_id(), "app": app, "version": "%s+agino%d" % (version, attempt),
        "upstream_version": version, "tag": tag, "commit": commit, "attempt": attempt,
        "status": "queued", "by": by, "created": now, "started": None, "finished": None, "error": "",
        "artifact": None, "candidate": None,
        # not part of the contract's task record: what build-step needs later
        "mode": "build", "repo": repo, "package": recipe.package["name"], "limits": recipe.limits.as_dict(),
        "recipe": task_recipe, "source": None,
    }
    if recipe.is_v2:
        record["pins"] = source_assembly.pin_map(task_recipe["sources"], repo, tag, commit)
        if not repo:   # a recipe without a tracked upstream: the main source stands in (shown as the origin)
            git = next((s for s in task_recipe["sources"] if s["kind"] == "git"), task_recipe["sources"][0])
            record["repo"] = git["url"]
    record.update(extra)
    return record


def request(cfg, st, content, app, version, by, now=None):
    """Create a queued task for upstream `version` of `app`; BuildError when it must not be built."""
    check_by(by)
    check_version(version)
    up, recipe = content.upstream(app), content.recipe(app)
    if up is None or recipe is None:
        raise BuildError("%s has no upstream tracking or no build recipe" % app)
    memo = st.upstream_memo(app)
    entry = (memo.get("seen") or {}).get(version)
    if not isinstance(entry, dict) or not entry.get("tag") or not entry.get("commit"):
        raise BuildError("version %s of %s has not been seen by upstream-check" % (version, app))
    if any(m.get("version") == version for m in memo.get("tag_moved") or []):
        raise BuildError("the tag of %s %s moved to another commit; not building it" % (app, version))
    existing = [b for b in st.builds(app=app) if b.get("upstream_version") == version and b.get("mode") != "ai"]
    unfinished = [b for b in existing if not is_terminal(b)]
    if unfinished:
        raise BuildError("%s %s already has an unfinished build (%s)" % (app, version, unfinished[0].get("id")))
    record = new_record(st, up, recipe, version, entry, len(existing) + 1, by, now or debinfo.utc_now())
    st.save_build(record)
    return record


def write_cancel_file(cfg, record, by, now):
    """builds.queue/<id>/cancel, if the task has a queue directory (the runner looks for it)."""
    directory = queue_dir(cfg, record["id"])
    if os.path.islink(directory) or not os.path.isdir(directory):
        return False
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("%s %s\n" % (now, by))
        os.chmod(tmp, 0o640)
        os.replace(tmp, os.path.join(directory, CANCEL_FILE))
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise
    return True


def cancel(cfg, st, build_id, by, now=None):
    """Mark an unfinished task cancelled and ask the runner to stop it."""
    check_by(by)
    try:
        record = st.build(build_id)
    except StateError as e:
        raise BuildUsage(str(e)) from e
    if record is None:
        raise BuildUsage("unknown build %s" % build_id)
    if is_terminal(record):
        raise BuildError("build %s is already %s" % (build_id, record.get("status")))
    now = now or debinfo.utc_now()
    write_cancel_file(cfg, record, by, now)
    updated = dict(record, status="cancelled", finished=now, error="cancelled by %s" % by)
    st.save_build(updated)
    return updated


def task_document(record, source_sha256, source_size):
    """The task.json the build runner reads. The runner rejects unknown keys: only these may appear.

    `recipe` carries the v2 keys (sources, files, test, evidence, recipe_version, derived_from) only when the
    recipe has them; a git source's pinned commit is not in it (the commit is part of source.tar already)."""
    doc = {
        "id": record["id"], "app": record["app"], "version": record["version"], "mode": "build",
        "upstream_version": record["upstream_version"], "attempt": record["attempt"], "by": record["by"],
        "created": record["created"],
        "source": {"file": SOURCE_FILE, "sha256": source_sha256, "size": source_size},
        "recipe": record["recipe"], "limits": record["limits"],
    }
    for key in ("tag", "commit"):
        if record.get(key):
            doc[key] = record[key]
    return doc


def ai_task_document(record):
    """task.json of an AI mode task (contract 10.2): no recipe and no source, `ai` instead; the workspace
    travels as workspace.tar in the same directory. app and version are placeholders for an import."""
    doc = {
        "id": record["id"], "app": record["app"], "version": record["version"], "mode": "ai",
        "upstream_version": record["upstream_version"], "attempt": record["attempt"], "by": record["by"],
        "created": record["created"],
        "ai": {"agent": record["agent"], "kind": record["kind"], "minutes": record["minutes"]},
    }
    for key in ("tag", "commit"):
        if record.get(key):
            doc[key] = record[key]
    return doc
