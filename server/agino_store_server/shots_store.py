# SPDX-License-Identifier: GPL-3.0-or-later
"""The screenshot directory and its meta.json (docs/server.md section 12).

<screenshots>/<app>/meta.json, <lang>/NN-<scene>.png, .prev/<UTC stamp>/<lang>/<file>.
Writers hold a per-app flock (<app>/.lock), never the global state lock. Nothing is destroyed:
replaced and removed images move to .prev. Records are plain dicts that are never changed in
place; every change builds a new list.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile

from . import catalogwriter as cw, pngclean
from .state import StateLocked
from .status_io import ReadError, read_bytes, read_json

SCHEMA = 1
LANGS = ("zh_CN", "en_US")
APP_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,63}\Z")
SCENE_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,40}\Z")
FILE_RE = re.compile(r"\A(\d\d)-([a-z0-9][a-z0-9-]{0,40})\.png\Z")
REVIEWER_RE = re.compile(r"\A[A-Za-z0-9._-]{1,32}\Z")
SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
SOURCES = ("sandbox", "upload", "manual", "vendor")
STATUSES = ("draft", "approved", "rejected")
MAX_BYTES = 3 << 19             # 1.5 MiB, the stored (stripped) file
MIN_HEIGHT = 300
MAX_DIM = 4096
MAX_TEXT = 200
META_LIMIT = 1 << 20
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
FIELDS = ("lang", "file", "scene", "sha256", "size", "width", "height", "source", "status",
          "reviewer", "reviewed", "note", "origin")


class ShotsError(Exception):
    pass


def app_dir(cfg, app):
    return os.path.join(cfg.screenshots, app)


def _int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _text(value):
    return isinstance(value, str) and len(value) <= MAX_TEXT


def _problem(index, record):
    """Why a meta.json image record is unusable, or None."""
    if not isinstance(record, dict):
        return "image %d is not an object" % index
    match = FILE_RE.match(str(record.get("file")))
    checks = (
        (record.get("lang") in LANGS, "lang"), (match is not None, "file"),
        (match is not None and record.get("scene") == match.group(2), "scene"),
        (isinstance(record.get("sha256"), str) and SHA256_RE.match(record["sha256"]), "sha256"),
        (all(_int(record.get(k)) for k in ("size", "width", "height")), "size/width/height"),
        (record.get("source") in SOURCES, "source"), (record.get("status") in STATUSES, "status"),
        (record.get("reviewer") is None or (isinstance(record["reviewer"], str)
                                            and REVIEWER_RE.match(record["reviewer"])), "reviewer"),
        (record.get("reviewed") is None or _text(record["reviewed"]), "reviewed"),
        (_text(record.get("note")), "note"), (_text(record.get("origin")), "origin"))
    bad = [name for ok, name in checks if not ok]
    return "image %d (%s): bad %s" % (index, record.get("file"), ", ".join(bad)) if bad else None


def parse_meta(doc, app):
    """(images as plain dicts with exactly FIELDS, problems) from a parsed meta.json."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or doc.get("app") != app \
            or not isinstance(doc.get("images"), list):
        return [], ["meta.json is not a schema %d document for %s" % (SCHEMA, app)]
    images, problems, seen = [], [], set()
    for i, record in enumerate(doc["images"]):
        why = _problem(i, record)
        if why is None and (record["lang"], record["file"]) in seen:
            why = "image %d: duplicate %s/%s" % (i, record["lang"], record["file"])
        if why:
            problems.append(why)
            continue
        seen.add((record["lang"], record["file"]))
        images.append({k: record[k] for k in FIELDS})
    return images, problems


def read_images(cfg, app):
    """([images], [problems]) for reading (status, catalog); a missing meta.json is simply empty."""
    path = os.path.join(app_dir(cfg, app), "meta.json")
    if not os.path.lexists(path):
        return [], []
    try:
        return parse_meta(read_json(path, META_LIMIT), app)
    except ReadError as e:
        return [], [str(e)]


def sort_key(image):
    return (image["lang"], image["file"])


