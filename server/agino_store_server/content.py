# SPDX-License-Identifier: GPL-3.0-or-later
"""Load and strictly validate store-content (docs/server.md section 3).

The YAML is only parsed with yaml.safe_load. Every problem found is collected and
raised together as one ContentError ("path: message" per line). The result is a tree
of frozen dataclasses built fresh from the parsed data.
"""

import hashlib
import ipaddress
import os
import re
import stat
from dataclasses import dataclass
from urllib.parse import urlsplit

import yaml

from . import demo, shots

ID_RE = re.compile(r"\A[a-z0-9][a-z0-9.+-]*\Z")
LANG_RE = re.compile(r"\A[a-z]{2,3}(_[A-Z]{2})?\Z")
REQUIRED_LANGS = ("zh_CN", "en_US")
SCREENSHOT_LANGS = REQUIRED_LANGS
VERSION_RE = re.compile(r"\A(?:[0-9]+:)?[0-9][A-Za-z0-9.+~-]*\Z")
LICENSE_RE = re.compile(r"\A[A-Za-z0-9.+():\- ]{1,200}\Z")
LAUNCHABLE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.desktop\Z")
DEFAULT_PATH_RE = re.compile(r"\A/etc/default/[a-z0-9][a-z0-9.+-]*\Z")
HOST_RE = re.compile(r"\A[a-z0-9]([a-z0-9.-]*[a-z0-9])?\Z")
FINGERPRINT_RE = re.compile(r"\A[0-9A-Fa-f]{40}\Z")
DOTTED_RE = re.compile(r"\A[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*\Z")
SUITE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")
PACKAGE_RE = re.compile(r"\A[a-z0-9][a-z0-9+.-]+\Z")
MAX_L10N_LANGS = 8
MAX_L10N_PACKAGES = 4
ARCH_RE = re.compile(r"\A[a-z0-9][a-z0-9-]*\Z")

ICON_EXTS = (".svg", ".png", ".jpg", ".jpeg", ".webp")
SCREENSHOT_EXTS = (".png", ".jpg", ".jpeg", ".webp")   # no SVG: build reads the pixel size
SOURCES = ("system", "official", "personal")
KINDS = ("milkyway", "deb")
DEBCONF_TYPES = ("boolean", "string", "select")
MAX_YAML_BYTES = 1 << 20
MAX_ASSET_BYTES = 10 << 20
MAX_DEFAULT_FILE_BYTES = 4096
MAX_REGEX_LEN = 500
# Heuristic: a repeat right before a group's closing paren and the group repeated again, e.g. (a+)+ (.*)* (a+)*.
NESTED_QUANTIFIER_RE = re.compile(r"[+*}]\??\)[+*{]")
MAX_TEXT_LEN = 4000

# track type -> (required keys, optional keys) besides the common ones
TRACK_FIELDS = {
    "apt": (("url", "suite", "component", "keys", "fingerprints"), ()),
    "json": (("url", "version_path", "url_path"), ("strip_js",)),
    "head": (("url",), ()),
    "page": (("url", "link_regex"), ()),
    "redirect": (("url", "version_regex"), ()),
}


class ContentError(Exception):
    def __init__(self, errors):
        self.errors = tuple(errors)
        super().__init__("\n".join(self.errors))


@dataclass(frozen=True)
class Category:
    id: str
    name: dict


@dataclass(frozen=True)
class Featured:
    id: str
    title: dict
    subtitle: dict | None
    banner: bool
    apps: tuple


@dataclass(frozen=True)
class Vendor:
    id: str
    package: str
    arch: str
    fetch: str
    allowed_hosts: tuple
    track: dict
    key_files: tuple   # absolute paths inside vendors/keys/


