# SPDX-License-Identifier: GPL-3.0-or-later
"""What an AI task delivered: reading it safely and validating it (contract ai-intake 10.5).

The runner leaves the whitelisted files in builds.out/<ai task>/ai/. They are untrusted data written by a model
that read the internet: every file is read without following links, size-limited, parsed with yaml.safe_load
(and no anchors or aliases), and then tried out as a content tree in a temporary directory with the same
`content.load` the real repository goes through, with the task's extra hosts allowed. The server owns some
fields (icon, maintainer, description source, recipe_version, derived_from, the tracking file's app and build
keys): whatever the agent wrote there is replaced.
"""

import dataclasses
import os
import re
import shutil
import struct
import tempfile
import zlib

import yaml

from . import buildcfg, build_collect, builds, content, content_builds, debinfo, upstream_fetch, upstream_tags, upstreams
from .intake_materials import clean_block

DELIVERY_DIR = "delivery"
TEXT_FILES = ("recipe.yaml", "app.yaml", "upstream.yaml", "NOTES.md")
MAX_TEXT = build_collect.AI_MAX_TEXT_BYTES
MAX_YAML_EVENTS = 50_000
MAINTAINER = {"name": "agino app store", "contact": "store@aginoos.com"}
APP_DROP = ("icon", "screenshots", "maintainer", "description_source", "description_confirmed")
SETUID_RE = re.compile(r"\bchmod\b[^;&|\n]*(?:[ugoa]*[+=][rwxXt]*s|\b[2467][0-7]{3}\b)")
AGINO_SUFFIX_RE = re.compile(r"\+agino[0-9]+\Z")


class DeliveryError(Exception):
    """The delivery is not acceptable; `messages` are given back to the agent."""

    def __init__(self, messages):
        self.messages = [debinfo.clean_text(m).strip()[:600] for m in ([messages] if isinstance(messages, str) else messages)]
        super().__init__("; ".join(self.messages))


@dataclasses.dataclass(frozen=True)
class Checked:
    app_id: str
    recipe_data: dict
    app_data: dict | None
    upstream_data: dict | None
    notes: str
    recipe: content_builds.Recipe
    version: str
    tag: str | None
    commit: str | None
    repo: str | None


# Reading

def _read(path, limit):
    try:
        if os.path.islink(os.path.dirname(path)):
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        if not os.path.isfile(path) or os.fstat(f.fileno()).st_size > limit:
            return None
        return f.read(limit)


def read_delivery(cfg, st, intake_id, ai_record):
    """{name: bytes} of the delivered text files (and the deb under 'deb', when there is one), and a copy of the
    text files in state/intake/<id>/delivery/ for the review and for adoption."""
    files = {}
    directory = os.path.join(builds.out_dir(cfg, ai_record["id"]), build_collect.AI_DIR)
    for name in ai_record.get("ai_files") or []:
        if name in TEXT_FILES:
            data = _read(os.path.join(directory, name), MAX_TEXT)
            if data is not None:
                files[name] = data
        elif name.endswith(".deb"):
            files["deb"] = name
    target = os.path.join(st.intake_dir(intake_id, create=True), DELIVERY_DIR)
    shutil.rmtree(target, ignore_errors=True)
    os.makedirs(target, mode=0o750)
    for name, data in files.items():
        if name != "deb":
            with open(os.path.join(target, name), "wb") as f:
                f.write(data)
    return files


def load_snapshot(st, intake_id):
    """The text files saved by read_delivery."""
    directory = os.path.join(st.intake_dir(intake_id), DELIVERY_DIR)
    out = {}
    for name in TEXT_FILES:
        data = _read(os.path.join(directory, name), MAX_TEXT)
        if data is not None:
            out[name] = data
    return out


# Parsing

def safe_yaml(name, data):
    """The mapping in `data`; DeliveryError for invalid UTF-8/YAML, anchors and aliases, or a non-mapping."""
    try:
        text = data.decode("utf-8")
        count = 0
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            count += 1
            if count > MAX_YAML_EVENTS:
                raise DeliveryError("%s: too complex" % name)
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise DeliveryError("%s: YAML anchors and aliases are not allowed" % name)
        loaded = yaml.safe_load(text)
    except UnicodeDecodeError as e:
        raise DeliveryError("%s: not valid UTF-8" % name) from e
    except yaml.YAMLError as e:
        raise DeliveryError("%s: invalid YAML: %s" % (name, str(e).replace("\n", " ")[:300])) from e
    if not isinstance(loaded, dict):
        raise DeliveryError("%s: must be a mapping" % name)
    return loaded


