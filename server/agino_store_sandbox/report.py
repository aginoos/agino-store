# SPDX-License-Identifier: GPL-3.0-or-later
"""Report building and signing (docs/server.md section 9: schema 1, Ed25519 over the exact bytes)."""

import base64
import json
import os
import socket
import stat
import subprocess
import tempfile

from .util import SandboxError, clean

CHECK_NAMES = ("installed", "third_party_sources", "desktop_file", "window_seen", "removed_clean")


def failed_checks(checks, problems=()):
    """Names of the checks that block a pass (new_errors is informational)."""
    failed = []
    if not checks.get("installed"):
        failed.append("installed")
    if checks.get("third_party_sources"):
        failed.append("third_party_sources")
    if not checks.get("desktop_file"):
        failed.append("desktop_file")
    if not checks.get("window_seen"):
        failed.append("window_seen")
    if not checks.get("removed_clean"):
        failed.append("removed_clean")
    return failed


def build(job, checks, details, problems, started, finished, host=None):
    failed = failed_checks(checks, problems)
    if failed:
        summary = "failed checks: " + ", ".join(failed)
    else:
        summary = "all checks passed"
    if problems:
        summary += "; " + "; ".join(clean(p, 200) for p in problems[:5])
    summary += "; new journal errors: %d" % checks.get("new_errors", 0)
    return {"schema": 1, "app": job.app, "version": job.version, "sha256": job.sha256,
            "status": "fail" if failed else "pass", "started": started, "finished": finished,
            "host": host or socket.gethostname(), "checks": dict(checks), "summary": clean(summary, 1900),
            "details": details}


def build_failure(job, reason, started, finished, host=None):
    checks = {"installed": False, "third_party_sources": [], "desktop_file": None, "window_seen": False,
              "new_errors": 0, "removed_clean": False}
    return {"schema": 1, "app": job.app, "version": job.version, "sha256": job.sha256, "status": "fail",
            "started": started, "finished": finished, "host": host or socket.gethostname(), "checks": checks,
            "summary": clean("sandbox could not run: " + reason, 1900), "details": {}}


def encode(report):
    return (json.dumps(report, indent=1, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def check_key_file(path):
    try:
        st = os.stat(path)
    except OSError:
        raise SandboxError("signing key is not readable") from None
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid():
        raise SandboxError("signing key must be a regular file owned by the current user with mode 0600")


def sign(key_path, data):
    """Base64 Ed25519 signature of data made by `openssl pkeyutl -sign -rawin`."""
    check_key_file(key_path)
    with tempfile.TemporaryDirectory() as tmp:
        src, out = os.path.join(tmp, "in"), os.path.join(tmp, "sig")
        with open(src, "wb") as f:
            f.write(data)
        done = subprocess.run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", key_path, "-in", src, "-out", out],
                              capture_output=True)
        if done.returncode != 0:
            raise SandboxError("signing failed: " + done.stderr.decode("utf-8", "replace")[:200])
        with open(out, "rb") as f:
            sig = f.read(256)
    if len(sig) != 64:
        raise SandboxError("signing produced a signature of unexpected length")
    return base64.b64encode(sig).decode() + "\n"


def _atomic(directory, name, data):
    tmp = os.path.join(directory, ".%s.tmp" % name)
    with open(tmp, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, os.path.join(directory, name))


def write(reports_dir, key_path, stem, report):
    """Write <stem>.json.sig first, then <stem>.json, so the server never sees an unsigned report."""
    data = encode(report)
    sig = sign(key_path, data)
    os.makedirs(reports_dir, mode=0o755, exist_ok=True)
    _atomic(reports_dir, stem + ".json.sig", sig.encode("ascii"))
    _atomic(reports_dir, stem + ".json", data)
    return stem + ".json"
