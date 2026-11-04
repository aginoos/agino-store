# SPDX-License-Identifier: GPL-3.0-or-later
"""Base class for the command level tests of upstream tracking and build tasks."""

import hashlib
import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures  # noqa: E402
from agino_store_server import config  # noqa: E402

from . import build_helpers as B  # noqa: E402
from . import helpers as H  # noqa: E402
from .test_cli import CliCase  # noqa: E402

DPKG = fixtures.have("dpkg-deb", "dpkg")
GIT = fixtures.have("git")
DEPS_OK = {"ok": True, "detail": ""}


class BuildCase(CliCase):
    """A content tree with the personal app `tool` (recipe + upstream), a local git repository for it
    (tag v1.0.0) and a server.conf with [builds] directories and allow_insecure (file:// repositories)."""

    def setUp(self):
        super().setUp()
        base = os.path.dirname(self.conf)
        self.repo = os.path.join(base, "upstream-repo")
        self.commit = B.make_repo(self.repo, {"README": b"hello\n", "src/main.c": b"int main(void){return 0;}\n"},
                                  tags=("v1.0.0",)) if GIT else "1" * 40
        self.queue = os.path.join(base, "build-queue")
        self.out = os.path.join(base, "build-out")
        with open(self.conf, encoding="utf-8") as f:
            text = f.read().replace("allow_insecure = false", "allow_insecure = true")
        text += "[builds]\nqueue = %s\nout = %s\n" % (self.queue, self.out)
        H.write(self.conf, text, "w")
        self.cfg = config.load(self.conf)
        os.makedirs(self.queue)
        os.makedirs(self.out)
        B.add_built_app(self.cfg.content, upstream=dict(B.UPSTREAM, repo="file://" + self.repo))

    # state helpers

    def memo(self, app="tool"):
        return self.st.upstream_memo(app)

    def seed_seen(self, version="1.0.0", tag="v1.0.0", commit=None, **extra):
        """Pretend upstream-check has seen `version`."""
        memo = dict(self.memo(), last_ok="2026-11-04T00:00:00Z", latest_version=version,
                    seen={version: {"tag": tag, "commit": commit or self.commit, "found": "2026-11-04T00:00:00Z"}})
        memo.update(extra)
        self.st.save_upstream_memo("tool", memo)

    def request(self, version="1.0.0", by="alice"):
        code, out, err = self.run_cli("build-request", "tool", "--version", version, "--by", by)
        self.assertEqual(code, 0, err)
        return out.strip()

    def task(self, build_id):
        return self.st.build(build_id)

    def set_status(self, build_id, **changes):
        self.st.save_build(dict(self.st.build(build_id), **changes))

    def ready_task(self):
        """A task fetched by build-step (needs git)."""
        self.seed_seen()
        build_id = self.request()
        self.assertEqual(self.run_cli("build-step")[0], 0)
        self.assertEqual(self.task(build_id)["status"], "ready")
        return build_id

    def write_result(self, build_id, result, deb=None):
        """Play the runner: builds.out/<id>/result.json (+ the deb)."""
        directory = os.path.join(self.out, build_id)
        os.makedirs(directory, exist_ok=True)
        if deb is not None:
            H.write(os.path.join(directory, os.path.basename(deb[0])), deb[1])
        H.write(os.path.join(directory, "result.json"), json.dumps(result), "w")

    def make_deb(self, version, package="tool-agino", arch="amd64", **kw):
        """(path, bytes) of a tiny deb named like the recipe's package."""
        path = fixtures.build_deb(self._tmp.name, package, version, arch=arch, **kw)
        with open(path, "rb") as f:
            return path, f.read()

    def result_for(self, deb, **extra):
        path, data = deb
        art = {"file": os.path.basename(path), "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        return dict({"ok": True, "error": "", "steps": [{"n": 1, "cmd": "make", "exit": 0, "seconds": 3}],
                     "artifacts": [art]}, **extra)

    def deps_ok(self):
        return mock.patch("agino_store_server.checks.check_deps", return_value=dict(DEPS_OK))