def placeholder_icon(size=64):
    """A plain grey PNG used as the icon until a person supplies one."""
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x88\x88\x88" * size for _ in range(size))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


# Normalising: the fields the server owns

def normalise_recipe(recipe, previous, record=None):
    """The agent's recipe with the fields the server owns set (`previous` is the old Recipe or None): the version
    fields and `requirements`, which are the administrator's words from the submit form (an import) or the ones the
    recipe already had (a repair), never what the agent wrote."""
    out = {k: v for k, v in recipe.items() if k not in ("recipe_version", "derived_from", "requirements")}
    # An import: the form's text. A repair: the text of the task (the recipe's requirements plus a new one), which
    # for an automatic repair is just the recipe's own.
    wanted = ((record or {}).get("hint") or (previous.requirements if previous is not None else "")).strip()
    if wanted:
        out["requirements"] = wanted
    if previous is None:
        out["recipe_version"] = 1
    else:
        before = previous.recipe_version or 1
        out["recipe_version"], out["derived_from"] = before + 1, before
    return out


def normalise_app(app):
    out = {k: v for k, v in app.items() if k not in APP_DROP}
    out.update(icon="icon.png", maintainer=dict(MAINTAINER), description_source="ai")
    return out


def normalise_upstream(upstream, app_id):
    out = dict(upstream)
    out.update(app=app_id, build="apps/%s/build.yaml" % app_id)
    out["auto_task"] = False
    return out


def app_id_of(app, upstream):
    """The store id: upstream.yaml `app`, else the package name."""
    if upstream is not None and isinstance(upstream.get("app"), str):
        return upstream["app"]
    package = app.get("package") if isinstance(app, dict) else None
    return package.get("name") if isinstance(package, dict) and isinstance(package.get("name"), str) else None


# The trial content tree

def _policy(cfg, record):
    hosts = tuple(dict.fromkeys(tuple(cfg.upstream_allowed_hosts) + tuple(record.get("allow_hosts") or ())))
    return buildcfg.Policy(hosts, cfg.builds_limits)


def _write_yaml(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, default_flow_style=False, width=1_000_000)


def _build_trial(root, loaded, kind, app_id, recipe, app, upstream):
    shutil.copyfile(os.path.join(loaded.root, "categories.yaml"), os.path.join(root, "categories.yaml"))
    with open(os.path.join(root, "featured.yaml"), "w", encoding="utf-8") as f:
        f.write("[]\n")
    app_dir = os.path.join(root, "apps", app_id)
    if kind == "repair":
        shutil.copytree(os.path.join(loaded.root, "apps", app_id), app_dir, symlinks=True)
        source = os.path.join(loaded.root, "upstreams", app_id + ".yaml")
        if os.path.isfile(source) and not os.path.islink(source):
            os.makedirs(os.path.join(root, "upstreams"), exist_ok=True)
            shutil.copyfile(source, os.path.join(root, "upstreams", app_id + ".yaml"))
    else:
        os.makedirs(app_dir)
        _write_yaml(os.path.join(app_dir, "app.yaml"), app)
        with open(os.path.join(app_dir, "icon.png"), "wb") as f:
            f.write(placeholder_icon())
        if upstream is not None:
            _write_yaml(os.path.join(root, "upstreams", app_id + ".yaml"), upstream)
    _write_yaml(os.path.join(app_dir, "build.yaml"), recipe)


def _hard_rules(recipe):
    """Rules beyond the schema for what an agent delivers; messages for the agent."""
    errors = []
    if not recipe.get("sources"):
        errors.append("recipe.yaml: the recipe must use `sources` (recipe v2), not `source`")
    if not recipe.get("test"):
        errors.append("recipe.yaml: `test` needs at least one command that starts the application under xvfb")
    steps = recipe.get("steps")
    for i, step in enumerate(steps if isinstance(steps, list) else []):
        if isinstance(step, str) and SETUID_RE.search(step):
            errors.append("recipe.yaml: steps[%d] sets a setuid/setgid bit, which is not allowed" % i)
    return errors


def _trial_load(cfg, record, loaded, kind, app_id, recipe, app, upstream):
    with tempfile.TemporaryDirectory(prefix="agino-intake-") as root:
        try:
            _build_trial(root, loaded, kind, app_id, recipe, app, upstream)
            return content.load(root, allow_insecure=cfg.allow_insecure, policy=_policy(cfg, record))
        except content.ContentError as e:
            raise DeliveryError(list(e.errors)[:30]) from e
        except OSError as e:
            raise DeliveryError("cannot set up the trial content tree: %s" % e.strerror) from e


# Validation

