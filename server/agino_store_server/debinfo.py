# SPDX-License-Identifier: GPL-3.0-or-later
"""Read .deb metadata without ever running maintainer scripts (docs/server.md section 7)."""

import datetime
import functools
import io
import os
import posixpath
import re
import resource
import subprocess
import tarfile
import threading

import apt_pkg

TOOL_TIMEOUT = 300
STDOUT_CAP = 4 << 20
STDERR_CAP = 1 << 20
LISTING_CAP = 64 << 20
LISTING_LINES = 2_000_000
CTRL_TAR_CAP = 16 << 20
CONTROL_FILE_MAX = 1 << 20
LIMIT_AS = 2 << 30
LIMIT_FSIZE = 256 << 20
LIMIT_CPU = 300
CONTROL_NAMES = frozenset(("control", "md5sums", "conffiles", "preinst", "postinst", "prerm", "postrm",
                           "config", "templates", "triggers", "shlibs", "symbols"))
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f]")
SCRIPT_NAMES = ("preinst", "postinst", "prerm", "postrm", "config")
VERSION_RE = re.compile(r"\A(?:[0-9]+:)?[0-9][A-Za-z0-9.+~:-]*\Z")
_LIST_RE = re.compile(
    r"^(?P<mode>[-dlhcbps][-rwxsStT]{9})\s+(?P<owner>\S+)\s+(?P<size>\d+)\s+"
    r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s(?P<name>.*)$"
)


class DebError(Exception):
    pass


def utc_now():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_text(value):
    """Vendor-supplied text for messages: control characters (except tab) removed."""
    return _CONTROL_CHARS.sub("", str(value))


def _limit_resources():
    resource.setrlimit(resource.RLIMIT_AS, (LIMIT_AS, LIMIT_AS))
    resource.setrlimit(resource.RLIMIT_FSIZE, (LIMIT_FSIZE, LIMIT_FSIZE))
    resource.setrlimit(resource.RLIMIT_CPU, (LIMIT_CPU, LIMIT_CPU))


class _Capture(threading.Thread):
    """Drain a pipe, keeping at most `cap` bytes; on overflow stop and kill the process."""

    def __init__(self, pipe, cap, proc, fatal):
        super().__init__(daemon=True)
        self.pipe, self.cap, self.proc, self.fatal = pipe, cap, proc, fatal
        self.chunks, self.size, self.overflow = [], 0, False

    def run(self):
        while True:
            chunk = self.pipe.read(65536)
            if not chunk:
                return
            room = self.cap - self.size
            self.chunks.append(chunk[:max(room, 0)])
            self.size += len(chunk)
            if self.size > self.cap:
                self.overflow = True
                if self.fatal:
                    self.proc.kill()
                    return

    def data(self):
        return b"".join(self.chunks)


def _feed(proc, data):
    try:
        if data is not None:
            proc.stdin.write(data)
    except OSError:
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass


def run_tool(args, timeout=TOOL_TIMEOUT, stdin=None, max_stdout=STDOUT_CAP):
    """Run a tool (argument list, no shell, with timeout and resource limits).

    Returns CompletedProcess; never raises on rc. stdout beyond max_stdout is an error.
    """
    try:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                preexec_fn=_limit_resources)   # noqa: PLW1509 - limits must be set pre-exec
    except OSError as e:
        raise DebError("cannot run %s: %s" % (args[0], e.strerror or e)) from e
    out, err = _Capture(proc.stdout, max_stdout, proc, True), _Capture(proc.stderr, STDERR_CAP, proc, False)
    timed_out = []
    timer = threading.Timer(timeout, lambda: (timed_out.append(True), proc.kill()))
    timer.start()
    threads = [out, err] + ([threading.Thread(target=_feed, args=(proc, stdin), daemon=True)]
                            if stdin is not None else [])
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
        proc.wait()
    finally:
        timer.cancel()
        proc.stdout.close()
        proc.stderr.close()
    if timed_out:
        raise DebError("%s timed out" % args[0])
    if out.overflow:
        raise DebError("output of %s exceeds %d bytes" % (args[0], max_stdout))
    return subprocess.CompletedProcess(args, proc.returncode, out.data(), err.data())