@dataclass(frozen=True)
class App:
    id: str
    dir: str
    name: dict
    summary: dict
    description: dict       # lang -> tuple of paragraphs
    categories: tuple
    source: str
    license: str
    proprietary: bool
    developer: dict
    homepage: str | None
    maintainer: dict | None
    launchable: str | None
    icon: str               # path relative to dir
    screenshots: dict       # lang -> tuple of paths relative to dir
    kind: str
    package: str
    vendor: str | None
    install_profile: dict | None
    release_notes: dict     # version -> {lang: tuple of lines}
    l10n: dict | None = None    # lang -> tuple of language package names (kind milkyway only)
    shots: dict | None = None   # validated shots.yaml (schema, window, scenes), None without the file
    promo: tuple = ()           # vendor promo images from shots.yaml (reviewed by hand, not built)


@dataclass(frozen=True)
class Content:
    root: str
    categories: tuple
    featured: tuple
    apps: tuple             # sorted by id
    vendors: tuple          # sorted by id

    def app(self, app_id):
        return next((a for a in self.apps if a.id == app_id), None)

    def vendor(self, vendor_id):
        return next((v for v in self.vendors if v.id == vendor_id), None)


@dataclass(frozen=True)
class TrackingView:
    """Plain-dict view of the parts trackers.py and checks.py read (docs/server.md section 5)."""
    root: str
    vendors: dict           # vendor id -> {package, arch, fetch, allowed_hosts, track}
    apps: dict              # app id -> {"package": {kind, name, vendor}}


def tracking_view(content):
    vendors = {}
    for v in content.vendors:
        track = dict(v.track)
        if "keys" in track:
            track["keys"] = ["keys/" + name for name in track["keys"]]
        if "fingerprints" in track:
            track["fingerprints"] = list(track["fingerprints"])
        vendors[v.id] = {"package": v.package, "arch": v.arch, "fetch": v.fetch,
                         "allowed_hosts": list(v.allowed_hosts), "track": track}
    apps = {a.id: {"package": {"kind": a.kind, "name": a.package, "vendor": a.vendor}}
            for a in content.apps}
    return TrackingView(root=content.root, vendors=vendors, apps=apps)


class _Check:
    """Error collector with the small validators every section shares."""

    def __init__(self):
        self.errors = []

    def fail(self, path, message):
        self.errors.append("%s: %s" % (path, message))

    def mapping(self, path, value, required=(), optional=()):
        if not isinstance(value, dict):
            self.fail(path, "must be a mapping")
            return False
        ok = True
        for key in value:
            if key not in required and key not in optional:
                self.fail(path, "unknown key %r" % (key,))
                ok = False
        for key in required:
            if key not in value:
                self.fail(path, "missing key %r" % key)
                ok = False
        return ok

    def string(self, path, value, regex=None, max_len=MAX_TEXT_LEN):
        if not isinstance(value, str) or not value.strip() or len(value) > max_len:
            self.fail(path, "must be a non-empty string of at most %d characters" % max_len)
            return None
        if any(ord(c) < 0x20 and c not in "\n\t" for c in value):
            self.fail(path, "contains control characters")
            return None
        if regex is not None and not regex.match(value):
            self.fail(path, "bad format: %r" % value)
            return None
        return value

    def boolean(self, path, value):
        if not isinstance(value, bool):
            self.fail(path, "must be true or false")
            return None
        return value

    def sequence(self, path, value, min_len=1, max_len=200):
        if not isinstance(value, list) or not min_len <= len(value) <= max_len:
            self.fail(path, "must be a list of %d..%d items" % (min_len, max_len))
            return None
        return value

    def text(self, path, value, max_len=300):
        """Localized single-line string: zh_CN and en_US required, others optional."""
        if not self._langs(path, value):
            return None
        out = {}
        for lang, line in value.items():
            single = self.string("%s.%s" % (path, lang), line, max_len=max_len)
            if single is not None and "\n" in single:
                self.fail("%s.%s" % (path, lang), "must be a single line")
                single = None
            out[lang] = single
        return out if None not in out.values() else None

    def paragraphs(self, path, value, max_len=MAX_TEXT_LEN):
        """Localized list of paragraphs."""
        if not self._langs(path, value):
            return None
        out = {}
        for lang, items in value.items():
            where = "%s.%s" % (path, lang)
            items = self.sequence(where, items, 1, 50)
            lines = [self.string("%s[%d]" % (where, i), s, max_len=max_len)
                     for i, s in enumerate(items or [])]
            out[lang] = tuple(lines) if items and None not in lines else None
        return out if None not in out.values() else None

    def _langs(self, path, value):
        if not isinstance(value, dict):
            self.fail(path, "must be a mapping of language to text")
            return False
        ok = True
        for lang in REQUIRED_LANGS:
            if lang not in value:
                self.fail(path, "missing %s" % lang)
                ok = False
        for lang in value:
            if not isinstance(lang, str) or not LANG_RE.match(lang):
                self.fail(path, "bad language key %r" % (lang,))
                ok = False
        return ok

    def url(self, path, value, allow_insecure):
        schemes = ("https", "http") if allow_insecure else ("https",)
        text = self.string(path, value, max_len=2000)
        if text is None:
            return None
        parts = urlsplit(text)
        if parts.scheme not in schemes or not parts.hostname or parts.username or parts.password:
            self.fail(path, "must be an %s URL without credentials" % "/".join(schemes))
            return None
        return text


