# SPDX-License-Identifier: GPL-3.0-or-later
"""Read .deb metadata without ever running maintainer scripts (docs/server.md section 7)."""

import datetime
import os
import re
import subprocess

TOOL_TIMEOUT = 300
SCRIPT_NAMES = ("preinst", "postinst", "prerm", "postrm", "config")
VERSION_RE = re.compile(r"\A[A-Za-z0-9.+~:-]+\Z")
_LIST_RE = re.compile(
    r"^(?P<mode>[-dlhcbps][-rwxsStT]{9})\s+(?P<owner>\S+)\s+(?P<size>\d+)\s+"
    r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s(?P<name>.*)$"
)


class DebError(Exception):
    pass


def utc_now():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_tool(args, timeout=TOOL_TIMEOUT, stdin=None):
    """Run a tool (argument list, no shell, with timeout). Returns CompletedProcess; never raises on rc."""
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout, input=stdin, check=False)
    except subprocess.TimeoutExpired as e:
        raise DebError("%s timed out" % args[0]) from e
    except OSError as e:
        raise DebError("cannot run %s: %s" % (args[0], e.strerror or e)) from e


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


def extract_control(deb_path, dest_dir):
    """Extract the control tarball into dest_dir (must not exist). Never runs anything."""
    proc = run_tool(["dpkg-deb", "-e", os.path.abspath(deb_path), os.path.abspath(dest_dir)])
    if proc.returncode != 0:
        raise DebError("cannot extract control from %s" % os.path.basename(deb_path))


def file_list(deb_path):
    """Entries of the data archive: dicts with mode, type, owner, size, path and target."""
    proc = run_tool(["dpkg-deb", "-c", os.path.abspath(deb_path)])
    if proc.returncode != 0:
        raise DebError("cannot list %s" % os.path.basename(deb_path))
    out = []
    for line in proc.stdout.decode("utf-8", "replace").split("\n"):
        entry = parse_list_line(line)
        if entry:
            out.append(entry)
    return out


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


def version_gt(a, b):
    """True if Debian version a is strictly greater than b (uses dpkg)."""
    for v in (a, b):
        if not VERSION_RE.match(v or ""):
            raise DebError("bad version string: %r" % (v,))
    rc = run_tool(["dpkg", "--compare-versions", a, "gt", b]).returncode
    if rc not in (0, 1):
        raise DebError("dpkg --compare-versions failed for %s and %s" % (a, b))
    return rc == 0
