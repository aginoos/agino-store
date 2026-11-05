# SPDX-License-Identifier: GPL-3.0-or-later
"""The patch file of content-edit: parsing, strict validation and applying it to a parsed app.yaml
(docs/server.md section 13). Field limits are the ones content.py enforces when it loads the repository."""

import json
import re

from . import content as C

PATCH_NAME_RE = re.compile(r"\A[A-Za-z0-9_-]{8,64}\.json\Z")
MAX_PATCH_BYTES = 64 << 10
MAX_CATEGORIES = 2
CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f]")
SINGLE_LINE = {"name": 100, "summary": 300, "developer": 100}   # localized single-line texts: max length
LOCALIZED = tuple(SINGLE_LINE) + ("description",)
PATCH_KEYS = LOCALIZED + ("categories", "license", "proprietary", "homepage", "distribution")
DISTRIBUTION_KEY = "distribution"
DEFAULT_DISTRIBUTION = {"mode": "store"}
REVIEWED_KEY = "description"   # only a changed description counts as reviewed (a summary edit does not)
RECONFIRMED_SOURCES = ("ai", "confirmed")   # official and submitter descriptions keep their origin mark


class PatchError(Exception):
    """The patch is not acceptable; the message has one problem per line."""


def parse(raw):
    """The patch object in `raw` (bytes of a UTF-8 JSON object)."""
    def pairs(items):
        keys = [k for k, _ in items]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate key in an object")
        return dict(items)

    def constant(name):
        raise ValueError("%s is not allowed" % name)

    try:
        patch = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise PatchError("patch is not valid UTF-8 JSON: %s" % e) from e
    if not isinstance(patch, dict) or not patch:
        raise PatchError("patch must be a non-empty JSON object")
    return patch


def _strings(path, value):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _strings("%s[%d]" % (path, i), item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings("%s.%s" % (path, key), item)


def _plain_text(chk, key, value):
    """No C0/C1 control characters in any string of a text field (markup and invisible characters are
    refused by the shared Check.display used for the field limits right after)."""
    for path, text in _strings("patch." + key, value):
        if CONTROL_RE.search(text):
            chk.fail(path, "contains control characters")


def _localized(chk, key, value):
    path = "patch." + key
    if isinstance(value, dict) and set(value) != set(C.REQUIRED_LANGS):
        chk.fail(path, "needs exactly zh_CN and en_US")
        return
    _plain_text(chk, key, value)
    if key == "description":
        chk.paragraphs(path, value)
    else:
        chk.text(path, value, SINGLE_LINE[key])


def _categories(chk, value, category_ids):
    items = chk.sequence("patch.categories", value, 1, MAX_CATEGORIES)
    seen = []
    for i, cid in enumerate(items or []):
        where = "patch.categories[%d]" % i
        if chk.string(where, cid, C.ID_RE, 64) is None:
            continue
        if cid not in category_ids:
            chk.fail(where, "unknown category %r" % cid)
        elif cid in seen:
            chk.fail(where, "duplicate category %r" % cid)
        seen.append(cid)


def validate(patch, category_ids):
    """Raise PatchError unless every key of the patch is allowed and valid."""
    chk = C.Check()
    for key, value in patch.items():
        if key in LOCALIZED:
            _localized(chk, key, value)
        elif key == "categories":
            _categories(chk, value, category_ids)
        elif key == "license":
            chk.string("patch.license", value, C.LICENSE_RE, 200)
        elif key == "proprietary":
            chk.boolean("patch.proprietary", value)
        elif key == "homepage":
            chk.url("patch.homepage", value, False)
        elif key == DISTRIBUTION_KEY:
            C.check_distribution(chk, "patch.distribution", value)
        else:
            chk.fail("patch", "unknown key %r (allowed: %s)" % (key, ", ".join(PATCH_KEYS)))
    if chk.errors:
        raise PatchError("\n".join(chk.errors))


def _normalized_distribution(value):
    """The comparable form of a distribution value: a missing key and {mode: store} are the same, and a
    vendor mapping without `direct` means direct false."""
    if not isinstance(value, dict) or value.get("mode", "store") == "store":
        return DEFAULT_DISTRIBUTION
    return {"mode": value["mode"], "page": value.get("page"), "direct": value.get("direct", False)}


def apply(data, patch):
    """(new app.yaml data, changed key names). Localized keys replace zh_CN and en_US and keep other
    languages; every other key replaces the whole value; distribution mode store removes the key. `data`
    is not modified."""
    new, changed = dict(data), []
    for key, value in patch.items():
        old = data.get(key)
        if key == DISTRIBUTION_KEY:
            if _normalized_distribution(value) == _normalized_distribution(old):
                continue
            if _normalized_distribution(value) == DEFAULT_DISTRIBUTION:
                new.pop(key, None)
            else:
                new[key] = value
            changed.append(key)
            continue
        merged = {**old, **value} if key in LOCALIZED and isinstance(old, dict) else value
        if merged != old:
            new[key] = merged
            changed.append(key)
    return new, changed


def reviews_description(old_data, changed):
    """True when this edit makes the editor the reviewer of the description: the description itself changed
    and it was AI-written (or already confirmed, then the reviewer is replaced). Editing any other key never
    does, and official / submitter descriptions are never turned into confirmed."""
    source = old_data.get("description_source", C.DEFAULT_DESCRIPTION_SOURCE)
    return REVIEWED_KEY in changed and source in RECONFIRMED_SOURCES


def mark_confirmed(data, editor, stamp):
    """A copy of `data` with description_source confirmed by `editor`; new keys follow `description`."""
    review = {"description_source": "confirmed", "description_confirmed": {"by": editor, "at": stamp}}
    out = {}
    for key, value in data.items():
        if key not in review:
            out[key] = value
        if key == "description":
            out.update(review)
    for key, value in review.items():
        out[key] = value   # description missing (cannot happen in a valid app): append
    return out
