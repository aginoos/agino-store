# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared fixtures: store-content trees, configs, throwaway signing keys."""

import base64
import copy
import os
import struct
import subprocess
import sys
import zlib

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "server"))

from agino_store_server import config  # noqa: E402

SHA = "a" * 64


def text(zh, en):
    return {"zh_CN": zh, "en_US": en}


def write(path, data, mode="wb"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as f:
        f.write(data)


def write_yaml(path, obj):
    write(path, yaml.safe_dump(obj, allow_unicode=True), "w")


def png(width, height):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


GIMP = {
    "name": text("GIMP", "GIMP"), "summary": text("图像编辑器", "Image editor"),
    "description": {"zh_CN": ["段落一", "段落二"], "en_US": ["One", "Two"]},
    "categories": ["graphics"], "source": "system", "license": "GPL-3.0-or-later",
    "proprietary": False, "developer": text("GIMP 团队", "The GIMP Team"),
    "homepage": "https://www.gimp.org/", "launchable": "gimp.desktop", "icon": "icon.svg",
    "screenshots": {"zh_CN": [{"image": "screenshots/zh_CN/1.png"}],
                    "en_US": [{"image": "screenshots/en_US/1.png"}]},
    "package": {"kind": "milkyway", "name": "gimp"},
}

CHROME = {
    "name": text("Chrome", "Chrome"), "summary": text("浏览器", "Browser"),
    "description": {"zh_CN": ["浏览器"], "en_US": ["A browser"]},
    "categories": ["web"], "source": "official", "license": "LicenseRef-proprietary",
    "proprietary": True, "developer": text("谷歌", "Google"), "icon": "icon.png",
    "package": {"kind": "deb", "name": "google-chrome-stable", "vendor": "chrome"},
    "install_profile": {
        "debconf": ["code code/add-microsoft-repo boolean false"],
        "default_files": [{"path": "/etc/default/google-chrome", "content": "repo_add_once=\"false\"\n"}]},
    "release_notes": {"1.0-1": {"zh_CN": ["旧版"], "en_US": ["Old"]},
                      "2.0-1": {"zh_CN": ["新版"], "en_US": ["New"]}},
}

VENDOR = {
    "package": "google-chrome-stable", "arch": "amd64", "fetch": "auto",
    "allowed_hosts": ["dl.google.com"],
    "track": {"type": "apt", "url": "https://dl.google.com/linux/chrome/deb/", "suite": "stable",
              "component": "main", "keys": ["keys/google.asc"],
              "fingerprints": ["EB4C1BFD4F042F6DDDCCEC917721F63BD38B4796"]},
}


def make_content(root):
    """A valid store-content tree: gimp (milkyway) and chrome (deb)."""
    write_yaml(os.path.join(root, "categories.yaml"), [
        {"id": "graphics", "name": text("图形", "Graphics")}, {"id": "web", "name": text("网络", "Web")}])
    write_yaml(os.path.join(root, "featured.yaml"), [
        {"id": "essentials", "title": text("装机必备", "Essentials"), "banner": False,
         "apps": ["chrome", "gimp"]}])
    write_yaml(os.path.join(root, "apps", "gimp", "app.yaml"), copy.deepcopy(GIMP))
    write(os.path.join(root, "apps", "gimp", "icon.svg"), b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    write(os.path.join(root, "apps", "gimp", "screenshots", "zh_CN", "1.png"), png(320, 200))
    write(os.path.join(root, "apps", "gimp", "screenshots", "en_US", "1.png"), png(640, 400))
    write_yaml(os.path.join(root, "apps", "chrome", "app.yaml"), copy.deepcopy(CHROME))
    write(os.path.join(root, "apps", "chrome", "icon.png"), png(64, 64))
    write_yaml(os.path.join(root, "vendors", "chrome.yaml"), copy.deepcopy(VENDOR))
    write(os.path.join(root, "vendors", "keys", "google.asc"), b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n")
    return root


def edit_yaml(path, change):
    """Load a YAML file, apply change(dict) -> dict on a copy and write the result back."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    write_yaml(path, change(copy.deepcopy(data)))


def make_key(directory):
    """Generate a throwaway Ed25519 key; returns (private pem, public pem)."""
    key = os.path.join(directory, "test.pem")
    pub = os.path.join(directory, "test.pub.pem")
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", key],
                   check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", key, "-pubout", "-out", pub], check=True, capture_output=True)
    return key, pub


def verify_signature(directory, pub):
    """True if manifest.json.sig is a valid Ed25519 signature of manifest.json."""
    manifest = os.path.join(directory, "manifest.json")
    with open(manifest + ".sig", encoding="ascii") as f:
        raw = base64.b64decode(f.read().strip(), validate=True)
    sig = os.path.join(os.path.dirname(directory), ".sig-check.bin")
    write(sig, raw)
    try:
        done = subprocess.run(["openssl", "pkeyutl", "-verify", "-rawin", "-pubin", "-inkey", pub,
                               "-in", manifest, "-sigfile", sig], capture_output=True)
    finally:
        os.unlink(sig)
    return done.returncode == 0


def make_config(base, key="", **overrides):
    values = dict(
        content=os.path.join(base, "content"), state=os.path.join(base, "state"),
        downloads=os.path.join(base, "downloads"), preview=os.path.join(base, "preview"),
        reports=os.path.join(base, "reports"), metrics=os.path.join(base, "metrics"),
        milkyway_url="https://repo.example.invalid", milkyway_suite="milkyway",
        milkyway_components=("main",), arch="amd64", key=key,
        deb_base_url="https://store.example.invalid/debs/", valid_days=14, renew_after_hours=20,
        keep_catalogs=3, user_agent="test", min_interval_seconds=0.0, max_deb_bytes=1 << 20,
        max_index_bytes=1 << 20, allow_insecure=False)
    values.update(overrides)
    return config.Config(**values)


def write_conf_file(path, cfg):
    lines = ["[paths]", "content = %s" % cfg.content, "state = %s" % cfg.state,
             "downloads = %s" % cfg.downloads, "preview = %s" % cfg.preview,
             "reports = %s" % cfg.reports, "metrics = %s" % cfg.metrics,
             "[milkyway]", "url = %s" % cfg.milkyway_url, "suite = %s" % cfg.milkyway_suite,
             "components = %s" % " ".join(cfg.milkyway_components), "arch = %s" % cfg.arch,
             "[catalog]", "key = %s" % cfg.key, "deb_base_url = %s" % cfg.deb_base_url,
             "valid_days = %d" % cfg.valid_days, "renew_after_hours = %d" % cfg.renew_after_hours,
             "keep_catalogs = %d" % cfg.keep_catalogs,
             "[fetch]", "user_agent = %s" % cfg.user_agent,
             "min_interval_seconds = %s" % cfg.min_interval_seconds,
             "max_deb_bytes = %d" % cfg.max_deb_bytes, "max_index_bytes = %d" % cfg.max_index_bytes,
             "allow_insecure = %s" % ("true" if cfg.allow_insecure else "false")]
    write(path, "\n".join(lines) + "\n", "w")


def candidate(app="chrome", version="2.0-1", sha=SHA, **extra):
    name = "google-chrome-stable_%s_amd64.deb" % version
    record = {"app": app, "vendor": "chrome", "package": "google-chrome-stable", "arch": "amd64",
              "version": version, "sha256": sha, "size": 11, "installed_size": 4096,
              "filename": name, "file": "%s/%s" % (sha, name),
              "vendor_url": "https://dl.google.com/x.deb", "found": "2026-11-01T08:00:00Z",
              "status": "pending", "decided": None,
              "checks": {"deps": {"ok": True, "detail": ""}, "sandbox": {"status": "pass"}}}
    record.update(extra)
    return record
