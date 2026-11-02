# SPDX-License-Identifier: GPL-3.0-or-later
"""Screenshot scenes: validation of the queue's `shots` object, desktop Exec handling, guest geometry, shots.json."""

import re

from .shots_img import clamp_rect
from .util import clean

LANGS = ("zh_CN", "en_US")
LOCALES = {"zh_CN": "zh_CN.UTF-8", "en_US": "en_US.UTF-8"}
LANGUAGE_VARS = {"zh_CN": "zh_CN:zh", "en_US": "en_US:en"}
SCENE_ID_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,30}\Z")
DEMO_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,40}\Z")
KEY_RE = re.compile(r"\A[A-Za-z0-9_+]{1,40}\Z")
BUTTONS = {"left": "1", "middle": "2", "right": "3"}
MAX_SCENES = 6
MAX_STEPS = 30
MAX_ARGS = 8
WINDOW_LIMITS = ((800, 1900), (600, 1050))
DEFAULT_WINDOW = (1600, 1000)
MIN_IMAGE = (400, 300)
UNIFORM_STDDEV = 4.0
HOME = "/home/tester"


class ShotsError(ValueError):
    pass


def _int(value, low, high, what):
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ShotsError("%s must be an integer between %d and %d" % (what, low, high))
    return value


def _text(value, limit, what, minimum=1):
    if (not isinstance(value, str) or not minimum <= len(value) <= limit
            or not all(c.isprintable() for c in value)):
        raise ShotsError("%s must be one line of printable text (%d..%d chars)" % (what, minimum, limit))
    return value


def validate_window(value, default):
    if value is None:
        return default
    if not isinstance(value, dict):
        raise ShotsError("window must be an object")
    return (_int(value.get("width"), *WINDOW_LIMITS[0], "window width"),
            _int(value.get("height"), *WINDOW_LIMITS[1], "window height"))


def validate_step(step):
    """(kind, value) for one step object; ShotsError otherwise."""
    if not isinstance(step, dict) or len(step) != 1:
        raise ShotsError("a step has exactly one key")
    (kind, value), = step.items()
    if kind == "wait":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 20:
            raise ShotsError("wait must be a number in (0, 20]")
        return kind, float(value)
    if kind == "key":
        if not isinstance(value, str) or not KEY_RE.match(value):
            raise ShotsError("bad key")
        return kind, value
    if kind == "type":
        return kind, _text(value, 200, "type text")
    if kind == "wait_window":
        return kind, _text(value, 80, "wait_window title")
    if kind == "click":
        if not isinstance(value, list) or len(value) not in (2, 3):
            raise ShotsError("click needs [x, y] or [x, y, button]")
        button = value[2] if len(value) == 3 else "left"
        if button not in BUTTONS:
            raise ShotsError("bad click button")
        return kind, (_int(value[0], 0, 4000, "click x"), _int(value[1], 0, 4000, "click y"), button)
    raise ShotsError("unknown step")


def validate_scene(scene, default_window):
    if not isinstance(scene, dict):
        raise ShotsError("scene must be an object")
    sid = scene.get("id")
    if not isinstance(sid, str) or not SCENE_ID_RE.match(sid):
        raise ShotsError("bad scene id")
    caption = scene.get("caption")
    if not isinstance(caption, dict):
        raise ShotsError("scene %s needs a caption" % sid)
    caps = {lang: _text(caption.get(lang), 80, "caption " + lang) for lang in LANGS}
    demo = scene.get("demo", [])
    if not isinstance(demo, list) or len(demo) > 20 or not all(isinstance(d, str) and DEMO_RE.match(d) for d in demo):
        raise ShotsError("bad demo list")
    args = scene.get("args", [])
    if not isinstance(args, list) or len(args) > MAX_ARGS:
        raise ShotsError("bad args")
    for a in args:
        _text(a, 300, "arg")
    steps = scene.get("steps", [])
    if not isinstance(steps, list) or len(steps) > MAX_STEPS:
        raise ShotsError("too many steps")
    if scene.get("capture", "window") != "window":
        raise ShotsError("capture must be window")
    return {"id": sid, "caption": caps, "demo": list(demo), "args": list(args),
            "window": validate_window(scene.get("window"), default_window),
            "steps": [validate_step(s) for s in steps]}


def validate(doc):
    """Normalised {"window": (w, h), "scenes": [...]} from the queue JSON's shots object."""
    if not isinstance(doc, dict) or doc.get("schema") != 1:
        raise ShotsError("unsupported shots schema")
    window = validate_window(doc.get("window"), DEFAULT_WINDOW)
    scenes = doc.get("scenes")
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= MAX_SCENES:
        raise ShotsError("shots needs 1..%d scenes" % MAX_SCENES)
    out = [validate_scene(s, window) for s in scenes]
    if len({s["id"] for s in out}) != len(out):
        raise ShotsError("duplicate scene id")
    return {"window": window, "scenes": out}


