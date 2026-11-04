# SPDX-License-Identifier: GPL-3.0-or-later
"""workspace.tar of an AI task (contract ai-intake 10.2 and 10.5).

The runner unpacks it to /work in the VM: TASK.md, CLAUDE.md and AGENTS.md (from intake_prompts/), inputs/
(the task's reading material and, in a correction round, the feedback and the previous delivery) and an empty
out/. Only regular files and directories go in: no symbolic links, hard links, absolute paths or `..`; the
finished tar is read back with the same check source.tar gets (upstream_fetch.verify_tar).
"""

import io
import os
import re
import tarfile
import tempfile

from . import debinfo, upstream_fetch
from .intake_materials import clean_block

PROMPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intake_prompts")
PROMPT_FILES = ("TASK.md", "CLAUDE.md", "AGENTS.md")
MAX_PROMPT_BYTES = 128 << 10
MAX_FEEDBACK_BYTES = 32 << 10
BLOCK_RE = re.compile(r"<!-- if:(\w+) -->\n(.*?)<!-- endif:\1 -->\n", re.DOTALL)
VAR_RE = re.compile(r"@@([A-Z_]+)@@")
FEEDBACK_NAMES = ("validation-error.txt", "replay-failure.log")


def read_prompt(name):
    """The text of one prompt template; OSError when the installation lacks it."""
    if name not in PROMPT_FILES:
        raise ValueError("unknown prompt %r" % name)
    with open(os.path.join(PROMPT_DIR, name), "rb") as f:
        data = f.read(MAX_PROMPT_BYTES + 1)
    if len(data) > MAX_PROMPT_BYTES:
        raise ValueError("prompt %s is too large" % name)
    return data.decode("utf-8")


def render(template, flags, values):
    """Keep the `<!-- if:NAME -->` blocks whose NAME is in `flags`, then replace @@VAR@@ by values[VAR]
    (a missing variable stays visible as @@VAR@@ so that a template mistake shows up in tests)."""
    text = BLOCK_RE.sub(lambda m: m.group(2) if m.group(1) in flags else "", template)
    return VAR_RE.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def one_line(value, limit=500):
    """A value for a single-line spot: control characters removed, whitespace squeezed, cut to `limit`."""
    return " ".join(debinfo.clean_text(value).split())[:limit]


def task_values(cfg, record):
    of = record.get("repair_of") or {}
    return {
        "ADDRESS": one_line(record.get("address") or ""),
        "HINT": one_line(record.get("hint") or "(none)", 1000),
        "APP": one_line(of.get("app") or ""), "VERSION": one_line(of.get("version") or ""),
        "MINUTES": str(cfg.intake_ai_minutes), "ROUND": str(record.get("rounds") or 0),
        "FEEDBACK_FILE": record.get("feedback_file") or "validation-error.txt",
    }


def task_text(cfg, record):
    flags = {record["kind"]}
    if record.get("rounds"):
        flags.add("retry")
    return render(read_prompt("TASK.md"), flags, task_values(cfg, record))


def _file(out, name, data):
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.type = len(data), 0o644, tarfile.REGTYPE
    out.addfile(info, io.BytesIO(data))


def _dir(out, name):
    info = tarfile.TarInfo(name)
    info.type, info.mode = tarfile.DIRTYPE, 0o755
    out.addfile(info)


def _copy_inputs(out, inputs_tar, skip):
    """Everything of inputs.tar (it holds `inputs/...`) except the members whose names are in `skip`."""
    names = set()
    with tarfile.open(inputs_tar, "r:*") as tar:
        for member in tar:
            if member.name in skip or not (member.isreg() or member.isdir()):
                continue
            names.add(member.name)
            out.addfile(member, tar.extractfile(member) if member.isreg() else None)
    return names


def build(cfg, record, inputs_tar, dest, feedback=None, previous=None):
    """Write the workspace tar `dest`.

    `feedback` is (file name, text) for a correction round (inputs/<name>); `previous` is {file name: bytes}, the
    files of the rejected delivery (inputs/previous/<name>). Returns the size of `dest`.
    """
    parts = {name: read_prompt(name) if name != "TASK.md" else task_text(cfg, record) for name in PROMPT_FILES}
    directory = os.path.dirname(dest)
    fd, tmp = tempfile.mkstemp(prefix=".workspace-", dir=directory)
    os.close(fd)
    try:
        with tarfile.open(tmp, "w", format=tarfile.PAX_FORMAT) as out:
            for name in PROMPT_FILES:
                _file(out, name, parts[name].encode("utf-8"))
            names = _copy_inputs(out, inputs_tar, ())
            if "inputs" not in names:
                _dir(out, "inputs")
            if feedback:
                _file(out, "inputs/" + feedback[0], clean_block(feedback[1])[:MAX_FEEDBACK_BYTES].encode("utf-8"))
            if previous:
                _dir(out, "inputs/previous")
                for name, data in sorted(previous.items()):
                    _file(out, "inputs/previous/" + name, data[:MAX_FEEDBACK_BYTES * 8])
            _dir(out, "out")
        limit = (cfg.intake_max_inputs_mib + 8) << 20
        upstream_fetch.verify_tar(tmp, limit)
        _refuse_links(tmp)
        os.replace(tmp, dest)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise
    return os.path.getsize(dest)


def _refuse_links(path):
    with tarfile.open(path, "r:*") as tar:
        for member in tar:
            if member.issym() or member.islnk() or not (member.isreg() or member.isdir()):
                raise upstream_fetch.UpstreamError("the workspace holds a link or special file: %s" % member.name[:100])