def _newest_tag(cfg, record, loaded_up, work):
    """(version, tag, commit) of the newest tag the tracking file of a new app accepts."""
    limited = dataclasses.replace(cfg, upstream_allowed_hosts=_policy(cfg, record).allowed_hosts)
    try:
        tags = upstream_fetch.list_tags(limited, loaded_up.repo, work)
        found = upstreams.pick_versions(loaded_up, tags, {})
    except (upstream_fetch.UpstreamError, upstream_tags.TagError, debinfo.DebError) as e:
        raise DeliveryError("upstream.yaml: cannot list the tags of the repository: %s" % e) from e
    newest = upstream_tags.newest(found)
    if newest is None:
        raise DeliveryError("upstream.yaml: no tag of the repository matches track.match (and min_version)")
    tag, commit = found[newest]
    return newest, tag, commit


def _deb_version(cfg, ai_record, name):
    path = os.path.join(builds.out_dir(cfg, ai_record["id"]), build_collect.AI_DIR, name)
    try:
        version = AGINO_SUFFIX_RE.sub("", debinfo.control_fields(path).get("Version") or "")
    except (debinfo.DebError, OSError):
        version = ""
    return version if content_builds.UPSTREAM_VERSION_RE.match(version) else None


def validate(cfg, st, loaded, record, files, ai_record=None, pinned=None):
    """Check a delivery; returns Checked or raises DeliveryError.

    `files` is {name: bytes} (read_delivery / load_snapshot). `pinned` ({version, tag, commit}) is the version
    chosen when the delivery was first checked (adoption does not look again); `ai_record` is only needed to
    read the version of the agent's own deb when there is no tracking file.
    """
    if "recipe.yaml" not in files:
        raise DeliveryError("recipe.yaml is missing")
    kind = record["kind"]
    recipe_in = safe_yaml("recipe.yaml", files["recipe.yaml"])
    errors = _hard_rules(recipe_in)
    if kind == "repair":
        app_id, app, upstream = record["repair_of"]["app"], None, None
        previous = loaded.recipe(app_id)
        if previous is None:
            raise DeliveryError("%s has no recipe to repair" % app_id)
    else:
        if "app.yaml" not in files:
            raise DeliveryError("app.yaml is missing")
        app_in = safe_yaml("app.yaml", files["app.yaml"])
        up_in = safe_yaml("upstream.yaml", files["upstream.yaml"]) if "upstream.yaml" in files else None
        app_id = app_id_of(app_in, up_in)
        if not isinstance(app_id, str) or not content_builds.ID_RE.match(app_id) or len(app_id) > 64:
            raise DeliveryError("the store id (upstream.yaml `app`, else app.yaml package.name) is not valid")
        if loaded.app(app_id) is not None or os.path.lexists(os.path.join(loaded.root, "apps", app_id)):
            raise DeliveryError("the store id %r is already used by another application; choose another" % app_id)
        previous, app, upstream = None, normalise_app(app_in), (normalise_upstream(up_in, app_id) if up_in else None)
    recipe = normalise_recipe(recipe_in, previous, record)
    try:
        trial = _trial_load(cfg, record, loaded, kind, app_id, recipe, app, upstream)
    except DeliveryError as e:
        raise DeliveryError(errors + e.messages) from e
    if errors:
        raise DeliveryError(errors)
    parsed = trial.recipe(app_id)
    if parsed is None:
        raise DeliveryError("recipe.yaml was not accepted")
    version, tag, commit, repo = _version(cfg, st, record, trial, app_id, ai_record, files, pinned)
    return Checked(app_id, recipe, app, upstream, clean_block(files.get("NOTES.md", b"").decode("utf-8", "replace")),
                   parsed, version, tag, commit, repo)


def _version(cfg, st, record, trial, app_id, ai_record, files, pinned):
    """(version, tag, commit, repo) the verification build uses; repo is the tracked repository or None."""
    up = trial.upstream(app_id)
    repo = up.repo if up is not None else None
    if record["kind"] == "repair":
        of = record["repair_of"]
        entry = (st.upstream_memo(app_id).get("seen") or {}).get(of["version"]) or {}
        return of["version"], entry.get("tag"), entry.get("commit"), repo
    if pinned:
        return pinned["version"], pinned.get("tag"), pinned.get("commit"), repo
    if up is not None:
        with tempfile.TemporaryDirectory(prefix="agino-ls-") as work:
            version, tag, commit = _newest_tag(cfg, record, up, work)
        return version, tag, commit, repo
    version = _deb_version(cfg, ai_record, files["deb"]) if ai_record and files.get("deb") else None
    if version is None:
        raise DeliveryError("the version cannot be determined: deliver upstream.yaml, or a deb whose Version is "
                            "the upstream version")
    return version, None, None, repo