def _load_yaml(chk, root, rel):
    path = os.path.join(root, rel)
    try:
        st = os.lstat(path)
    except OSError:
        chk.fail(rel, "file not found")
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_YAML_BYTES:
        chk.fail(rel, "must be a regular file of at most 1 MiB")
        return None
    try:
        with open(path, "rb") as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as e:
        chk.fail(rel, "invalid YAML: %s" % str(e).replace("\n", " "))
    except UnicodeDecodeError:
        chk.fail(rel, "not valid UTF-8")
    return None


def _inside(root, rel):
    """Absolute real path of root/rel if it stays strictly inside root, else None."""
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel or "\0" in rel:
        return None
    base = os.path.realpath(root)
    real = os.path.realpath(os.path.join(base, rel))
    if real == base or os.path.commonpath([real, base]) != base:
        return None
    return real


def _asset(chk, path, base, rel, exts):
    real = _inside(base, rel)
    if real is None:
        chk.fail(path, "path must stay inside the app directory: %r" % (rel,))
        return None
    if os.path.splitext(rel)[1].lower() not in exts:
        chk.fail(path, "file type must be one of %s" % ", ".join(exts))
        return None
    if not os.path.isfile(real) or os.path.getsize(real) > MAX_ASSET_BYTES:
        chk.fail(path, "must be an existing file of at most 10 MiB: %r" % rel)
        return None
    return rel


def _duplicates(chk, path, ids):
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        chk.fail(path, "duplicate id %r" % dup)


# categories.yaml and featured.yaml

def _categories(chk, root):
    data = _load_yaml(chk, root, "categories.yaml")
    if data is None or chk.sequence("categories.yaml", data) is None:
        return ()
    out = []
    for i, item in enumerate(data):
        where = "categories.yaml[%d]" % i
        if not chk.mapping(where, item, ("id", "name")):
            continue
        cid = chk.string(where + ".id", item["id"], ID_RE, 64)
        name = chk.text(where + ".name", item["name"], 100)
        if cid and name:
            out.append(Category(cid, name))
    _duplicates(chk, "categories.yaml", [c.id for c in out])
    return tuple(out)


def _featured(chk, root, app_ids):
    data = _load_yaml(chk, root, "featured.yaml")
    if data is None or chk.sequence("featured.yaml", data, 0) is None:
        return ()
    out = []
    for i, item in enumerate(data):
        where = "featured.yaml[%d]" % i
        if chk.mapping(where, item, ("id", "title", "apps"), ("subtitle", "banner")):
            entry = _featured_entry(chk, where, item, app_ids)
            if entry:
                out.append(entry)
    _duplicates(chk, "featured.yaml", [f.id for f in out])
    return tuple(out)


