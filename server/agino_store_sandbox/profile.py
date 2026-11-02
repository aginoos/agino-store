# SPDX-License-Identifier: GPL-3.0-or-later
"""install_profile validation, the same rules as the store helper (docs/design.md)."""

import re

DEBCONF_RE = re.compile(r"\A([^\s\x00-\x1f\x7f]+) ([^\s\x00-\x1f\x7f]+) (boolean|string|select) ([^\x00-\x1f\x7f]*)\Z")
DEFAULT_PATH_RE = re.compile(r"\A/etc/default/[a-z0-9][a-z0-9.+-]*\Z")
MAX_CONTENT = 4096
MAX_LINES = 200
MAX_FILES = 20


class ProfileError(ValueError):
    pass


def validate(profile):
    """(debconf lines, [(path, content bytes)]) of a profile, or ProfileError. None means empty."""
    if profile is None:
        return [], []
    if not isinstance(profile, dict) or set(profile) - {"debconf", "default_files"}:
        raise ProfileError("bad install_profile")
    lines = profile.get("debconf") or []
    files = profile.get("default_files") or []
    if not isinstance(lines, list) or not isinstance(files, list) or len(lines) > MAX_LINES or len(files) > MAX_FILES:
        raise ProfileError("bad install_profile")
    for line in lines:
        if not isinstance(line, str) or not DEBCONF_RE.match(line):
            raise ProfileError("bad debconf line")
    out = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "content"}:
            raise ProfileError("bad default_files entry")
        path, content = item["path"], item["content"]
        if not isinstance(path, str) or not DEFAULT_PATH_RE.match(path) or not isinstance(content, str):
            raise ProfileError("bad default_files entry")
        data = content.encode("utf-8")
        if len(data) > MAX_CONTENT or b"\0" in data:
            raise ProfileError("default_files content too large or binary")
        out.append((path, data))
    return list(lines), out
