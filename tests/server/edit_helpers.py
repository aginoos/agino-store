# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixtures for content-edit tests: the temporary content tree is a git checkout with a bare `origin`."""

import json
import os
import subprocess

from agino_store_server import content_git

from . import helpers as H
from .shots_helpers import ShotsCase


def git(cwd, *args):
    done = subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]})
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


def slurp(path):
    with open(path, "rb") as f:
        return f.read()


class EditCase(ShotsCase):
    """gimp and chrome in a git checkout whose origin is a bare repository next to it."""

    PUSH = "yes"

    def setUp(self):
        super().setUp()
        base = os.path.dirname(self.conf)
        self.content = self.cfg.content
        self.origin = os.path.join(base, "origin.git")
        git(base, "init", "--bare", "-b", "main", self.origin)
        git(self.content, "init", "-b", "main")
        git(self.content, "remote", "add", "origin", self.origin)
        self.commit_all("initial")
        git(self.content, "push", "origin", "main")
        self.set_content(self.PUSH)
        self.gimp = os.path.join(self.content, "apps", "gimp", "app.yaml")

    def set_content(self, push=None, ssh=None):
        """Rewrite the [content] section of server.conf (None leaves the key out)."""
        with open(self.conf, encoding="utf-8") as f:
            text = f.read().split("\n[content]")[0]
        lines = ["[content]"] + (["push = %s" % push] if push is not None else []) + (
            ["push_ssh_command = %s" % ssh] if ssh is not None else [])
        H.write(self.conf, text + "\n" + "\n".join(lines) + "\n", "w")

    def commit_all(self, message="change"):
        git(self.content, "add", "-A")
        git(self.content, "commit", "-m", message)

    def head(self, repo=None):
        return git(repo or self.content, "rev-parse", "HEAD")

    def origin_head(self):
        return git(self.origin, "rev-parse", "main")

    def subject(self, repo=None):
        return git(repo or self.content, "log", "-1", "--format=%s")

    def app_data(self, app="gimp"):
        import yaml
        return yaml.safe_load(slurp(os.path.join(self.content, "apps", app, "app.yaml")))

    def set_app(self, change, app="gimp"):
        """Change an app.yaml and commit it (the checkout must be clean for content-edit)."""
        H.edit_yaml(os.path.join(self.content, "apps", app, "app.yaml"), change)
        self.commit_all("fixture")
        try:  # the fixture commit is already on origin: content-edit's startup repair pushes unpushed commits
            git(self.content, "push", "-q", "origin", "main")
        except Exception:  # noqa: BLE001 - a checkout without a usable origin has nothing to push
            pass

    def staged(self, name):
        return os.path.join(self.staging, name)

    def edit(self, patch, app="gimp", editor="alice", name="patch-0001.json", raw=None):
        data = raw if raw is not None else json.dumps(patch).encode("utf-8")
        self.stage(name, data)
        return self.run_cli("content-edit", app, "--patch", name, "--editor", editor)

    def confirm(self, app="gimp", editor="alice"):
        return self.run_cli("content-confirm", app, "--editor", editor)

    def other_clone(self, name="other"):
        """A second clone of origin; returns its path."""
        path = os.path.join(os.path.dirname(self.conf), name)
        git(os.path.dirname(self.conf), "clone", "-b", "main", self.origin, path)
        return path

    def push_from_other(self, rel="apps/chrome/app.yaml", text="# other\n"):
        other = self.other_clone()
        with open(os.path.join(other, rel), "a", encoding="utf-8") as f:
            f.write(text)
        git(other, "commit", "-am", "other change")
        git(other, "push", "origin", "main")
        return other


NEW_TEXT = {"summary": {"zh_CN": "新的简介", "en_US": "New summary"},
            "description": {"zh_CN": ["新段落一", "新段落二"], "en_US": ["New one", "New two"]}}
assert content_git.PUSH_OFF_WARNING
