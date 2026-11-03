# SPDX-License-Identifier: GPL-3.0-or-later
"""The git side of content-edit and content-confirm (docs/server.md section 13).

Every command is a fixed argv run without a shell, with a 60 second timeout and an environment reduced to
PATH, HOME and (for the network) GIT_SSH_COMMAND. Hooks are switched off for the checkout."""

import os
import subprocess

TIMEOUT = 60
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


def run(root, args, ssh_command="", check=True):
    """`git -C root ...`; returns stdout. GitError on a non-zero exit (unless check is False) or a timeout."""
    argv = ["git", "-C", root, "-c", "user.name=" + AUTHOR[0], "-c", "user.email=" + AUTHOR[1],
            "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args]
    try:
        done = subprocess.run(argv, env=_env(ssh_command), stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, errors="replace", timeout=TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise GitError("git %s timed out after %d s" % (args[0], TIMEOUT)) from e
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


def _undo(root, previous, message):
    """Drop our commit (a --keep reset: nothing else in the working tree is touched)."""
    subject = run(root, ["log", "-1", "--format=%s"], check=False).strip()
    target = "HEAD~1" if subject == message.splitlines()[0] else previous
    run(root, ["reset", "--keep", target])


def push(root, ssh_command, previous, message):
    """Push HEAD to origin main. A rejected push gets one `pull --rebase` and one more try; if that fails
    too, our commit is undone and GitError raised."""
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
    run(root, ["rebase", "--abort"], check=False)
    try:
        _undo(root, previous, message)
    except GitError as e:
        raise GitError("%s; and the local commit could not be undone: %s" % (problem, e)) from e
    raise GitError("%s; the local commit was undone" % problem)


def publish(root, rel, message, push_enabled, ssh_command):
    """Commit `rel` and, when push_enabled, push it. Returns the warning lines to print."""
    previous = head(root)
    commit(root, rel, message)
    if not push_enabled:
        return [PUSH_OFF_WARNING]
    push(root, ssh_command, previous, message)
    return []