def _featured_entry(chk, where, item, app_ids):
    fid = chk.string(where + ".id", item["id"], ID_RE, 64)
    title = chk.text(where + ".title", item["title"], 100)
    subtitle = chk.text(where + ".subtitle", item["subtitle"], 200) if "subtitle" in item else None
    banner = chk.boolean(where + ".banner", item["banner"]) if "banner" in item else False
    apps = chk.sequence(where + ".apps", item["apps"], 1, 200)
    listed = []
    for j, app_id in enumerate(apps or []):
        if app_id not in app_ids:
            chk.fail("%s.apps[%d]" % (where, j), "unknown app %r" % (app_id,))
        elif app_id in listed:
            chk.fail("%s.apps[%d]" % (where, j), "duplicate app %r" % app_id)
        else:
            listed.append(app_id)
    if fid and title and apps and banner is not None and len(listed) == len(apps):
        return Featured(fid, title, subtitle, banner, tuple(listed))
    return None


# apps/<id>/app.yaml

def _debconf_line(chk, path, line):
    if chk.string(path, line, max_len=500) is None:
        return None
    if any(ord(c) < 0x20 for c in line):
        chk.fail(path, "must be a single line without control characters")
        return None
    fields = line.split(" ")
    if len(fields) < 3 or not all(fields[:3]) or fields[2] not in DEBCONF_TYPES:
        chk.fail(path, "needs '<owner> <question> <type> <value>' with type %s" % "/".join(DEBCONF_TYPES))
        return None
    return line


def _default_file(chk, path, item):
    if not chk.mapping(path, item, ("path", "content")):
        return None
    where = chk.string(path + ".path", item["path"], DEFAULT_PATH_RE, 200)
    content = item["content"]
    if not isinstance(content, str) or "\0" in content or len(content.encode("utf-8")) > MAX_DEFAULT_FILE_BYTES:
        chk.fail(path + ".content", "must be text of at most 4 KiB without NUL")
        return None
    return {"path": where, "content": content} if where else None


def _install_profile(chk, path, value):
    if value is None:
        return None
    if not chk.mapping(path, value, (), ("debconf", "default_files")):
        return None
    lines = chk.sequence(path + ".debconf", value.get("debconf", []), 0, 50) or []
    debconf = [_debconf_line(chk, "%s.debconf[%d]" % (path, i), s) for i, s in enumerate(lines)]
    items = chk.sequence(path + ".default_files", value.get("default_files", []), 0, 10) or []
    files = [_default_file(chk, "%s.default_files[%d]" % (path, i), s) for i, s in enumerate(items)]
    paths = [f["path"] for f in files if f]
    if len(paths) != len(set(paths)):
        chk.fail(path + ".default_files", "duplicate path")
    if None in debconf or None in files:
        return None
    return {"debconf": tuple(debconf), "default_files": tuple(files)}


