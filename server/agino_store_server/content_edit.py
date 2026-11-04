# SPDX-License-Identifier: GPL-3.0-or-later
"""content-edit and content-confirm (docs/server.md section 13): change apps/<id>/app.yaml in the content
checkout, validate the whole repository, commit and optionally push. Both run under the global state lock
(taken by the caller), so they never overlap with track, build or each other."""

import contextlib
import os
import tempfile

import yaml

from . import catalogwriter, content, shots_cmds, shots_store
from . import content_git as git
from . import content_patch as patch_mod

EDITOR_RE = content.EDITOR_RE
DUMP = dict(sort_keys=False, allow_unicode=True, default_flow_style=False, width=1_000_000)


class EditUsage(shots_cmds.ShotsUsage):
    """A bad argument (exit code 2)."""


class EditError(Exception):
    """The edit cannot be done or was undone (exit code 1)."""


def dump(data):
    return yaml.safe_dump(data, **DUMP)


def _stamp():
    return catalogwriter.now().strftime(catalogwriter.TIME_FORMAT)


def _check_args(cfg, app, editor):
    shots_cmds.check_app(cfg, app)
    if not isinstance(editor, str) or not EDITOR_RE.match(editor):
        raise EditUsage("bad editor: %r" % (editor,))


def _read_app_file(path):
    """The bytes of app.yaml (a regular file, as content.load requires)."""
    with open(path, "rb") as f:
        return f.read()


def _write_atomic(path, data):
    """Replace `path` with `data` (bytes), keeping its permission bits."""
    mode = os.stat(path).st_mode & 0o777
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _load_checked(cfg):
    try:
        return content.load(cfg.content, allow_insecure=cfg.allow_insecure, policy=cfg.build_policy())
    except content.ContentError as e:
        raise EditError("store-content is invalid:\n%s" % e) from e


def _commit_change(cfg, app, original, new_data, message):
    """Write new_data as apps/<app>/app.yaml, validate the repository, commit and push. Any failure puts
    the file (and, after a failed push, the checkout) back; returns the warning lines to print."""
    rel = "apps/%s/app.yaml" % app
    path = os.path.join(cfg.content, rel)
    root = os.path.abspath(cfg.content)
    try:
        git.check_clean(root, rel)
        previous = git.head(root)
    except git.GitError as e:
        raise EditError("content checkout: %s" % e) from e
    _write_atomic(path, dump(new_data).encode("utf-8"))
    try:
        _load_checked(cfg)
        return git.publish(root, rel, message, cfg.content_push, cfg.content_push_ssh_command)
    except EditError as e:
        _write_atomic(path, original)
        raise EditError("the edit makes the content invalid and was undone:\n%s" % e) from e
    except git.GitError as e:
        if not _commit_exists(root, previous):
            _write_atomic(path, original)   # nothing was committed (or it was undone): put the file back
        raise EditError(str(e)) from e
    except BaseException:
        _write_atomic(path, original)
        raise


def _commit_exists(root, previous):
    try:
        return git.head(root) != previous
    except git.GitError:
        return False


def _load_app(cfg, app):
    path = os.path.join(cfg.content, "apps", app, "app.yaml")
    original = _read_app_file(path)
    try:
        data = yaml.safe_load(original)
    except yaml.YAMLError as e:
        raise EditError("apps/%s/app.yaml: invalid YAML" % app) from e
    if not isinstance(data, dict):
        raise EditError("apps/%s/app.yaml: not a mapping" % app)
    return original, data


def _read_patch(cfg, name):
    try:
        return patch_mod.parse(shots_cmds.read_staged(cfg, name, patch_mod.MAX_PATCH_BYTES))
    except shots_store.ShotsError as e:
        raise EditError(str(e)) from e
    except patch_mod.PatchError as e:
        raise EditError(str(e)) from e


def _delete_patch(cfg, name):
    try:
        shots_cmds.delete_staged(cfg, name)
    except OSError as e:
        raise EditError("done, but cannot delete the staged file %s: %s" % (name, e.strerror)) from e


def cmd_edit(cfg, st, args):
    app, editor = args.app, args.editor
    _check_args(cfg, app, editor)
    name = shots_cmds.check_match(args.patch, patch_mod.PATCH_NAME_RE, "patch file name")
    with st.lock():
        current = _load_checked(cfg)
        patch = _read_patch(cfg, name)
        try:
            patch_mod.validate(patch, {c.id for c in current.categories})
        except patch_mod.PatchError as e:
            raise EditError(str(e)) from e
        original, data = _load_app(cfg, app)
        new, changed = patch_mod.apply(data, patch)
        if not changed:
            _delete_patch(cfg, name)
            print("no changes for %s" % app)
            return 0
        if patch_mod.reviews_description(data, changed):
            new = patch_mod.mark_confirmed(new, editor, _stamp())
        message = "info(%s): %s by %s" % (app, ", ".join(changed), editor)
        warnings = _commit_change(cfg, app, original, new, message)
        _delete_patch(cfg, name)
    print("edited %s: %s" % (app, ", ".join(changed)))
    for line in warnings:
        print("warning: %s" % line)
    return 0


def cmd_confirm(cfg, st, args):
    app, editor = args.app, args.editor
    _check_args(cfg, app, editor)
    with st.lock():
        _load_checked(cfg)
        original, data = _load_app(cfg, app)
        if data.get("description_source") == "confirmed":
            print("description of %s is already confirmed" % app)
            return 0
        message = "info(%s): confirm description by %s" % (app, editor)
        warnings = _commit_change(cfg, app, original, patch_mod.mark_confirmed(data, editor, _stamp()), message)
    print("confirmed the description of %s" % app)
    for line in warnings:
        print("warning: %s" % line)
    return 0