def parse_stanzas(text):
    """Parse deb822 text into a list of dicts. Continuation lines are joined with newlines."""
    stanzas = []
    current = {}
    last = None
    for line in text.split("\n"):
        if not line.strip():
            if current:
                stanzas.append(current)
            current, last = {}, None
        elif line[0] in " \t" and last is not None:
            current[last] += "\n" + line.strip()
        elif line[0] != "#" and ":" in line:
            key, _, value = line.partition(":")
            last = key.strip()
            current[last] = value.strip()
    if current:
        stanzas.append(current)
    return stanzas


def control_fields(deb_path):
    proc = run_tool(["dpkg-deb", "-f", os.path.abspath(deb_path)])
    if proc.returncode != 0:
        raise DebError("not a valid deb: %s" % os.path.basename(deb_path))
    stanzas = parse_stanzas(proc.stdout.decode("utf-8", "replace"))
    if not stanzas:
        raise DebError("deb has no control fields")
    return stanzas[0]


def _control_member_name(member):
    name = posixpath.normpath(member.name).lstrip("/")
    return name if member.isreg() and name in CONTROL_NAMES else None


def _write_control_file(dest_dir, name, data):
    fd = os.open(os.path.join(dest_dir, name),
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def extract_control(deb_path, dest_dir):
    """Write the known top-level control files into the new directory dest_dir. Never runs anything."""
    proc = run_tool(["dpkg-deb", "--ctrl-tarfile", os.path.abspath(deb_path)], max_stdout=CTRL_TAR_CAP)
    if proc.returncode != 0:
        raise DebError("cannot extract control from %s" % os.path.basename(deb_path))
    os.mkdir(dest_dir, 0o755)
    try:
        with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tar:
            for member in tar:
                _extract_member(tar, member, dest_dir)
    except tarfile.TarError as e:
        raise DebError("bad control archive in %s: %s" % (os.path.basename(deb_path), e)) from e


def _extract_member(tar, member, dest_dir):
    name = _control_member_name(member)
    if name is None or member.size > CONTROL_FILE_MAX:
        return
    try:
        safe = tarfile.data_filter(member, dest_dir)
    except tarfile.FilterError:
        return
    src = tar.extractfile(safe)
    if src is None:
        return
    try:
        _write_control_file(dest_dir, name, src.read(CONTROL_FILE_MAX + 1)[:CONTROL_FILE_MAX])
    except FileExistsError:
        return


def file_list(deb_path):
    """Entries of the data archive: dicts with mode, type, owner, size, path and target."""
    proc = run_tool(["dpkg-deb", "-c", os.path.abspath(deb_path)], max_stdout=LISTING_CAP)
    if proc.returncode != 0:
        raise DebError("cannot list %s" % os.path.basename(deb_path))
    lines = proc.stdout.decode("utf-8", "replace").split("\n")
    if len(lines) > LISTING_LINES:
        raise DebError("%s lists more than %d entries" % (os.path.basename(deb_path), LISTING_LINES))
    return [e for e in map(parse_list_line, lines) if e]


def parse_list_line(line):
    m = _LIST_RE.match(line)
    if not m:
        return None
    name, target = m.group("name"), None
    kind = m.group("mode")[0]
    if kind == "l" and " -> " in name:
        name, _, target = name.partition(" -> ")
    elif kind == "h" and " link to " in name:
        name, _, target = name.partition(" link to ")
    path = name[2:] if name.startswith("./") else name.lstrip("/")
    return {"mode": m.group("mode"), "type": kind, "owner": m.group("owner"),
            "size": int(m.group("size")), "path": path, "target": target}


def installed_size_bytes(fields, files):
    """Installed-Size is in KiB in the control file; fall back to summing the file list."""
    raw = fields.get("Installed-Size", "")
    if raw.isdigit():
        return int(raw) * 1024
    return sum(e["size"] for e in files if e["type"] == "-")


@functools.cache
def _apt_pkg():
    apt_pkg.init_system()
    return apt_pkg


def version_gt(a, b):
    """True if Debian version a is strictly greater than b (apt_pkg: dpkg's rules, in process).

    Called tens of thousands of times when summarizing milkyway, so no dpkg subprocess per call.
    """
    for v in (a, b):
        if not VERSION_RE.match(v or ""):
            raise DebError("bad version string: %r" % (v,))
    return _apt_pkg().version_compare(a, b) > 0