def approved_images(cfg, app):
    """Approved images of app ordered by (lang, file), plus the problems met while reading."""
    images, problems = read_images(cfg, app)
    return sorted((i for i in images if i["status"] == "approved"), key=sort_key), problems


def active(images, lang):
    return [i for i in images if i["lang"] == lang and i["status"] != "rejected"]


def check_png(cfg, data):
    """(stripped bytes, width, height) for a PNG that may be stored; ShotsError otherwise."""
    try:
        data, width, height = pngclean.clean(data, MAX_DIM)
    except pngclean.PngError as e:
        raise ShotsError("not an acceptable PNG: %s" % e) from e
    if width < cfg.shots_min_width or height < MIN_HEIGHT:
        raise ShotsError("image is %dx%d, at least %dx%d is required" % (width, height, cfg.shots_min_width, MIN_HEIGHT))
    if len(data) > MAX_BYTES:
        raise ShotsError("image is %d bytes after stripping, at most %d are allowed" % (len(data), MAX_BYTES))
    return data, width, height


def verify_stored(cfg, app, image):
    """Re-validate the file on disk behind a meta.json record (before approving it)."""
    path = os.path.join(app_dir(cfg, app), image["lang"], image["file"])
    try:
        data = read_bytes(path, MAX_BYTES)
    except ReadError as e:
        raise ShotsError("cannot read %s/%s: %s" % (image["lang"], image["file"], e)) from e
    if hashlib.sha256(data).hexdigest() != image["sha256"]:
        raise ShotsError("%s/%s does not match its recorded sha256" % (image["lang"], image["file"]))
    clean, _, _ = check_png(cfg, data)
    if clean != data:
        raise ShotsError("%s/%s contains chunks that are not allowed" % (image["lang"], image["file"]))


def _now():
    return cw.now()


def _stamp():
    return _now().strftime(cw.TIME_FORMAT)


def _write_file_atomic(directory, name, data):
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), 0o644)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return tmp


