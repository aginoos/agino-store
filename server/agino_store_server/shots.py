# SPDX-License-Identifier: GPL-3.0-or-later
"""Validation of apps/<id>/shots.yaml (docs/server.md section 3, screenshot pipeline contract).

Pure data validation: the caller loads the YAML, passes the names of the valid demo trees and a
checker for promo image files. The result is plain data, exactly what the sandbox queue exports.
"""

import re

SCENE_ID_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,30}\Z")
KEY_RE = re.compile(r"\A[A-Za-z0-9_+]{1,40}\Z")
WIDTH_RANGE = (800, 1900)
HEIGHT_RANGE = (600, 1050)
DEFAULT_WINDOW = {"width": 1600, "height": 1000}
MAX_SCENES = 6
MAX_STEPS = 30
MAX_ARGS = 8
MAX_PROMO = 20
CLICK_BUTTONS = ("left", "right", "middle")
STEP_KEYS = ("wait", "key", "type", "click", "wait_window")
LANGS = ("zh_CN", "en_US")


def _int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _window(chk, path, value):
    if not chk.mapping(path, value, ("width", "height")):
        return None
    ok = True
    for key, (low, high) in (("width", WIDTH_RANGE), ("height", HEIGHT_RANGE)):
        if not _int(value[key]) or not low <= value[key] <= high:
            chk.fail("%s.%s" % (path, key), "must be an integer %d..%d" % (low, high))
            ok = False
    return {"width": value["width"], "height": value["height"]} if ok else None


def _printable(chk, path, value, max_len):
    text = chk.string(path, value, max_len=max_len)
    if text is not None and (not text.isprintable() or text != text.strip()):
        chk.fail(path, "must be printable text on one line without leading or trailing space")
        return None
    return text


def _step(chk, path, step):
    if not isinstance(step, dict) or len(step) != 1 or next(iter(step)) not in STEP_KEYS:
        chk.fail(path, "must be a mapping with exactly one of %s" % ", ".join(STEP_KEYS))
        return None
    (kind, value), = step.items()
    if kind == "wait":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 20:
            chk.fail(path + ".wait", "must be a number of seconds with 0 < x <= 20")
            return None
        return {"wait": value}
    if kind == "key":
        text = chk.string(path + ".key", value, KEY_RE, 40)
        return {"key": text} if text else None
    if kind == "type":
        text = _localized(chk, path + ".type", value, _type_text)
        return {"type": text} if text is not None else None
    if kind == "wait_window":
        text = _localized(chk, path + ".wait_window", value, lambda c, p, v: _printable(c, p, v, 80) or None)
        return {"wait_window": text} if text is not None else None
    return _click(chk, path + ".click", value)


def _type_text(chk, path, value):
    text = chk.string(path, value, max_len=200)
    if text is not None and (not text.isprintable() or "\n" in text):
        chk.fail(path, "must be printable text without newlines (use key: Return)")
        return None
    return text


def _localized(chk, path, value, check):
    """One string for both languages, or {zh_CN: ..., en_US: ...} (titles and typed text differ by language)."""
    if not isinstance(value, dict):
        return check(chk, path, value)
    if set(value) != set(LANGS):
        chk.fail(path, "must be a string or a mapping with exactly %s" % ", ".join(LANGS))
        return None
    out = {lang: check(chk, "%s.%s" % (path, lang), value[lang]) for lang in LANGS}
    return out if None not in out.values() else None


def _click(chk, path, value):
    if not isinstance(value, list) or len(value) not in (2, 3):
        chk.fail(path, "must be [x, y] or [x, y, button]")
        return None
    x, y = value[0], value[1]
    if not (_int(x) and _int(y) and 0 <= x <= WIDTH_RANGE[1] and 0 <= y <= HEIGHT_RANGE[1]):
        chk.fail(path, "x and y must be integers inside the window (0..%d, 0..%d)"
                 % (WIDTH_RANGE[1], HEIGHT_RANGE[1]))
        return None
    if len(value) == 3 and value[2] not in CLICK_BUTTONS:
        chk.fail(path, "button must be one of %s" % ", ".join(CLICK_BUTTONS))
        return None
    return {"click": list(value)}


