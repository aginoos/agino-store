# SPDX-License-Identifier: GPL-3.0-or-later
"""Put the content checkout back into a usable state before content-edit / content-confirm touch it
(docs/server.md section 13). A killed run can leave an unfinished rebase or merge, a stale .git/index.lock,
a half-written app.yaml or a commit that was never pushed; without this every later edit would fail.

Callers hold the global state lock, so no other agino-store-server command runs; a git process started by a
person in the checkout is detected through /proc and makes the repair refuse instead of guessing."""

import os
import sys
import time

from . import content_git as git

STALE_LOCK_SECONDS = 600
RECOVERED_DIR = "content-recovered"   # under the state directory: diffs of discarded uncommitted changes


def _emit(message):
    print("content-repair: %s" % message, file=sys.stderr)


def busy_git_process(root):
    """The pid of another git process working in `root` (its cwd is inside it, or `root` is on its command
    line), or None. Unreadable /proc entries are skipped."""
    real = os.path.realpath(root)
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        base = os.path.join("/proc", name)
        try:
            with open(os.path.join(base, "comm"), encoding="utf-8", errors="replace") as f:
                if not f.read().strip().startswith("git"):
                    continue
            cwd = os.path.realpath(os.readlink(os.path.join(base, "cwd")))
            with open(os.path.join(base, "cmdline"), "rb") as f:
                argv = f.read().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        if cwd == real or cwd.startswith(real + os.sep) or real in argv:
            return int(name)
    return None


def _git_dir(root):
    try:
        return git.run(root, ["rev-parse", "--absolute-git-dir"]).strip()
    except git.GitError:
        return None   # not a repository: the normal flow reports it


def _clear_lock(root, gitdir, min_age, now):
    lock = os.path.join(gitdir, "index.lock")
    try:
        age = now() - os.stat(lock).st_mtime
    except FileNotFoundError:
        return
    if age < min_age:
        raise git.GitError(".git/index.lock is only %d s old; another git may be running, try again later" % age)
    os.unlink(lock)
    _emit("removed a stale .git/index.lock (%d s old)" % age)


def _abort_unfinished(root, gitdir):
    for marker, args in (("rebase-merge", "rebase"), ("rebase-apply", "rebase"), ("MERGE_HEAD", "merge")):
        if os.path.exists(os.path.join(gitdir, marker)):
            _emit("aborting an unfinished %s (%s)" % (args, marker))
            git.run(root, [args, "--abort"], check=False)
            if os.path.exists(os.path.join(gitdir, marker)):
                raise git.GitError("cannot abort the unfinished %s in the content checkout (%s); fix it by hand" % (
                    args, marker))


def _discard_uncommitted(root, state_dir, stamp):
    """Tracked changes under apps/ are not ours (we hold the lock): save the diff, then drop them."""
    if not git.run(root, ["status", "--porcelain", "--untracked-files=no", "--", "apps"]).strip():
        return
    diff = git.run(root, ["diff", "HEAD", "--", "apps"])
    folder = os.path.join(state_dir, RECOVERED_DIR)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, stamp.replace(":", "") + ".diff")
    with open(path, "w", encoding="utf-8") as f:
        f.write(diff)
    git.run(root, ["checkout", "HEAD", "--", "apps"])
    _emit("discarded uncommitted changes under apps/ (diff saved to %s):\n%s" % (path, diff.rstrip()))


def _push_unpushed(root, ssh_command):
    count = git.unpushed(root)
    if not count:
        return
    _emit("%d local commit(s) were never pushed; pushing them" % count)
    try:
        git.push_retry(root, ssh_command)
    except git.GitError as e:
        raise git.GitError(
            "%d local commit(s) in the content checkout are not on origin and pushing failed (%s); "
            "they are kept, push or reset them by hand" % (count, e)) from e


def repair(root, state_dir, stamp, push_enabled, ssh_command, min_lock_age=STALE_LOCK_SECONDS, now=time.time):
    """Fix the leftovers of an interrupted run; GitError when that is not possible or not safe.
    `min_lock_age`: how old an index.lock must be (seconds) to count as stale."""
    gitdir = _git_dir(root)
    if gitdir is None:
        return
    leftovers = any(os.path.exists(os.path.join(gitdir, n))
                    for n in ("index.lock", "rebase-merge", "rebase-apply", "MERGE_HEAD"))
    if leftovers:
        pid = busy_git_process(root)
        if pid is not None:
            raise git.GitError("another git process (pid %d) is using the content checkout" % pid)
    _clear_lock(root, gitdir, min_lock_age, now)
    _abort_unfinished(root, gitdir)
    _discard_uncommitted(root, state_dir, stamp)
    if push_enabled:
        _push_unpushed(root, ssh_command)


def release(root):
    """After our own interrupted git child was killed (global lock held): drop its index.lock and abort a
    rebase or merge it left behind. Unlike repair() this keeps every change and commit."""
    gitdir = _git_dir(root)
    if gitdir is None:
        return
    _clear_lock(root, gitdir, 0, time.time)
    _abort_unfinished(root, gitdir)
