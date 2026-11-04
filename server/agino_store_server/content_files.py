# SPDX-License-Identifier: GPL-3.0-or-later
"""Commit new or changed files to the content checkout (used by intake-adopt; docs/server.md section 15).

Like content-edit (section 13): the checkout is put back into shape first (an interrupted run may have left an
unfinished rebase), the files are written, the whole repository is validated with the strict settings of
server.conf, and only then committed and pushed. Any failure puts every file back (and drops our commit after
a failed push). The caller holds the global state lock.
"""

import contextlib
import os
import tempfile

from . import catalogwriter, content, content_edit, content_repair
from . import content_git as git


class CommitError(Exception):
    """Nothing was committed (or it was undone); the message says why."""


def _stamp():
    return catalogwriter.now().strftime(catalogwriter.TIME_FORMAT)


def _read_original(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _restore(root, originals):
    """Put every file back as it was: the old bytes, or deleted when it did not exist (empty directories go too)."""
    for rel, data in originals.items():
        path = os.path.join(root, rel)
        if data is None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
            _prune(root, os.path.dirname(path))
        else:
            _write(path, data)


def _prune(root, directory):
    while directory != root and directory.startswith(root + os.sep):
        try:
            os.rmdir(directory)
        except OSError:
            return
        directory = os.path.dirname(directory)


def _commit_exists(root, previous):
    try:
        return git.head(root) != previous
    except git.GitError:
        return False


def _rollback(root, originals, previous, message):
    try:
        content_repair.release(root)
    except (OSError, git.GitError):
        pass
    try:
        if _commit_exists(root, previous):
            git.undo(root, previous, message)
    except git.GitError:
        return   # the commit stays, so the files must stay as committed
    _restore(root, originals)


def commit_files(cfg, files, message):
    """Write `files` ({relative path: bytes}) into the content checkout, validate, commit and push.
    Returns the warning lines (for example that nothing was pushed). CommitError on any problem."""
    root = os.path.abspath(cfg.content)
    rels = sorted(files)
    with content_edit.interruptible():
        try:
            content_repair.repair(root, cfg.state, _stamp(), cfg.content_push, cfg.content_push_ssh_command)
            git.check_clean_paths(root, rels)
            previous = git.head(root)
        except git.GitError as e:
            raise CommitError("content checkout: %s" % e) from e
        originals = {rel: _read_original(os.path.join(root, rel)) for rel in rels}
        try:
            for rel in rels:
                _write(os.path.join(root, rel), files[rel])
            try:
                content.load(root, allow_insecure=cfg.allow_insecure, policy=cfg.build_policy())
            except content.ContentError as e:
                _restore(root, originals)
                raise CommitError("the change makes the content invalid and was undone:\n%s" % e) from e
            return git.publish_paths(root, rels, message, cfg.content_push, cfg.content_push_ssh_command)
        except CommitError:
            raise
        except git.GitError as e:
            if not _commit_exists(root, previous):
                _restore(root, originals)
            raise CommitError(str(e)) from e
        except BaseException:
            _rollback(root, originals, previous, message)
            raise