class Album:
    """All images of one app, loaded under the app lock. Use through `locked`."""

    def __init__(self, cfg, app, images):
        self.cfg, self.app, self.images = cfg, app, tuple(images)

    @property
    def directory(self):
        return app_dir(self.cfg, self.app)

    def find(self, lang, file):
        return next((i for i in self.images if i["lang"] == lang and i["file"] == file), None)

    def duplicate(self, lang, sha256, rejected_too=False):
        pool = [i for i in self.images if i["lang"] == lang] if rejected_too else active(self.images, lang)
        return next((i for i in pool if i["sha256"] == sha256), None)

    def _replace_image(self, old, new):
        rest = [i for i in self.images if i is not old]
        self.images = tuple(sorted(rest + ([new] if new else []), key=sort_key))

    def next_name(self, lang, scene, prefer=None):
        used = {int(FILE_RE.match(i["file"]).group(1)) for i in self.images if i["lang"] == lang}
        number = prefer if prefer and prefer not in used else (max(used, default=0) + 1)
        if number > 99:
            free = [n for n in range(1, 100) if n not in used]
            if not free:
                raise ShotsError("no file number left in %s" % lang)
            number = free[0]
        return "%02d-%s.png" % (number, scene)

    def archive(self, lang, file, keep_original):
        """Move (or, with keep_original, hard link) <lang>/<file> into .prev/<stamp>/<lang>/."""
        source = os.path.join(self.directory, lang, file)
        stamp = _now().strftime(STAMP_FORMAT)
        for suffix in [""] + ["-%d" % n for n in range(1, 100)]:
            target_dir = os.path.join(self.directory, ".prev", stamp + suffix, lang)
            if not os.path.lexists(os.path.join(target_dir, file)):
                break
        else:
            raise ShotsError("cannot find a free .prev slot for %s" % file)
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, file)
        if keep_original:
            os.link(source, target)
        else:
            os.rename(source, target)

    def store(self, lang, scene, payload, width, height, *, source, status, reviewer, origin, note="",
              replace=None, prefer=None):
        """Add the (already validated, stripped) PNG `payload`; `replace` is the file whose slot it takes over."""
        old = None
        if replace is not None:
            old = self.find(lang, replace)
            if old is None:
                raise ShotsError("no image %s/%s to replace" % (lang, replace))
        sha = hashlib.sha256(payload).hexdigest()
        twin = self.duplicate(lang, sha)
        if twin is not None:
            raise ShotsError("identical to %s/%s" % (lang, twin["file"]))
        counted = len(active(self.images, lang)) - (1 if old is not None and old["status"] != "rejected" else 0)
        if counted >= self.cfg.shots_max_per_lang:
            raise ShotsError("%s already has %d images (at most %d)" % (lang, counted, self.cfg.shots_max_per_lang))
        if old is not None:
            name = "%s-%s.png" % (FILE_RE.match(old["file"]).group(1), scene)
        else:
            name = self.next_name(lang, scene, prefer)
        record = {"lang": lang, "file": name, "scene": scene, "sha256": sha, "size": len(payload),
                  "width": width, "height": height, "source": source, "status": status,
                  "reviewer": reviewer if status != "draft" else None,
                  "reviewed": _stamp() if status != "draft" else None, "note": note, "origin": origin}
        directory = os.path.join(self.directory, lang)
        tmp = _write_file_atomic(directory, name, payload)
        try:
            if old is not None:
                self.archive(lang, old["file"], keep_original=True)
            os.replace(tmp, os.path.join(directory, name))
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        if old is not None and old["file"] != name:
            os.unlink(os.path.join(directory, old["file"]))
        self._replace_image(old, record)
        return record

    def set_status(self, lang, file, status, reviewer, note=None):
        old = self.find(lang, file)
        if old is None:
            raise ShotsError("no image %s/%s" % (lang, file))
        if status == "approved":
            verify_stored(self.cfg, self.app, old)
        if status != "rejected" and old["status"] == "rejected" \
                and len(active(self.images, lang)) >= self.cfg.shots_max_per_lang:
            raise ShotsError("%s already has %d images (at most %d)" % (
                lang, len(active(self.images, lang)), self.cfg.shots_max_per_lang))
        new = dict(old, status=status, note=old["note"] if note is None else note,
                   reviewer=None if status == "draft" else reviewer,
                   reviewed=None if status == "draft" else _stamp())
        self._replace_image(old, new)
        return new

    def remove(self, lang, file):
        old = self.find(lang, file)
        if old is None:
            raise ShotsError("no image %s/%s" % (lang, file))
        self.archive(lang, file, keep_original=False)
        self._replace_image(old, None)
        return old

    def save(self):
        doc = {"schema": SCHEMA, "app": self.app, "updated": _stamp(), "images": list(self.images)}
        tmp = _write_file_atomic(self.directory, "meta.json", (
            json.dumps(doc, ensure_ascii=True, indent=1, sort_keys=True) + "\n").encode("ascii"))
        try:
            os.replace(tmp, os.path.join(self.directory, "meta.json"))
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        self.persist_dirs()

    def persist_dirs(self):
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


@contextlib.contextmanager
def locked(cfg, app):
    """The Album of app under its flock; StateLocked if another writer holds it, ShotsError for a bad tree."""
    try:
        if not stat.S_ISDIR(os.stat(cfg.screenshots).st_mode):
            raise ShotsError("%s is not a directory" % cfg.screenshots)
    except OSError as e:
        raise ShotsError("screenshots directory %s: %s" % (cfg.screenshots, e.strerror)) from e
    directory = app_dir(cfg, app)
    try:
        os.makedirs(directory, exist_ok=True)
        fd = os.open(os.path.join(directory, ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    except OSError as e:
        raise ShotsError("cannot use %s: %s" % (directory, e.strerror)) from e
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise StateLocked("another change to the screenshots of %s is running" % app) from e
        images, problems = read_images(cfg, app)
        if problems:
            raise ShotsError("meta.json of %s needs a human: %s" % (app, "; ".join(problems[:3])))
        yield Album(cfg, app, images)
    finally:
        os.close(fd)
