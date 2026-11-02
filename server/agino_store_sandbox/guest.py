# SPDX-License-Identifier: GPL-3.0-or-later
"""The steps run inside the guest. Everything the guest returns is untrusted data."""

import re
import time

from . import profile
from .util import DESKTOP_PATH_RE, DESKTOP_RE, InfraError, clean

APT_DIRS = ["/etc/apt/sources.list.d", "/etc/apt/trusted.gpg.d", "/etc/apt/keyrings", "/usr/share/keyrings"]
WATCH_DIRS = APT_DIRS + ["/etc/systemd/system", "/etc/cron.d"]
USER = "tester"
UID = "1000"
ENV_KEYS = ("DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")
MAX_LISTED = 5000
MAX_ERROR_LINES = 20
CURSOR_RE = re.compile(r"\A[A-Za-z0-9=;_-]{1,512}\Z")
WINDOW_RE = re.compile(r"0x[0-9a-fA-F]{1,16}")
ENV_VALUE_RE = re.compile(r"\A[\x20-\x7e]{1,512}\Z")

COPY_DEB = """
mkdir -p /var/tmp/sbx && chmod 755 /var/tmp/sbx || exit 3
i=0
while [ ! -b /dev/disk/by-id/virtio-sbxdeb ] && [ $i -lt 30 ]; do sleep 1; i=$((i+1)); done
head -c "$1" /dev/disk/by-id/virtio-sbxdeb > /var/tmp/sbx/x.deb || exit 4
chmod 644 /var/tmp/sbx/x.deb
echo "$2  /var/tmp/sbx/x.deb" | sha256sum -c - >/dev/null || exit 5
"""
LIST_DIRS = 'for d in "$@"; do [ -d "$d" ] && find "$d" -mindepth 1 \\( -type f -o -type l \\) -print0; done; true'
REMOVE_UNOWNED = 'for f in "$@"; do dpkg -S -- "$f" >/dev/null 2>&1 || { rm -f -- "$f" && printf "%s\\0" "$f"; }; done; true'
WRITE_DEFAULT = 'rm -f -- "$1" && umask 022 && set -C && cat > "$1" && chmod 0644 "$1"'
SESSION_ENV = r"""
for p in $(pgrep -u 1000 -x dde-shell; pgrep -u 1000 -x startdde; pgrep -u 1000); do
  e=$(tr '\0' '\n' < /proc/$p/environ 2>/dev/null | grep -E '^(DISPLAY|XAUTHORITY|DBUS_SESSION_BUS_ADDRESS|XDG_RUNTIME_DIR)=')
  if printf '%s\n' "$e" | grep -q '^DISPLAY=' && printf '%s\n' "$e" | grep -q '^DBUS_SESSION_BUS_ADDRESS='; then
    printf '%s\n' "$e"; exit 0
  fi
done
exit 1
"""


def _in_dirs(path, dirs):
    return (path.startswith("/") and ".." not in path.split("/") and "\n" not in path
            and any(path.startswith(d + "/") for d in dirs))


def parse_listing(raw, dirs):
    """Set of file paths from a NUL-separated guest listing; anything outside dirs is dropped."""
    out = set()
    for item in raw.split(b"\0"):
        path = item.decode("utf-8", "replace")
        if _in_dirs(path, dirs) and not any(ord(c) < 0x20 for c in path):
            out.add(path)
            if len(out) >= MAX_LISTED:
                break
    return out


def parse_env(text):
    """Session environment {KEY: value} from KEY=value lines; None unless DISPLAY and the bus are there."""
    env = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in ENV_KEYS and ENV_VALUE_RE.match(value):
            env.setdefault(key, value)
    return env if "DISPLAY" in env and "DBUS_SESSION_BUS_ADDRESS" in env else None


def parse_windows(text):
    return set(WINDOW_RE.findall(text))


def parse_cursor(text):
    m = re.search(r"cursor:\s*(\S+)", text)
    return m.group(1) if m and CURSOR_RE.match(m.group(1)) else None


