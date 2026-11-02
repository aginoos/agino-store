# SPDX-License-Identifier: GPL-3.0-or-later
"""Guest-side operations of the screenshot phase. Everything the guest returns is untrusted data."""

import re
import time

from . import shots_scene
from .guest import USER, WINDOW_RE
from .util import InfraError, clean

DEMO_TAR = "/var/tmp/sbx/demo.tar"
DEMO_DIR = shots_scene.HOME + "/Demo"
MAX_PIDS = 500
TRANSIENT_TYPES = ("SPLASH", "DIALOG", "NOTIFICATION", "TOOLTIP", "POPUP_MENU", "DROPDOWN_MENU", "UTILITY")

# Both languages run in the same session: each app gets LANG/LANGUAGE/LC_ALL at launch and only
# its window is captured, so the session's language does not matter. The locales must exist.
ENSURE_LOCALES = r"""
for l in "$@"; do
    want=$(printf '%s' "$l" | tr 'A-Z' 'a-z' | sed 's/utf-8$/utf8/')
    locale -a 2>/dev/null | tr 'A-Z' 'a-z' | grep -qx "$want" && continue
    localedef -i "${l%%.*}" -c -f UTF-8 -A /usr/share/locale/locale.alias "$l" || exit 1
done
"""
COPY_DEMO = """
mkdir -p /var/tmp/sbx && chmod 755 /var/tmp/sbx || exit 3
i=0
while [ ! -b /dev/disk/by-id/virtio-sbxdemo ] && [ $i -lt 30 ]; do sleep 1; i=$((i+1)); done
head -c "$1" /dev/disk/by-id/virtio-sbxdemo > /var/tmp/sbx/demo.tar || exit 4
chmod 644 /var/tmp/sbx/demo.tar
echo "$2  /var/tmp/sbx/demo.tar" | sha256sum -c - >/dev/null || exit 5
"""
# fresh ~/Demo for every scene; the trees are extracted as tester without owner or modes from the archive
FRESH_DEMO = """
rm -rf -- /home/tester/Demo || exit 3
runuser -u tester -- mkdir -p /home/tester/Demo || exit 4
[ $# -eq 0 ] && exit 0
runuser -u tester -- tar -xf /var/tmp/sbx/demo.tar -C /home/tester/Demo --no-same-owner --no-same-permissions -- "$@"
"""


def hex_id(value):
    """Canonical 0x… form of a window id (decimal or hex text) or None."""
    try:
        n = int(value, 0) if isinstance(value, str) else int(value)
    except ValueError:
        return None
    return "0x%x" % n if 0 < n < (1 << 32) else None


def parse_pids(text):
    return {int(w) for w in text.split() if re.fullmatch(r"[0-9]{1,9}", w)}


class ShotsGuest:
    def __init__(self, guest):
        self.guest = guest
        self.qga = guest.qga

    def has_xdotool(self):
        return self.qga.sh('[ -x /usr/bin/xdotool ]', timeout=10).ok

    def xdotool(self, args, timeout=20):
        return self.guest.as_tester(["/usr/bin/xdotool", *args], timeout)

    def stage_demo(self, job):
        res = self.qga.sh(COPY_DEMO, [str(job.demo_size), job.demo_sha256], timeout=300)
        if not res.ok:
            raise InfraError("copying the demo archive into the guest failed (exit %d)" % res.exitcode)

    def fresh_demo(self, names):
        return self.qga.sh(FRESH_DEMO, list(names), timeout=120).ok

    def ensure_locales(self):
        """Generate the scene locales when the base image lacks them; InfraError otherwise."""
        if not self.qga.sh(ENSURE_LOCALES, list(shots_scene.LOCALES.values()), timeout=300).ok:
            raise InfraError("cannot generate the zh_CN and en_US locales in the guest")

    def tester_pids(self):
        res = self.qga.exec(["/usr/bin/pgrep", "-u", USER], 20)
        return parse_pids(res.text)

    def kill_new(self, base_pids):
        """TERM then KILL tester's processes that did not exist in base_pids (never by name)."""
        new = sorted(self.tester_pids() - base_pids)[:MAX_PIDS]
        for sig in ("-TERM", "-KILL"):
            if not new:
                return 0
            self.qga.exec(["/bin/kill", sig, "--", *[str(p) for p in new]], 20)
            if sig == "-TERM":
                time.sleep(2)
            new = sorted(self.tester_pids() & set(new))
        return 1

    def start_app(self, argv, lang):
        env = ["LANG=" + shots_scene.LOCALES[lang], "LANGUAGE=" + shots_scene.LANGUAGE_VARS[lang],
               "LC_ALL=" + shots_scene.LOCALES[lang], "HOME=" + shots_scene.HOME]
        session = ["%s=%s" % kv for kv in sorted(self.guest.env.items())]
        return self.qga.start(["/usr/bin/timeout", "-k", "5", "600", "/usr/sbin/runuser", "-u", USER, "--",
                               "/usr/bin/env", *session, *env, *argv])

    def read_desktop_exec(self, desktop_path):
        res = self.qga.exec(["/bin/cat", "--", desktop_path], 20)
        return shots_scene.read_exec(res.text) if res.ok else None

    def title(self, wid):
        res = self.xdotool(["getwindowname", wid], 10)
        return clean(res.text.strip(), 200) if res.ok else ""

    def geometry(self, wid):
        res = self.xdotool(["getwindowgeometry", "--shell", wid], 10)
        return shots_scene.parse_geometry(res.text) if res.ok else None

    def extents(self, wid):
        res = self.guest.as_tester(["/usr/bin/xprop", "-id", wid, "_NET_FRAME_EXTENTS"], 10)
        return shots_scene.parse_extents(res.text) if res.ok else (0, 0, 0, 0)

    def window_type(self, wid):
        res = self.guest.as_tester(["/usr/bin/xprop", "-id", wid, "_NET_WM_WINDOW_TYPE"], 10)
        return res.text if res.ok else ""

    def is_dialog(self, wid):
        return "_NET_WM_WINDOW_TYPE_DIALOG" in self.window_type(wid)

    def is_main_candidate(self, wid):
        """Not a splash screen, dialog or other transient window (GIMP shows a splash before its window)."""
        return not any("_NET_WM_WINDOW_TYPE_" + t in self.window_type(wid) for t in TRANSIENT_TYPES)

    def active_window(self):
        res = self.xdotool(["getactivewindow"], 10)
        return hex_id(res.text.strip()) if res.ok else None

    def windows(self):
        return {w for w in (hex_id(x) for x in self.guest.windows()) if w and WINDOW_RE.fullmatch(w)}
