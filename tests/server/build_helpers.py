# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixtures for the upstream tracking and build task tests: a personal app with a recipe and an upstream file."""

import copy
import os
import subprocess

from . import helpers as H

TOOL_APP = {
    "name": H.text("小工具", "Tool"), "summary": H.text("一个工具", "A tool"),
    "description": {"zh_CN": ["工具"], "en_US": ["A tool"]},
    "categories": ["graphics"], "source": "personal", "license": "GPL-3.0-or-later",
    "proprietary": False, "developer": H.text("某人", "Someone"),
    "maintainer": {"name": "Alice", "contact": "alice@example.org"}, "icon": "icon.png",
    "package": {"kind": "deb", "name": "tool-agino"},
}

RECIPE = {
    "base": "debian-trixie",
    "source": {"kind": "git", "checksum": "required"},
    "build_depends": ["build-essential", "meson"],
    "steps": ["meson setup build --prefix=/usr", "meson compile -C build",
              "DESTDIR=$PWD/pkgroot meson install -C build"],
    "package": {"name": "tool-agino", "section": "utils", "depends": ["libc6"],
                "maintainer": "agino app store <store@aginoos.com>"},
    "limits": {"cpu_minutes": 60, "disk_gib": 10, "memory_gib": 4},
}

UPSTREAM = {
    "app": "tool", "repo": "https://github.com/example/tool.git",
    "track": {"type": "tag", "match": r"^v(\d+)\.(\d+)\.(\d+)$", "min_version": "1.0.0"},
    "build": "apps/tool/build.yaml", "auto_task": False,
}


def add_built_app(root, recipe=None, upstream=None, app=None):
    """Add apps/tool (personal deb, built from source) with build.yaml and upstreams/tool.yaml."""
    H.write_yaml(os.path.join(root, "apps", "tool", "app.yaml"), copy.deepcopy(app or TOOL_APP))
    H.write(os.path.join(root, "apps", "tool", "icon.png"), H.png(64, 64))
    H.write_yaml(os.path.join(root, "apps", "tool", "build.yaml"), copy.deepcopy(recipe or RECIPE))
    H.write_yaml(os.path.join(root, "upstreams", "tool.yaml"), copy.deepcopy(upstream or UPSTREAM))
    return root


def git(cwd, *args, env=None):
    """Run git in a throwaway repository of a test (local config only)."""
    full_env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
                    GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.org",
                    GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.org")
    full_env.update(env or {})
    done = subprocess.run(["git", *args], cwd=cwd, env=full_env, check=True, capture_output=True, text=True,
                          timeout=60)
    return done.stdout.strip()


def make_repo(path, files, tags=("v1.0.0",), annotated=()):
    """A git repository at `path` with one commit holding `files` ({name: bytes}) and the given tags.

    Returns the commit id. A value ("symlink", target) makes a symlink, ("exec", bytes) an executable file.
    """
    os.makedirs(path, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    for name, data in files.items():
        full = os.path.join(path, name)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        if isinstance(data, tuple) and data[0] == "symlink":
            os.symlink(data[1], full)
        elif isinstance(data, tuple):   # ("exec", bytes)
            H.write(full, data[1])
            os.chmod(full, 0o755)
        else:
            H.write(full, data)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    for tag in tags:
        if tag in annotated:
            git(path, "tag", "-a", "-m", "release", tag)
        else:
            git(path, "tag", tag)
    return git(path, "rev-parse", "HEAD")


def commit_more(path, name, data, message="more"):
    H.write(os.path.join(path, name), data)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", message)
    return git(path, "rev-parse", "HEAD")
