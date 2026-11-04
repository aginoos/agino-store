# SPDX-License-Identifier: GPL-3.0-or-later
"""The git side of content-edit and content-confirm (docs/server.md section 13).

Every command is a fixed argv run without a shell, with a timeout (push 120 s, pull 60 s, anything else
30 s) and an environment reduced to PATH, HOME and (for the network) GIT_SSH_COMMAND. Hooks are switched off
for the checkout."""

import os
import subprocess

TIMEOUT_PUSH = 120
TIMEOUT_PULL = 60
TIMEOUT_DEFAULT = 30
AUTHOR = ("agino-store", "agino-store@localhost")
BRANCH = "main"
PUSH_OFF_WARNING = "not pushed (content.push is off)"


class GitError(Exception):
    pass


def _env(ssh_command):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_TERMINAL_PROMPT": "0"}
    if os.environ.get("HOME"):
        env["HOME"] = os.environ["HOME"]
    if ssh_command:
        env["GIT_SSH_COMMAND"] = ssh_command
    return env


def _timeout(args):
    return {"push": TIMEOUT_PUSH, "pull": TIMEOUT_PULL}.get(args[0], TIMEOUT_DEFAULT)


def run(root, args, ssh_command="", check=True):
    """`git -C root ...`; returns stdout. GitError on a non-zero exit (unless check is False) or a timeout."""
    argv = ["git", "-C", root, "-c", "user.name=" + AUTHOR[0], "-c", "user.email=" + AUTHOR[1],
            "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args]
    limit = _timeout(args)
    try:
        done = subprocess.run(argv, env=_env(ssh_command), stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, errors="replace", timeout=limit)
    except subprocess.TimeoutExpired as e:
        raise GitError("git %s timed out after %d s" % (args[0], limit)) from e
    except OSError as e:
        raise GitError("cannot run git: %s" % e.strerror) from e
    if check and done.returncode != 0:
        detail = (done.stderr.strip() or done.stdout.strip() or "exit %d" % done.returncode)
        raise GitError("git %s failed: %s" % (args[0], detail.splitlines()[-1] if detail else ""))
    return done.stdout


def head(root):
    return run(root, ["rev-parse", "--verify", "HEAD"]).strip()


def check_clean(root, rel):
    """The checkout must be a git repository and `rel` must have no uncommitted changes."""
    head(root)
    if run(root, ["status", "--porcelain", "--", rel]).strip():
        raise GitError("%s has uncommitted changes in the content checkout" % rel)


def commit(root, rel, message):
    """Commit only `rel`; returns the new HEAD."""
    try:
        run(root, ["add", "--", rel])
        run(root, ["commit", "--no-verify", "-m", message, "--only", "--", rel])
    except GitError:
        run(root, ["reset", "-q", "HEAD", "--", rel], check=False)
        raise
    return head(root)


def check_clean_paths(root, rels):
    """The checkout must be a git repository and none of `rels` may have uncommitted changes."""
    head(root)
    if run(root, ["status", "--porcelain", "--", *rels]).strip():
        raise GitError("%s has uncommitted changes in the content checkout" % ", ".join(rels))


def commit_paths(root, rels, message):
    """Commit only the paths `rels` (new or changed files); returns the new HEAD."""
    try:
        run(root, ["add", "--", *rels])
        run(root, ["commit", "--no-verify", "-m", message, "--only", "--", *rels])
    except GitError:
        run(root, ["reset", "-q", "HEAD", "--", *rels], check=False)
        raise
    return head(root)


def publish_paths(root, rels, message, push_enabled, ssh_command):
    """commit_paths and, when push_enabled, push it. Returns the warning lines to print."""
    previous = head(root)
    commit_paths(root, rels, message)
    if not push_enabled:
        return [PUSH_OFF_WARNING]
    push(root, ssh_command, previous, message)
    return []


def undo(root, previous, message):
    """Drop our commit (a --keep reset: nothing else in the working tree is touched)."""
    subject = run(root, ["log", "-1", "--format=%s"], check=False).strip()
    target = "HEAD~1" if subject == message.splitlines()[0] else previous
    run(root, ["reset", "--keep", target])


def push_retry(root, ssh_command):
    """Push HEAD to origin main. A rejected push gets one `pull --rebase` and one more try; if that fails
    too, an unfinished rebase is aborted and GitError raised (nothing else is touched)."""
    refspec = "HEAD:" + BRANCH
    try:
        run(root, ["push", "origin", refspec], ssh_command)
        return
    except GitError as first:
        problem = first
    try:
        run(root, ["pull", "--rebase", "--autostash", "origin", BRANCH], ssh_command)
        run(root, ["push", "origin", refspec], ssh_command)
        return
    except GitError as second:
        problem = GitError("%s; after pull --rebase: %s" % (problem, second))
    abort_operations(root)
    raise problem


def push(root, ssh_command, previous, message):
    """push_retry; when it fails our commit is undone and GitError raised."""
    try:
        push_retry(root, ssh_command)
    except GitError as problem:
        try:
            undo(root, previous, message)
        except GitError as e:
            raise GitError("%s; and the local commit could not be undone: %s" % (problem, e)) from e
        raise GitError("%s; the local commit was undone" % problem) from problem


def abort_operations(root):
    """Abort an unfinished rebase or merge (best effort; each is a no-op when none is running)."""
    for args in (["rebase", "--abort"], ["merge", "--abort"]):
        try:
            run(root, args, check=False)
        except GitError:
            pass   # a timeout or a missing git: the caller reports what is left


def unpushed(root):
    """Number of local commits not on origin/main, by the local tracking ref (0 when it is unknown)."""
    out = run(root, ["rev-list", "--count", "origin/%s..HEAD" % BRANCH], check=False).strip()
    return int(out) if out.isdigit() else 0


def publish(root, rel, message, push_enabled, ssh_command):
    """Commit `rel` and, when push_enabled, push it. Returns the warning lines to print."""
    previous = head(root)
    commit(root, rel, message)
    if not push_enabled:
        return [PUSH_OFF_WARNING]
    push(root, ssh_command, previous, message)
    return []
