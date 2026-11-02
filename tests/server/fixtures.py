# SPDX-License-Identifier: GPL-3.0-or-later
"""Test helpers: temporary GPG keys, tiny debs, a signed apt repo and a local HTTP server.

Nothing here touches the network; the HTTP server listens on 127.0.0.1 only.
"""

import gzip
import hashlib
import http.server
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server"))

from agino_store_server.config import Config  # noqa: E402

RELEASE_DATE = "Mon, 02 Nov 2026 08:00:00 UTC"


def have(*tools):
    return all(shutil.which(t) for t in tools)


def run(args, **kw):
    return subprocess.run(args, check=True, capture_output=True, timeout=120, **kw)


def make_cfg(tmp, base_url="http://127.0.0.1:1", **over):
    values = dict(
        content=os.path.join(tmp, "content"), state=os.path.join(tmp, "state"),
        downloads=os.path.join(tmp, "downloads"), preview=os.path.join(tmp, "preview"),
        reports=os.path.join(tmp, "reports"), metrics=os.path.join(tmp, "metrics"),
        milkyway_url=base_url.rstrip("/") + "/milkyway", milkyway_suite="milkyway",
        milkyway_components=("main",), arch="amd64", key=os.path.join(tmp, "key.pem"),
        deb_base_url="http://127.0.0.1/debs/", valid_days=14, renew_after_hours=20,
        keep_catalogs=3, user_agent="agino-store-server-test/0.1", min_interval_seconds=0.0,
        max_deb_bytes=50_000_000, max_index_bytes=10_000_000, allow_insecure=True,
    )
    values.update(over)
    return Config(**values)


# GPG

@dataclass(frozen=True)
class TestKey:
    home: str
    fingerprint: str
    asc: str


def gen_key(workdir, name="Test Vendor"):
    home = tempfile.mkdtemp(prefix="gk-", dir=workdir)
    os.chmod(home, 0o700)
    run(["gpg", "--batch", "--homedir", home, "--pinentry-mode", "loopback", "--passphrase", "",
         "--quick-gen-key", "%s <test@example.org>" % name, "ed25519", "sign", "never"])
    out = run(["gpg", "--homedir", home, "--with-colons", "--fingerprint"]).stdout.decode()
    fpr = next(line.split(":")[9] for line in out.split("\n") if line.startswith("fpr:"))
    asc = os.path.join(home, "key.asc")
    with open(asc, "wb") as f:
        f.write(run(["gpg", "--homedir", home, "--armor", "--export", fpr]).stdout)
    return TestKey(home, fpr, asc)


def stop_agent(key):
    subprocess.run(["gpgconf", "--homedir", key.home, "--kill", "all"], capture_output=True, timeout=30)


def clearsign(key, text, out_path):
    src = out_path + ".in"
    with open(src, "w", encoding="utf-8") as f:
        f.write(text)
    run(["gpg", "--batch", "--homedir", key.home, "--pinentry-mode", "loopback", "--passphrase", "",
         "--default-key", key.fingerprint, "--digest-algo", "SHA256", "--yes", "--clearsign", "--output", out_path, src])
    os.unlink(src)


# debs

