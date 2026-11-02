# SPDX-License-Identifier: GPL-3.0-or-later
"""Guest-side operations of the screenshot phase. Everything the guest returns is untrusted data."""

import re
import time

from . import shots_scene
from .guest import USER, WINDOW_RE
from .util import InfraError, clean

SWITCH_SCRIPT_PATH = "/var/tmp/sbx/shots-locale.sh"
DEMO_TAR = "/var/tmp/sbx/demo.tar"
DEMO_DIR = shots_scene.HOME + "/Demo"
MAX_PIDS = 500

# Port of infra tools/iso-test/login-as.sh for the existing user: sets the language with
# LangSelector1.SetLocale, removes the generated input method files, restarts LightDM and waits
# for dde-shell. Prints the session's LANG; exit 1 when it is not the wanted locale.
SWITCH_SCRIPT = r"""#!/bin/bash
set -u
user=tester locale=$1
dropin=/etc/lightdm/lightdm.conf.d/99-agino-test-autologin.conf
home=/home/tester
mkdir -p "$(dirname "$dropin")"
printf '[Seat:*]\nautologin-user=%s\nautologin-user-timeout=0\nautologin-session=deepin\n' "$user" > "$dropin"
wait_shell() {
    local sp
    for _ in $(seq 60); do
        sp=$(pgrep -u "$user" -x dde-shell | head -1)
        [ -n "$sp" ] && { sleep 10; echo "$sp"; return 0; }
        sleep 2
    done
    return 1
}
session_lang() { tr '\0' '\n' < "/proc/$1/environ" | sed -n 's/^LANG=//p'; }
sp=$(wait_shell) || { echo "no dde-shell" >&2; exit 1; }
if [ "$(session_lang "$sp")" != "$locale" ]; then
    runuser -u "$user" -- /usr/bin/env -i $(tr '\0' '\n' < "/proc/$sp/environ" | grep -E '^(DBUS_SESSION_BUS_ADDRESS|DISPLAY|XDG_RUNTIME_DIR)=') \
        gdbus call --session -d org.deepin.dde.LangSelector1 -o /org/deepin/dde/LangSelector1 \
        -m org.deepin.dde.LangSelector1.SetLocale "$locale" >/dev/null || exit 1
    want=$(printf '%s' "${locale%%.*}" | tr 'A-Z' 'a-z')
    for _ in $(seq 90); do
        locale -a 2>/dev/null | tr 'A-Z' 'a-z' | grep -q "^$want\.utf-\?8$" && break
        sleep 2
    done
    sleep 30
    systemctl stop display-manager.service
    for _ in $(seq 30); do pgrep -u "$user" -x dde-shell >/dev/null || break; sleep 1; done
    rm -rf "$home/.config/fcitx5" "$home/.local/share/fcitx5/rime"
    systemctl start display-manager.service
    sp=$(wait_shell) || { echo "no dde-shell" >&2; exit 1; }
fi
echo "LANG=$(session_lang "$sp")"
[ "$(session_lang "$sp")" = "$locale" ]
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

    def switch_locale(self, lang):
        """Switch tester's session to lang; True when it now runs with that LANG."""
        self.qga.write_file(SWITCH_SCRIPT_PATH, SWITCH_SCRIPT.encode("utf-8"))
        res = self.qga.exec([SWITCH_SCRIPT_PATH, shots_scene.LOCALES[lang]], 600)
        self.guest.env = None
        self.guest.wait_session()
        return res.ok

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

    def is_dialog(self, wid):
        res = self.guest.as_tester(["/usr/bin/xprop", "-id", wid, "_NET_WM_WINDOW_TYPE"], 10)
        return res.ok and "_NET_WM_WINDOW_TYPE_DIALOG" in res.text

    def active_window(self):
        res = self.xdotool(["getactivewindow"], 10)
        return hex_id(res.text.strip()) if res.ok else None

    def windows(self):
        return {w for w in (hex_id(x) for x in self.guest.windows()) if w and WINDOW_RE.fullmatch(w)}