class Guest:
    def __init__(self, qga):
        self.qga = qga
        self.env = None

    def wait_session(self, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            res = self.qga.sh(SESSION_ENV, timeout=20)
            env = parse_env(res.text) if res.ok else None
            if env and self.qga.sh('[ -S /run/user/1000/bus ]', timeout=10).ok:
                self.env = env
                return env
            time.sleep(3)
        raise InfraError("tester's graphical session did not appear within %d s" % timeout)

    def as_tester(self, argv, timeout):
        env = ["%s=%s" % (k, v) for k, v in sorted(self.env.items())]
        return self.qga.exec(["/usr/sbin/runuser", "-u", USER, "--", "/usr/bin/env", *env, *argv], timeout)

    def windows(self):
        res = self.as_tester(["/usr/bin/xprop", "-root", "_NET_CLIENT_LIST"], 20)
        if not res.ok:
            raise InfraError("xprop failed in the guest session: %s" % clean(res.err.decode("utf-8", "replace"), 150))
        return parse_windows(res.text)

    def listing(self, dirs):
        res = self.qga.sh(LIST_DIRS, dirs, timeout=60)
        return parse_listing(res.out, dirs)

    def journal_cursor(self):
        res = self.qga.exec(["/usr/bin/journalctl", "--no-pager", "-n", "0", "--show-cursor", "-o", "cat"], 30)
        return parse_cursor(res.text)

    def journal_errors(self, cursor):
        if cursor is None:
            return 0, []
        res = self.qga.exec(["/usr/bin/journalctl", "--no-pager", "-p", "err", "-o", "cat",
                             "--after-cursor=" + cursor], 60)
        lines = [ln for ln in res.text.splitlines() if ln.strip()]
        return len(lines), [clean(ln, 300) for ln in lines[:MAX_ERROR_LINES]]

    def copy_deb(self, job):
        res = self.qga.sh(COPY_DEB, [str(job.size), job.sha256], timeout=300)
        if not res.ok:
            raise InfraError("copying the deb into the guest failed (exit %d)" % res.exitcode)

    def apply_profile(self, job):
        if job.debconf:
            data = ("\n".join(job.debconf) + "\n").encode("utf-8")
            res = self.qga.exec(["/usr/bin/debconf-set-selections"], 60, data)
            if not res.ok:
                return "debconf-set-selections failed (exit %d)" % res.exitcode
        for path, content in job.default_files:
            if not profile.DEFAULT_PATH_RE.match(path):
                return "bad default file path"
            res = self.qga.sh(WRITE_DEFAULT, [path], timeout=30, input_data=content)
            if not res.ok:
                return "writing %s failed" % path
        return None

    def remove_unowned(self, new_paths):
        """Delete new apt-dir files dpkg cannot attribute (like the helper); returns the removed paths."""
        paths = sorted(p for p in new_paths if _in_dirs(p, APT_DIRS))
        if not paths:
            return []
        res = self.qga.sh(REMOVE_UNOWNED, paths, timeout=60)
        return sorted(parse_listing(res.out, APT_DIRS))

    def find_desktop(self, job):
        if job.launchable:
            path = "/usr/share/applications/" + job.launchable
            if DESKTOP_RE.match(job.launchable) and self.qga.sh('[ -f "$1" ]', [path], timeout=10).ok:
                return path
            return None
        res = self.qga.exec(["/usr/bin/dpkg", "-L", job.package], 30)
        for line in res.text.splitlines():
            if DESKTOP_PATH_RE.match(line.strip()):
                return line.strip()
        return None

    def is_installed(self, package):
        res = self.qga.exec(["/usr/bin/dpkg-query", "-W", "-f=${db:Status-Abbrev}", package], 30)
        return res.ok and res.text.strip().startswith("ii")

    def apt(self, args, timeout):
        return self.qga.exec(["/usr/bin/env", "DEBIAN_FRONTEND=noninteractive", "/usr/bin/apt-get", *args], timeout)


def run_job(guest, job, screenshot, log=lambda msg: None):
    """All guest checks for one job. Returns (checks, details, problems); InfraError if the guest is unusable."""
    log("waiting for the session")
    guest.wait_session()
    snapshot = guest.listing(WATCH_DIRS)
    cursor = guest.journal_cursor()
    guest.copy_deb(job)
    details = {"package": job.package}
    problems = []
    checks = {"installed": False, "third_party_sources": [], "desktop_file": None, "window_seen": False,
              "new_errors": 0, "removed_clean": False}
    note = guest.apply_profile(job)
    if note:
        problems.append("install_profile: " + note)
    log("apt-get update / install")
    details["apt_update_exit"] = guest.apt(["update"], 300).exitcode
    res = guest.apt(["install", "-y", "/var/tmp/sbx/x.deb"], 900)
    details["apt_install_exit"] = res.exitcode
    if res.timed_out:
        problems.append("apt-get install timed out")
    elif not res.ok:
        details["apt_install_tail"] = clean(res.text[-600:] + res.err.decode("utf-8", "replace")[-300:], 900)
    checks["installed"] = guest.is_installed(job.package)

    new_apt = guest.listing(APT_DIRS) - snapshot
    removed = guest.remove_unowned(new_apt)
    details["third_party_sources_removed"] = [clean(p, 300) for p in removed[:50]]
    checks["third_party_sources"] = [clean(p, 300) for p in sorted(guest.listing(APT_DIRS) - snapshot)[:50]]

    desktop = guest.find_desktop(job) if checks["installed"] else None
    if desktop:
        checks["desktop_file"] = desktop.rsplit("/", 1)[-1]
        details["desktop_path"] = clean(desktop, 300)
        windows_before = guest.windows()
        launch = guest.qga.start(["/usr/bin/timeout", "-k", "5", "60", "/usr/sbin/runuser", "-u", USER, "--",
                                  "/usr/bin/env", *["%s=%s" % kv for kv in sorted(guest.env.items())],
                                  "/usr/bin/gio", "launch", desktop])
        log("launched %s" % desktop)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if guest.windows() - windows_before:
                checks["window_seen"] = True
                break
            time.sleep(2)
        time.sleep(2)
        finished = guest.qga.status(launch)
        details["launch_exit"] = finished.exitcode if finished else None
        screenshot()
    elif checks["installed"]:
        problems.append("no desktop file found")
    count, lines = guest.journal_errors(cursor)
    checks["new_errors"] = count
    details["error_lines"] = lines

    log("purging")
    purge = guest.apt(["purge", "-y", "--autoremove", job.package], 600)
    details["apt_purge_exit"] = purge.exitcode
    leftover = sorted(guest.listing(WATCH_DIRS) - snapshot)
    details["leftover_files"] = [clean(p, 300) for p in leftover[:50]]
    checks["removed_clean"] = not guest.is_installed(job.package) and not leftover and purge.ok
    return checks, details, problems
