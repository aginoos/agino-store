# SPDX-License-Identifier: GPL-3.0-or-later
"""Base class and fixtures for the AI source intake tests.

The content tree is a git checkout (no origin, push off) holding gimp, chrome and the built app `tool`. The
"runner" is played by the test: it writes builds.out/<id>/ai/* and result.json for AI tasks, and result.json
plus a deb for verification builds. Network is never used: the page fetch is mocked, tags are mocked.
"""

import hashlib
import json
import os
import subprocess
import tarfile
from unittest import mock

import yaml
from agino_store_server import config, intake, intake_materials

from . import build_helpers as B
from . import helpers as H
from .build_case import BuildCase

ADDRESS = "https://vendor.example.com/newtool"
PAGE = ("Newtool 1.2.0\nDownload the source.\n", [("https://github.com/example/newtool/archive/v1.2.0.tar.gz", "Source")])
TAGS = {"v1.0.0": "a" * 40, "v1.2.0": "b" * 40, "v1.3.0-rc1": "c" * 40}

RECIPE = {
    "base": "debian-trixie",
    "sources": [{"name": "src", "kind": "git", "url": "https://github.com/example/newtool.git", "ref": "v${VERSION}"}],
    "build_depends": ["build-essential"],
    "steps": ["cd src && make", "mkdir -p pkgroot/usr/bin && cp src/newtool pkgroot/usr/bin/"],
    "package": {"name": "newtool-agino", "section": "utils", "depends": ["libc6"],
                "maintainer": "agino app store <store@aginoos.com>"},
    "test": ["newtool --version"],
    "evidence": [{"claim": "license is MIT", "url": "https://github.com/example/newtool/blob/main/LICENSE"}],
}
APP = {
    "name": {"zh_CN": "新工具", "en_US": "Newtool"}, "summary": {"zh_CN": "一个工具", "en_US": "A tool"},
    "description": {"zh_CN": ["工具"], "en_US": ["A tool"]}, "categories": ["graphics"], "source": "personal",
    "license": "MIT", "proprietary": False, "developer": {"zh_CN": "某人", "en_US": "Someone"},
    "package": {"kind": "deb", "name": "newtool-agino"},
}
UPSTREAM = {"app": "newtool", "repo": "https://github.com/example/newtool.git",
            "track": {"type": "tag", "match": r"^v(\d+)\.(\d+)\.(\d+)$"}, "build": "apps/newtool/build.yaml"}


def dump(data):
    return yaml.safe_dump(data, allow_unicode=True).encode("utf-8")


def delivery(recipe=None, app=None, upstream=UPSTREAM, notes=b"notes\n"):
    """The files an agent delivers for an import ({name: bytes})."""
    files = {"recipe.yaml": dump(RECIPE if recipe is None else recipe), "NOTES.md": notes}
    if app is not False:
        files["app.yaml"] = dump(APP if app is None else app)
    if upstream:
        files["upstream.yaml"] = dump(upstream)
    return files


def git(cwd, *args):
    done = subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@t", *args], capture_output=True,
                          text=True, env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/")})
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


class IntakeCase(BuildCase):
    CONF = "[intake]\nagents = claude codex\nproxy_allow_file = {allow}\n"

    def setUp(self):
        super().setUp()
        self.allow_file = os.path.join(os.path.dirname(self.conf), "builder-proxy", "allow-extra.list")
        os.makedirs(os.path.dirname(self.allow_file))
        self.write_conf(self.CONF)
        content = self.cfg.content
        git(content, "init", "-q", "-b", "main")
        git(content, "add", "-A")
        git(content, "commit", "-q", "-m", "initial")
        patcher = mock.patch.object(intake_materials, "fetch_page", return_value=PAGE)
        self.fetch_page = patcher.start()
        self.addCleanup(patcher.stop)

    def write_conf(self, extra):
        with open(self.conf, encoding="utf-8") as f:
            text = f.read().split("\n[intake]")[0]
        H.write(self.conf, text + "\n" + extra.format(allow=self.allow_file), "w")
        self.cfg = config.load(self.conf)

    def new_task(self, address=ADDRESS, kind="import", **fields):
        """Save an intake record directly (for addresses the command line refuses, such as file:// in tests)."""
        record = dict(intake.new_record(self.st, kind, address, "", "claude", [], "alice", "2026-11-04T10:00:00Z"), **fields)
        self.st.save_intake(record)
        return record["id"]

    # commands

    def submit(self, address=ADDRESS, *extra, by="alice"):
        code, out, err = self.run_cli("intake-submit", address, "--by", by, *extra)
        self.assertEqual(code, 0, err)
        return out.strip()

    def step(self, expect=0):
        code, out, err = self.run_cli("intake-step")
        self.assertEqual(code, expect, out + err)
        return out

    def intake(self, intake_id):
        return self.st.intake(intake_id)

    def istatus(self, intake_id):
        return self.intake(intake_id)["status"]

    # playing the runner

    def run_ai(self, intake_id, files=None, result=None, names=None):
        """The AI task of `intake_id` runs and ends: files (or `result`) land in builds.out; build-step collects."""
        ai_id = self.intake(intake_id)["ai_task"]
        self.set_status(ai_id, status="building", started="2026-11-04T10:00:00Z")
        directory = os.path.join(self.out, ai_id)
        if files is not None:
            os.makedirs(os.path.join(directory, "ai"), exist_ok=True)
            for name, data in files.items():
                H.write(os.path.join(directory, "ai", name), data)
        H.write(os.path.join(directory, "result.json"), json.dumps(
            result if result is not None else {"ok": True, "error": "", "steps": [], "artifacts": [],
                                               "files": names if names is not None else sorted(files or {})}), "w")
        self.run_cli("build-step")
        return ai_id

    def run_verify(self, intake_id, ok=True, error="test 1 failed", deb=None, klass="recipe", log=b"log line\n"):
        """The verification build ends; build-step collects it."""
        verify_id = self.intake(intake_id)["verify_task"]
        record = self.st.build(verify_id)
        self.set_status(verify_id, status="building", started="2026-11-04T10:00:00Z")
        directory = os.path.join(self.out, verify_id)
        os.makedirs(directory, exist_ok=True)
        H.write(os.path.join(directory, "build.log"), log)
        if ok:
            deb = deb or self.make_deb(record["version"], package="newtool-agino")
            self.write_result(verify_id, self.result_for(deb, **{"class": "ok"}), deb)
        else:
            self.write_result(verify_id, {"ok": False, "error": error, "class": klass, "steps": [], "artifacts": []})
        with self.deps_ok():
            self.run_cli("build-step")
        return verify_id

    # reading what was made

    def workspace(self, ai_id):
        """{member name: bytes or None for a directory} of the workspace.tar of an AI task."""
        out = {}
        with tarfile.open(os.path.join(self.queue, ai_id, "workspace.tar")) as tar:
            for member in tar:
                out[member.name] = tar.extractfile(member).read() if member.isreg() else None
        return out

    @staticmethod
    def sha(data):
        return hashlib.sha256(data).hexdigest()


def tags_patch():
    """Patch the tag listing used to find the version of a new application."""
    from agino_store_server import upstream_fetch
    return mock.patch.object(upstream_fetch, "list_tags", return_value=dict(TAGS))


__all__ = ["IntakeCase", "delivery", "tags_patch", "ADDRESS", "RECIPE", "APP", "UPSTREAM", "TAGS", "B", "dump"]