def _release_notes(chk, path, value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        chk.fail(path, "must be a mapping of version to notes")
        return {}
    out = {}
    for version, notes in value.items():
        where = "%s[%s]" % (path, version)
        if not isinstance(version, str) or not VERSION_RE.match(version):
            chk.fail(where, "version must be a quoted string like \"1.2.3-1\"")
            continue
        parsed = chk.paragraphs(where, notes, 500)
        if parsed:
            out[version] = parsed
    return out


def _screenshots(chk, path, base, value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        chk.fail(path, "must be a mapping of language to a list of images")
        return {}
    out = {}
    for lang, items in value.items():
        where = "%s.%s" % (path, lang)
        if lang not in SCREENSHOT_LANGS:
            chk.fail(path, "language must be zh_CN or en_US, got %r" % (lang,))
            continue
        rels = []
        for i, item in enumerate(chk.sequence(where, items, 0, 20) or []):
            if chk.mapping("%s[%d]" % (where, i), item, ("image",)):
                rels.append(_asset(chk, "%s[%d].image" % (where, i), base, item["image"], SCREENSHOT_EXTS))
        out[lang] = tuple(r for r in rels if r)
    return out


def _maintainer(chk, path, value):
    if value is None or not chk.mapping(path, value, ("name", "contact")):
        return None
    name = chk.string(path + ".name", value["name"], max_len=100)
    contact = chk.string(path + ".contact", value["contact"], max_len=200)
    return {"name": name, "contact": contact} if name and contact else None


def _launchable(chk, path, value):
    if value is None:
        return None
    text = chk.string(path, value, LAUNCHABLE_RE, 220)
    if text and ".." in text:
        chk.fail(path, "must not contain '..'")
        return None
    return text


def _l10n(chk, path, value, kind):
    """Language packages per locale key: {lang: (package, ...)} or None when absent/invalid."""
    if value is None:
        return None
    if kind != "milkyway":
        chk.fail(path, "only allowed for kind milkyway")
        return None
    if not isinstance(value, dict) or not 1 <= len(value) <= MAX_L10N_LANGS:
        chk.fail(path, "must be a mapping of 1..%d languages to package lists" % MAX_L10N_LANGS)
        return None
    out = {}
    for lang, names in value.items():
        where = "%s.%s" % (path, lang)
        if not isinstance(lang, str) or not LANG_RE.match(lang):
            chk.fail(path, "bad language key %r" % (lang,))
            continue
        items = chk.sequence(where, names, 1, MAX_L10N_PACKAGES)
        parsed = [chk.string("%s[%d]" % (where, i), n, PACKAGE_RE, 128) for i, n in enumerate(items or [])]
        if items and None not in parsed:
            out[lang] = tuple(parsed)
    return out if len(out) == len(value) else None


def _app_fields(chk, where, data, app_dir):
    """Validate everything in app.yaml that does not depend on other files."""
    cats = chk.sequence(where + ".categories", data["categories"], 1, 10) or []
    return {
        "name": chk.text(where + ".name", data["name"], 100),
        "summary": chk.text(where + ".summary", data["summary"], 300),
        "description": chk.paragraphs(where + ".description", data["description"]),
        "developer": chk.text(where + ".developer", data["developer"], 100),
        "categories": tuple(chk.string("%s.categories[%d]" % (where, i), c, ID_RE, 64)
                            for i, c in enumerate(cats)),
        "license": chk.string(where + ".license", data["license"], LICENSE_RE, 200),
        "proprietary": chk.boolean(where + ".proprietary", data.get("proprietary", False)),
        "homepage": (chk.url(where + ".homepage", data["homepage"], True)
                     if data.get("homepage") is not None else None),
        "maintainer": _maintainer(chk, where + ".maintainer", data.get("maintainer")),
        "launchable": _launchable(chk, where + ".launchable", data.get("launchable")),
        "icon": _asset(chk, where + ".icon", app_dir, data["icon"], ICON_EXTS),
        "screenshots": _screenshots(chk, where + ".screenshots", app_dir, data.get("screenshots")),
        "install_profile": _install_profile(chk, where + ".install_profile", data.get("install_profile")),
        "release_notes": _release_notes(chk, where + ".release_notes", data.get("release_notes")),
    }


def _app_package(chk, where, data, vendors):
    pkg = data["package"]
    if not chk.mapping(where + ".package", pkg, ("kind", "name"), ("vendor",)):
        return None
    kind, source = pkg["kind"], data["source"]
    name = chk.string(where + ".package.name", pkg["name"], ID_RE, 128)
    if kind not in KINDS:
        chk.fail(where + ".package.kind", "must be one of %s" % ", ".join(KINDS))
        return None
    if source not in SOURCES:
        chk.fail(where + ".source", "must be one of %s" % ", ".join(SOURCES))
        return None
    if (kind == "milkyway") != (source == "system"):
        chk.fail(where + ".source", "kind milkyway needs source system; kind deb needs official or personal")
        return None
    vendor = pkg.get("vendor")
    if kind == "milkyway":
        if vendor is not None:
            chk.fail(where + ".package.vendor", "only allowed for kind deb")
        return (kind, name, None) if name else None
    if not isinstance(vendor, str) or vendor not in vendors:
        chk.fail(where + ".package.vendor", "unknown vendor %r" % (vendor,))
        return None
    if vendors[vendor].package != name:
        chk.fail(where + ".package.name", "must equal the vendor package %r" % vendors[vendor].package)
        return None
    return (kind, name, vendor)


def _shots(chk, root, app_id, app_dir, demo_names):
    """(shots, promo) of an app; (None, ()) without apps/<id>/shots.yaml."""
    rel = "apps/%s/shots.yaml" % app_id
    if not os.path.lexists(os.path.join(root, rel)):
        return None, ()
    data = _load_yaml(chk, root, rel)
    if data is None:
        return None, ()
    before = len(chk.errors)
    parsed = shots.validate(chk, rel, data, demo_names)
    promo = shots.promo(chk, rel + ".promo", data.get("promo", []), lambda p, r: _promo_image(chk, p, app_dir, r))
    if len(chk.errors) != before:
        return None, ()
    return parsed, promo or ()


def _promo_image(chk, path, base, rel):
    rel = _asset(chk, path, base, rel, (".png",))
    if rel is None:
        return None
    with open(_inside(base, rel), "rb") as f:
        if f.read(8) != b"\x89PNG\r\n\x1a\n":
            chk.fail(path, "not a PNG file: %r" % rel)
            return None
    return rel


def _app(chk, root, app_id, vendors, category_ids, demo_names=frozenset()):
    rel = "apps/%s/app.yaml" % app_id
    app_dir = os.path.join(root, "apps", app_id)
    data = _load_yaml(chk, root, rel)
    required = ("name", "summary", "description", "categories", "source", "license",
                "developer", "icon", "package")
    optional = ("proprietary", "homepage", "maintainer", "launchable", "screenshots",
                "install_profile", "release_notes", "l10n")
    before = len(chk.errors)
    # Unknown keys are reported but do not stop the remaining checks; missing ones do.
    if data is None or (not chk.mapping(rel, data, required, optional)
                        and (not isinstance(data, dict) or any(k not in data for k in required))):
        return None
    fields = _app_fields(chk, rel, data, app_dir)
    package = _app_package(chk, rel, data, vendors)
    for i, cat in enumerate(fields["categories"]):
        if cat and cat not in category_ids:
            chk.fail("%s.categories[%d]" % (rel, i), "unknown category %r" % cat)
    if data["source"] == "personal" and not fields["maintainer"]:
        chk.fail(rel + ".maintainer", "required for source personal")
    l10n = _l10n(chk, rel + ".l10n", data.get("l10n"), package[0] if package else None)
    app_shots, promo = _shots(chk, root, app_id, app_dir, demo_names)
    if len(chk.errors) != before or package is None:
        return None
    return App(id=app_id, dir=app_dir, source=data["source"], kind=package[0],
               package=package[1], vendor=package[2], l10n=l10n, shots=app_shots, promo=promo, **fields)


# vendors/<id>.yaml

def _regex(chk, path, value, required_groups, optional_groups=()):
    text = chk.string(path, value, max_len=MAX_REGEX_LEN)
    if text is None:
        return None
    if NESTED_QUANTIFIER_RE.search(text):
        chk.fail(path, "nested quantifiers (catastrophic backtracking) are not allowed")
        return None
    try:
        groups = re.compile(text).groupindex
    except re.error as e:
        chk.fail(path, "invalid regular expression: %s" % e)
        return None
    for name in required_groups:
        if name not in groups:
            chk.fail(path, "needs a named group (?P<%s>...)" % name)
            return None
    unknown = set(groups) - set(required_groups) - set(optional_groups)
    if unknown:
        chk.fail(path, "unknown named group %s" % ", ".join(sorted(unknown)))
        return None
    return text


def _track_keys(chk, path, vendors_dir, value):
    keys_dir = os.path.join(vendors_dir, "keys")
    out = []
    for i, rel in enumerate(chk.sequence(path, value, 1, 10) or []):
        where = "%s[%d]" % (path, i)
        real = _inside(keys_dir, rel[5:]) if isinstance(rel, str) and rel.startswith("keys/") else None
        if real is None or not os.path.isfile(real):
            chk.fail(where, "must be an existing file below vendors/keys/ (written keys/<name>.asc)")
        else:
            out.append(real)
    return tuple(out)


def _track(chk, where, vendors_dir, track, fetch, hosts, allow_insecure):
    if not isinstance(track, dict) or track.get("type") not in TRACK_FIELDS:
        chk.fail(where, "type must be one of %s" % ", ".join(TRACK_FIELDS))
        return None, ()
    required, optional = TRACK_FIELDS[track["type"]]
    if not chk.mapping(where, track, ("type",) + required, optional):
        return None, ()
    kind = track["type"]
    out = {"type": kind}
    url = chk.url(where + ".url", track["url"], allow_insecure)
    if url and urlsplit(url).hostname not in hosts:
        chk.fail(where + ".url", "host is not in allowed_hosts")
    if url and not allow_insecure and _port(url) not in (None, 443):
        chk.fail(where + ".url", "port must be 443")
    out["url"] = url
    key_files = ()
    if kind == "apt":
        key_files = _track_keys(chk, where + ".keys", vendors_dir, track["keys"])
        out["keys"] = tuple(os.path.basename(k) for k in key_files)
        out["suite"] = chk.string(where + ".suite", track["suite"], SUITE_RE, 100)
        out["component"] = chk.string(where + ".component", track["component"], SUITE_RE, 100)
        out["fingerprints"] = _fingerprints(chk, where + ".fingerprints", track["fingerprints"])
    elif kind == "json":
        out["version_path"] = chk.string(where + ".version_path", track["version_path"], DOTTED_RE, 200)
        out["url_path"] = chk.string(where + ".url_path", track["url_path"], DOTTED_RE, 200)
        out["strip_js"] = chk.boolean(where + ".strip_js", track.get("strip_js", False))
    elif kind == "page":
        out["link_regex"] = _regex(chk, where + ".link_regex", track["link_regex"], ("url",), ("version",))
        if fetch == "manual" and out["link_regex"] and "(?P<version>" not in out["link_regex"]:
            chk.fail(where + ".link_regex", "fetch manual needs a (?P<version>...) group")
    elif kind == "redirect":
        out["version_regex"] = _regex(chk, where + ".version_regex", track["version_regex"], ("version",))
    if kind == "head" and fetch == "manual":
        chk.fail(where, "fetch manual cannot be used with type head (no version without the file)")
    return (out if None not in out.values() else None), key_files


def _fingerprints(chk, path, value):
    items = [chk.string("%s[%d]" % (path, i), s, FINGERPRINT_RE, 40)
             for i, s in enumerate(chk.sequence(path, value, 1, 10) or [])]
    return tuple(s.upper() for s in items if s) if None not in items else None


def _port(url):
    try:
        return urlsplit(url).port
    except ValueError:
        return -1


def _bad_host(host):
    """Why a host cannot be an allowed vendor host (IP literal, local or single-label name), or None."""
    try:
        ipaddress.ip_address(host)
        return "IP literals are not allowed"
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) < 2 or labels[-1] == "localhost":
        return "must be a fully qualified public name (not localhost or a single label)"
    if labels[-1].isdigit() or labels[-1].startswith("0x"):
        return "IP literals are not allowed"
    return None


def _hosts(chk, path, value, allow_insecure=False):
    items = [chk.string("%s[%d]" % (path, i), h, HOST_RE, 253)
             for i, h in enumerate(chk.sequence(path, value, 1, 20) or [])]
    if None in items:
        return ()
    bad = [(i, _bad_host(h)) for i, h in enumerate(items) if not allow_insecure and _bad_host(h)]
    for i, reason in bad:
        chk.fail("%s[%d]" % (path, i), reason)
    return () if bad else tuple(items)


def _vendor(chk, root, vendor_id, allow_insecure):
    rel = "vendors/%s.yaml" % vendor_id
    data = _load_yaml(chk, root, rel)
    if data is None or not chk.mapping(rel, data, ("package", "arch", "fetch", "allowed_hosts", "track")):
        return None
    before = len(chk.errors)
    package = chk.string(rel + ".package", data["package"], ID_RE, 128)
    arch = chk.string(rel + ".arch", data["arch"], ARCH_RE, 32)
    fetch = data["fetch"]
    if fetch not in ("auto", "manual"):
        chk.fail(rel + ".fetch", "must be auto or manual")
    hosts = _hosts(chk, rel + ".allowed_hosts", data["allowed_hosts"], allow_insecure)
    track, key_files = _track(chk, rel + ".track", os.path.join(root, "vendors"),
                              data["track"], fetch, hosts, allow_insecure)
    if len(chk.errors) != before:
        return None
    return Vendor(vendor_id, package, arch, fetch, hosts, track, key_files)


def _vendors(chk, root, allow_insecure):
    directory = os.path.join(root, "vendors")
    out = {}
    for name in sorted(os.listdir(directory)) if os.path.isdir(directory) else []:
        if name == "keys":
            continue
        vendor_id = name[:-5]
        if not name.endswith(".yaml") or not ID_RE.match(vendor_id):
            chk.fail("vendors/%s" % name, "must be <vendor-id>.yaml")
            continue
        vendor = _vendor(chk, root, vendor_id, allow_insecure)
        if vendor:
            out[vendor_id] = vendor
    return out


def _app_ids(chk, root):
    directory = os.path.join(root, "apps")
    if not os.path.isdir(directory):
        chk.fail("apps", "directory not found")
        return []
    out = []
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if ID_RE.match(name) and os.path.isdir(path) and not os.path.islink(path):
            out.append(name)
        else:
            chk.fail("apps/%s" % name, "must be a real directory named like an id")
    return out


def load(root, allow_insecure=False):
    """Validate the whole tree and return a Content, or raise ContentError."""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise ContentError(["%s: not a directory" % root])
    chk = _Check()
    vendors = _vendors(chk, root, allow_insecure)
    app_ids = _app_ids(chk, root)
    categories = _categories(chk, root)
    category_ids = {c.id for c in categories}
    demo_names = demo.scan(chk, root)
    apps = [_app(chk, root, i, vendors, category_ids, demo_names) for i in app_ids]
    featured = _featured(chk, root, set(app_ids))
    if chk.errors:
        raise ContentError(chk.errors)
    return Content(root=root, categories=categories, featured=featured, apps=tuple(apps),
                   vendors=tuple(vendors[k] for k in sorted(vendors)))


def content_digest(root):
    """Hash over the sorted (path, sha256) of every relevant file; symlinks count by target.

    shots.yaml, promo/ and demo/ are left out: they never change the built catalog.
    """
    root = os.path.abspath(root)
    entries = []
    for top in ("categories.yaml", "featured.yaml", "apps", "vendors"):
        start = os.path.join(root, top)
        if os.path.isfile(start) or os.path.islink(start):
            entries.append(_digest_entry(root, start))
            continue
        for base, _dirs, files in os.walk(start):
            entries.extend(_digest_entry(root, os.path.join(base, n)) for n in files
                           if not _screenshot_only(root, os.path.join(base, n)))
    h = hashlib.sha256()
    for line in sorted(entries):
        h.update(line.encode("utf-8", "surrogateescape") + b"\n")
    return h.hexdigest()


def _screenshot_only(root, path):
    """apps/<id>/shots.yaml and apps/<id>/promo/** do not feed the catalog (docs/server.md section 3)."""
    parts = os.path.relpath(path, root).split(os.sep)
    return len(parts) >= 3 and parts[0] == "apps" and (parts[2] == "promo" or parts == [*parts[:2], "shots.yaml"])


def _digest_entry(root, path):
    rel = os.path.relpath(path, root)
    if os.path.islink(path):
        return "%s -> %s" % (rel, os.readlink(path))
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "%s %s" % (rel, h.hexdigest())