# ---- desktop entry Exec ----

def read_exec(text):
    """The Exec value of the [Desktop Entry] group of guest-provided desktop file text, or None."""
    in_main = False
    for line in text[:65536].splitlines():
        line = line.strip()
        if line.startswith("["):
            in_main = line == "[Desktop Entry]"
        elif in_main and line.startswith("Exec="):
            value = line[5:]
            return value if 0 < len(value) <= 4096 and all(c.isprintable() for c in value) else None
    return None


def _unescape_value(value):
    table = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
    return re.sub(r"\\(.)", lambda m: table.get(m.group(1), m.group(0)), value)


def split_exec(value):
    """Arguments of an Exec string per the desktop entry spec (string escapes, then double-quote rules)."""
    value = _unescape_value(value)
    args, cur, quoted, have = [], [], False, False
    i = 0
    while i < len(value):
        c = value[i]
        if quoted:
            if c == "\\" and i + 1 < len(value) and value[i + 1] in '"`$\\':
                cur.append(value[i + 1])
                i += 1
            elif c == '"':
                quoted = False
            else:
                cur.append(c)
        elif c == '"':
            quoted, have = True, True
        elif c in " \t":
            if cur or have:
                args.append("".join(cur))
            cur, have = [], False
        else:
            cur.append(c)
        i += 1
    if quoted:
        return None
    if cur or have:
        args.append("".join(cur))
    return args


def expand_args(args, scene_args):
    """Replace %f %F %u %U by the scene's arguments (~/ expands), drop other field codes, %% -> %."""
    given = [HOME + a[1:] if a.startswith("~/") else a for a in scene_args]
    out, used = [], False
    for arg in args:
        if arg in ("%F", "%U"):
            out += given
            used = True
        elif arg in ("%f", "%u"):
            out += given[:1]
            used = True
        else:
            def sub(m):
                nonlocal used
                code = m.group(1)
                if code == "%":
                    return "%"
                if code in "fFuU":
                    used = True
                    return given[0] if given else ""
                return ""
            text = re.sub(r"%(.)", sub, arg, flags=re.S)
            if text or not re.fullmatch(r"%.", arg, re.S):
                out.append(text)
    if not used:
        out += given
    return out


def build_command(exec_value, scene_args):
    """argv for the scene launch, or None when the Exec line is unusable."""
    args = split_exec(exec_value)
    if not args:
        return None
    argv = expand_args(args, scene_args)
    if not argv or not argv[0] or argv[0].startswith("-") or "\0" in "".join(argv):
        return None
    return argv


# ---- guest geometry (untrusted numbers) ----

def parse_geometry(text):
    """(x, y, w, h) from `xdotool getwindowgeometry --shell` output, or None."""
    vals = dict(re.findall(r"^(X|Y|WIDTH|HEIGHT)=(-?[0-9]{1,6})$", text, re.M))
    if len(vals) != 4:
        return None
    return int(vals["X"]), int(vals["Y"]), int(vals["WIDTH"]), int(vals["HEIGHT"])


def parse_extents(text):
    """(left, right, top, bottom) from `xprop _NET_FRAME_EXTENTS`; zeros when absent or implausible."""
    m = re.search(r"=\s*(\d{1,4}),\s*(\d{1,4}),\s*(\d{1,4}),\s*(\d{1,4})", text)
    if not m:
        return 0, 0, 0, 0
    vals = tuple(int(v) for v in m.groups())
    return vals if all(v <= 400 for v in vals) else (0, 0, 0, 0)


def frame_rect(geometry, extents, screen):
    """Window frame rectangle clamped to the screen, or None."""
    if geometry is None:
        return None
    x, y, w, h = geometry
    left, right, top, bottom = extents
    return clamp_rect(x - left, y - top, w + left + right, h + top + bottom, screen[0], screen[1])


def image_problems(width, height, stddev, dialogs):
    """Automatic checks for one image (docs/sandbox.md)."""
    problems = []
    if width < MIN_IMAGE[0] or height < MIN_IMAGE[1]:
        problems.append("image too small (%dx%d)" % (width, height))
    if stddev < UNIFORM_STDDEV:
        problems.append("image nearly uniform (luminance stddev %.1f)" % stddev)
    problems += ["extra dialog window: " + clean(t, 80) for t in dialogs[:3]]
    return problems


def build_shots_json(job, scene_results):
    """shots.json document; scene_results are dicts {scene, n, files, problems}."""
    scenes = []
    for r in scene_results:
        scenes.append({"id": r["scene"]["id"], "n": r["n"], "caption": dict(r["scene"]["caption"]),
                       "files": dict(r["files"]), "problems": [clean(p, 200) for p in r["problems"][:20]]})
    return {"schema": 1, "app": job.app, "version": job.version, "sha256": job.sha256, "scenes": scenes}
