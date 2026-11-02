# SPDX-License-Identifier: GPL-3.0-or-later
"""The screenshot phase: per language and scene launch the app, drive it, capture the window as a PNG."""

import hashlib
import json
import os
import time

from . import shots_img, shots_scene
from .shots_guest import ShotsGuest
from .shots_scene import BUTTONS, LANGS
from .util import InfraError, SandboxError, clean

WINDOW_WAIT = 30
SETTLE = 2
STABLE = 3
MAX_PPM_FILE = shots_img.MAX_PPM


def _atomic(path, data):
    tmp = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def read_ppm(path):
    with open(path, "rb") as f:
        return shots_img.parse_ppm(f.read(MAX_PPM_FILE))


class Phase:
    def __init__(self, cfg, vm, guest, job, desktop, log):
        self.cfg, self.vm, self.job, self.desktop, self.log = cfg, vm, job, desktop, log
        self.g = ShotsGuest(guest)
        self.out = os.path.join(cfg.shots_out, job.stem)
        self.results = [{"scene": sc, "n": i, "files": {}, "problems": []}
                        for i, sc in enumerate(job.shots["scenes"], 1)]

    # ---- orchestration ----

    def run(self, base_pids):
        """Take all screenshots; returns the report's details.shots object. Never raises for guest trouble."""
        details = {"images": 0, "problems": 0}
        try:
            if not self.g.has_xdotool():
                raise SandboxError("xdotool is not installed in the base image")
            command = self._command()
            self.g.kill_new(base_pids)  # the check's own app instance must not be reused by the scenes
            if self.job.demo_path:
                self.g.stage_demo(self.job)
            self.g.ensure_locales()
            for lang in LANGS:
                self._language(lang, command)
        except (InfraError, SandboxError, OSError) as e:
            details["error"] = clean(str(e), 200)
            self.log("screenshots aborted: %s" % details["error"])
        return self._finish(details)

    def _command(self):
        exec_value = self.g.read_desktop_exec(self.desktop)
        if not exec_value or shots_scene.split_exec(exec_value) is None:
            raise SandboxError("cannot read the Exec line of the desktop file")
        return exec_value

    def _language(self, lang, exec_value):
        for r in self.results:
            try:
                self._scene(lang, r, exec_value)
            except InfraError as e:
                r["problems"].append("%s: scene aborted: %s" % (lang, clean(str(e), 120)))
                raise

    def _finish(self, details):
        doc = shots_scene.build_shots_json(self.job, self.results)
        data = (json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")
        images = sum(len(r["files"]) for r in self.results)
        try:
            os.makedirs(self.out, mode=0o755, exist_ok=True)
            _atomic(os.path.join(self.out, "shots.json"), data)
            details["sha256"] = hashlib.sha256(data).hexdigest()
        except OSError as e:
            details["error"] = clean("cannot write shots.json: %s" % e.strerror, 200)
        details["images"] = images
        details["problems"] = sum(len(r["problems"]) for r in self.results)
        return details

    # ---- one scene ----

    def _scene(self, lang, result, exec_value):
        sc = result["scene"]
        problems = result["problems"]

        def problem(text):
            problems.append("%s: %s" % (lang, text))

        argv = shots_scene.build_command(exec_value, shots_scene.scene_args(sc["args"], lang))
        if argv is None:
            problem("unusable Exec line")
            return
        if not self.g.fresh_demo(sc["demo"] if self.job.demo_path else []):
            problem("could not prepare ~/Demo")
        base_pids = self.g.tester_pids()
        baseline = self.g.windows()
        self.log("scene %s (%s)" % (sc["id"], lang))
        try:
            self.g.start_app(argv, lang)
            target = self._main_window(baseline)
            if target is None:
                problem("window not found")
                return
            self._arrange(target, sc["window"])
            main = target
            target = self._steps(sc["steps"], lang, target, baseline, problem)
            if target not in self.g.windows():  # e.g. a dialog matched by wait_window and closed again
                target = main
            time.sleep(SETTLE)
            self._capture(lang, result, target, baseline, problem)
        finally:
            self.g.kill_new(base_pids)

    def _main_window(self, baseline):
        """The largest new normal window once it stayed the same for STABLE polls (splash screens come first)."""
        deadline = time.monotonic() + WINDOW_WAIT
        last, seen = None, 0
        while time.monotonic() < deadline:
            new = [w for w in sorted(self.g.windows() - baseline) if self.g.is_main_candidate(w)]
            best = max(new, key=self._area) if new else None
            seen = seen + 1 if best is not None and best == last else 1
            last = best
            if best is not None and seen >= STABLE:
                return best
            time.sleep(1)
        return last

    def _area(self, wid):
        geo = self.g.geometry(wid)
        return geo[2] * geo[3] if geo else 0

    def _arrange(self, wid, size):
        w, h = size
        sw, sh = self.cfg.screen
        self.g.xdotool(["windowsize", wid, str(w), str(h), "windowmove", wid, str(max(0, (sw - w) // 2)),
                        str(max(0, (sh - h) // 2))])
        self.g.xdotool(["windowactivate", wid])
        time.sleep(1)

    def _steps(self, steps, lang, target, baseline, problem):
        main = target
        for kind, value in steps:
            if kind in ("type", "wait_window"):
                value = value[lang]
            if kind == "wait":
                time.sleep(value)
            elif kind == "key":
                if not self.g.xdotool(["key", "--clearmodifiers", "--", value]).ok:
                    problem("key %s failed" % value)
            elif kind == "type":
                if not self.g.xdotool(["type", "--delay", "30", "--", value]).ok:
                    problem("typing failed")
            elif kind == "click":
                x, y, button = value
                if target != main and target not in self.g.windows():
                    target = main
                if not self.g.xdotool(["mousemove", "--window", target, str(x), str(y), "click", BUTTONS[button]]).ok:
                    problem("click failed")
            elif kind == "wait_window":
                found = self._wait_title(value, baseline)
                if found:
                    target = found
                    # keys go to the focused window: a dialog that opened behind the main window
                    # (GIMP's welcome) would otherwise not get the Escape that should close it
                    self.g.xdotool(["windowactivate", found])
                    time.sleep(0.5)
                else:
                    titles = [t for t in (self.g.title(w) for w in sorted(self.g.windows() - baseline)) if t][:4]
                    problem("window titled %r did not appear (new windows: %s)"
                            % (clean(value, 40), "; ".join(clean(t, 50) for t in titles) or "none"))
        return target

    def _wait_title(self, text, baseline):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            for wid in sorted(self.g.windows() - baseline):
                if text in self.g.title(wid):
                    time.sleep(1)
                    return wid
            time.sleep(1)
        return None

    # ---- capture and checks ----

    def _capture(self, lang, result, target, baseline, problem):
        active = self.g.active_window()
        if active and active in self.g.windows() - baseline:
            target = active
        rect = shots_scene.frame_rect(self.g.geometry(target), self.g.extents(target), self.cfg.screen)
        if rect is None:
            problem("window geometry unusable")
            return
        dialogs = [self.g.title(w) for w in sorted(self.g.windows() - baseline - {target}) if self.g.is_dialog(w)]
        ppm = self.vm.files.path("shot.ppm")
        self.vm.qmp.screendump(ppm, "ppm")
        try:
            width, height, rgb = read_ppm(ppm)
            rect = shots_img.clamp_rect(*rect, width, height)
            if rect is None:
                problem("window outside the screen")
                return
            rows = shots_img.crop_rgba(width, rgb, rect, self.cfg.corner_radius)
            stddev = shots_img.luminance_stddev(width, rgb, rect)
        except (shots_img.ImageError, OSError) as e:
            problem("screendump unusable: %s" % clean(str(e), 80))
            return
        finally:
            try:
                os.unlink(ppm)
            except OSError:
                pass
        for text in shots_scene.image_problems(rect[2], rect[3], stddev, dialogs):
            problem(text)
        png = shots_img.encode_png(rect[2], rect[3], rows)
        name = "%d-%s.png" % (result["n"], result["scene"]["id"])
        directory = os.path.join(self.out, lang)
        os.makedirs(directory, mode=0o755, exist_ok=True)
        _atomic(os.path.join(directory, name), png)
        result["files"][lang] = {"name": name, "sha256": hashlib.sha256(png).hexdigest(),
                                 "width": rect[2], "height": rect[3]}