def build_deb(outdir, package, version, arch="amd64", depends=None, scripts=None, files=None,
              modes=None, extra_control=None):
    """Build a tiny deb. files: {path: bytes}; modes: {path: int}; scripts: {name: text}."""
    work = tempfile.mkdtemp(prefix="deb-", dir=outdir)
    os.makedirs(os.path.join(work, "DEBIAN"))
    control = ["Package: %s" % package, "Version: %s" % version, "Architecture: %s" % arch,
               "Maintainer: Test <test@example.org>", "Description: test package"]
    if depends:
        control.append("Depends: %s" % depends)
    control.extend("%s: %s" % kv for kv in (extra_control or {}).items())
    with open(os.path.join(work, "DEBIAN", "control"), "w") as f:
        f.write("\n".join(control) + "\n")
    for name, text in (scripts or {}).items():
        path = os.path.join(work, "DEBIAN", name)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o755)
    for rel, data in (files or {"usr/share/%s/README" % package: b"hello\n"}).items():
        path = os.path.join(work, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        os.chmod(path, (modes or {}).get(rel, 0o644))
    deb = os.path.join(outdir, "%s_%s_%s.deb" % (package, version.replace(":", "_"), arch))
    run(["dpkg-deb", "--build", "--root-owner-group", work, deb])
    shutil.rmtree(work)
    return deb


def sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def packages_stanza(deb, filename):
    fields = run(["dpkg-deb", "-f", deb]).stdout.decode().rstrip("\n")
    with open(deb, "rb") as f:
        data = f.read()
    return "%s\nFilename: %s\nSize: %d\nSHA256: %s\n" % (fields, filename, len(data),
                                                         hashlib.sha256(data).hexdigest())


def write_apt_repo(root, key, debs, suite="stable", component="main", arch="amd64", date=RELEASE_DATE):
    """Write root/dists/<suite>/... with a signed InRelease and root/pool/*.deb. Returns Packages text."""
    pool = os.path.join(root, "pool")
    os.makedirs(pool, exist_ok=True)
    stanzas = []
    for deb in debs:
        name = os.path.basename(deb)
        shutil.copy(deb, os.path.join(pool, name))
        stanzas.append(packages_stanza(deb, "pool/" + name))
    packages = "\n".join(stanzas).encode()
    rel = "%s/binary-%s" % (component, arch)
    directory = os.path.join(root, "dists", suite, rel)
    os.makedirs(directory, exist_ok=True)
    gz = gzip.compress(packages)
    entries = {rel + "/Packages": packages, rel + "/Packages.gz": gz}
    for path, data in entries.items():
        with open(os.path.join(root, "dists", suite, path), "wb") as f:
            f.write(data)
    lines = ["Origin: Test", "Suite: %s" % suite, "Date: %s" % date, "SHA256:"]
    lines += [" %s %d %s" % (hashlib.sha256(d).hexdigest(), len(d), p) for p, d in entries.items()]
    clearsign(key, "\n".join(lines) + "\n", os.path.join(root, "dists", suite, "InRelease"))
    return packages.decode()


def write_milkyway_repo(root, stanzas, suite="milkyway", component="main", arch="amd64"):
    """Unsigned milkyway repo: Release plus Packages.gz. stanzas: list of dicts (field -> value)."""
    text = "\n".join("\n".join("%s: %s" % kv for kv in s.items()) + "\n" for s in stanzas)
    rel = "%s/binary-%s/Packages.gz" % (component, arch)
    gz = gzip.compress(text.encode())
    path = os.path.join(root, "dists", suite, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(gz)
    release = "Suite: %s\nDate: %s\nSHA256:\n %s %d %s\n" % (
        suite, RELEASE_DATE, hashlib.sha256(gz).hexdigest(), len(gz), rel)
    with open(os.path.join(root, "dists", suite, "Release"), "w") as f:
        f.write(release)


# HTTP server

@dataclass
class Route:
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    redirect: str | None = None
    etag: str | None = None
    status: int = 200


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _serve(self, send_body):
        server = self.server
        server.hits.append((self.command, self.path, dict(self.headers)))
        route = server.routes.get(self.path)
        if route is None:
            self.send_error(404)
            return
        if route.redirect:
            self.send_response(302)
            self.send_header("Location", route.redirect)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if route.etag and self.headers.get("If-None-Match") == route.etag:
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(route.status)
        for k, v in route.headers.items():
            self.send_header(k, v)
        if route.etag:
            self.send_header("ETag", route.etag)
        self.send_header("Content-Length", str(len(route.body)))
        self.end_headers()
        if send_body:
            self.wfile.write(route.body)

    def do_GET(self):
        self._serve(True)

    def do_HEAD(self):
        self._serve(False)


class TestServer:
    """Serves self.routes (path -> Route); a directory can be mounted with mount()."""

    def __init__(self):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.routes = {}
        self.httpd.hits = []
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def routes(self):
        return self.httpd.routes

    @property
    def hits(self):
        return self.httpd.hits

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def mount(self, directory, prefix=""):
        for dirpath, _, names in os.walk(directory):
            for name in names:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, directory).replace(os.sep, "/")
                with open(full, "rb") as f:
                    self.routes["%s/%s" % (prefix, rel)] = Route(body=f.read())

    def count(self, method, path):
        return sum(1 for m, p, _ in self.hits if m == method and p == path)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=10)


# content adapter

@dataclass(frozen=True)
class FakeContent:
    root: str
    vendors: dict
    apps: dict


def deb_app(vendor_id, name="pkg"):
    return {"package": {"kind": "deb", "name": name, "vendor": vendor_id}}


def milkyway_app(name):
    return {"package": {"kind": "milkyway", "name": name}}


def install_key(content_root, key, name="test.asc"):
    directory = os.path.join(content_root, "vendors", "keys")
    os.makedirs(directory, exist_ok=True)
    shutil.copy(key.asc, os.path.join(directory, name))
    return "keys/" + name
