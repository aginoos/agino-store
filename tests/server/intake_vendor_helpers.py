# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixtures for the vendor path of AI intake: a delivery with vendor.yaml and a local fake vendor."""

import copy
import os
import tempfile

from agino_store_server import content

from . import fixtures
from . import helpers as H
from .intake_case import APP, dump

PACKAGE = "demo-app"


def app_yaml(**changes):
    """The app.yaml an agent delivers for the vendor's deb (the server sets package, icon, maintainer)."""
    return dict(copy.deepcopy(APP), package={"kind": "deb", "name": PACKAGE}, **changes)


def vendor_yaml(base, **changes):
    """A vendor.yaml tracking `base`/latest.json (the local fake vendor, host 127.0.0.1)."""
    track = {"type": "json", "url": base + "/latest.json", "version_path": "version", "url_path": "url"}
    vendor = {"package": PACKAGE, "arch": "amd64", "fetch": "auto", "allowed_hosts": ["127.0.0.1"], "track": track}
    vendor.update(changes)
    return vendor


def files(base, app=None, vendor=None, notes=b"License: see the page.\n", **extra):
    """The files of a vendor delivery ({name: bytes})."""
    out = {"vendor.yaml": dump(vendor if vendor is not None else vendor_yaml(base)),
           "app.yaml": dump(app if app is not None else app_yaml()), "NOTES.md": notes}
    out.update(extra)
    return out


def distribution_supported():
    """True when content.load already knows the `distribution` key of app.yaml."""
    with tempfile.TemporaryDirectory() as tmp:
        H.make_content(tmp)
        path = os.path.join(tmp, "apps", "chrome", "app.yaml")
        H.edit_yaml(path, lambda d: dict(d, distribution={"mode": "store"}))
        try:
            content.load(tmp, allow_insecure=True)
        except content.ContentError:
            return False
    return True


def serve(server, version, package=PACKAGE, **deb_kw):
    """Publish `version` of the demo deb on the fake vendor; returns (path, bytes) of the deb."""
    with tempfile.TemporaryDirectory() as tmp:
        deb = fixtures.build_deb(tmp, package, version, **deb_kw)
        with open(deb, "rb") as f:
            data = f.read()
    name = "/files/%s_%s_amd64.deb" % (package, version)
    server.routes[name] = fixtures.Route(body=data)
    server.routes["/latest.json"] = fixtures.Route(body=('{"version": "%s", "url": "%s"}' % (version, name)).encode())
    return name, data