def _args(chk, path, value):
    items = chk.sequence(path, value, 0, MAX_ARGS)
    out = []
    for i, arg in enumerate(items or []):
        text = chk.string("%s[%d]" % (path, i), arg, max_len=300)
        if text is not None and ("\n" in text or "\r" in text):
            chk.fail("%s[%d]" % (path, i), "must not contain newlines")
            text = None
        out.append(text)
    return out if items is not None and None not in out else None


def _demo(chk, path, value, demo_names):
    items = chk.sequence(path, value, 0, 10)
    out = []
    for i, name in enumerate(items or []):
        where = "%s[%d]" % (path, i)
        if not isinstance(name, str) or name not in demo_names:
            chk.fail(where, "unknown demo tree %r (needs a directory demo/<name>/)" % (name,))
        elif name in out:
            chk.fail(where, "duplicate demo tree %r" % name)
        else:
            out.append(name)
    return out if items is not None and len(out) == len(items) else None


def _scene(chk, path, item, default_window, demo_names):
    keys = ("id", "caption")
    if not chk.mapping(path, item, keys, ("demo", "args", "window", "steps", "capture")):
        return None
    scene = {
        "id": chk.string(path + ".id", item["id"], SCENE_ID_RE, 31),
        "caption": chk.text(path + ".caption", item["caption"], 80),
        "demo": _demo(chk, path + ".demo", item.get("demo", []), demo_names),
        "args": _args(chk, path + ".args", item.get("args", [])),
        "window": (_window(chk, path + ".window", item["window"]) if "window" in item
                   else dict(default_window)),
        "capture": item.get("capture", "window"),
    }
    if scene["capture"] != "window":
        chk.fail(path + ".capture", 'must be "window"')
    steps = chk.sequence(path + ".steps", item.get("steps", []), 0, MAX_STEPS)
    parsed = [_step(chk, "%s.steps[%d]" % (path, i), s) for i, s in enumerate(steps or [])]
    scene["steps"] = parsed if steps is not None else None
    return scene if None not in scene.values() and None not in parsed else None


def validate(chk, path, data, demo_names):
    """Validated shots dict {schema, window, scenes} or None (errors collected in chk)."""
    if not chk.mapping(path, data, ("schema", "scenes"), ("window", "promo")):
        return None
    if data["schema"] != 1 or not _int(data["schema"]):
        chk.fail(path + ".schema", "must be 1")
        return None
    window = _window(chk, path + ".window", data["window"]) if "window" in data else dict(DEFAULT_WINDOW)
    items = chk.sequence(path + ".scenes", data["scenes"], 1, MAX_SCENES)
    scenes = [_scene(chk, "%s.scenes[%d]" % (path, i), s, window or DEFAULT_WINDOW, demo_names)
              for i, s in enumerate(items or [])]
    ids = [s["id"] for s in scenes if s]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        chk.fail(path + ".scenes", "duplicate scene id %r" % dup)
    if window is None or items is None or None in scenes or len(set(ids)) != len(ids):
        return None
    return {"schema": 1, "window": window, "scenes": scenes}


def promo(chk, path, value, check_image):
    """Validated promo list (tuple of dicts) or None. check_image(path, rel) returns rel or None."""
    items = chk.sequence(path, value, 0, MAX_PROMO)
    out = []
    for i, item in enumerate(items or []):
        where = "%s[%d]" % (path, i)
        if not chk.mapping(where, item, ("image", "lang", "source", "license")):
            out.append(None)
            continue
        lang = item["lang"]
        if lang not in ("zh_CN", "en_US"):
            chk.fail(where + ".lang", "must be zh_CN or en_US")
            lang = None
        out.append({"image": check_image(where + ".image", item["image"]), "lang": lang,
                    "source": chk.string(where + ".source", item["source"], max_len=2000),
                    "license": chk.string(where + ".license", item["license"], max_len=500)})
        if None in out[-1].values():
            out[-1] = None
    return tuple(out) if items is not None and None not in out else None
