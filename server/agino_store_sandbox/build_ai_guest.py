# SPDX-License-Identifier: GPL-3.0-or-later
"""What the host does inside an AI-mode guest (docs/sandbox.md section 10.7), all through the guest agent.

Like build_guest.py: the scripts are fixed texts, variable parts travel as argv elements, and everything the
guest says is untrusted. The agent runs as `builder` in /work with a minimal environment; when it is over every
process of that user is killed, and only then are the deliverables listed and read (regular files with a white-listed
name, sizes checked before reading).
"""

import re

from .build_guest import APT_LIST, HOME, SCRIPT_DIR, USER_LOGS, BuildStepError, GuestBuild, as_builder
from .util import InfraError

WORK = "/work"
OUT_DIR = WORK + "/out"
TASK_FILE = WORK + "/TASK.md"
WORKSPACE_TAR = HOME + "/workspace.tar"
AI_RUN = SCRIPT_DIR + "/ai-run.sh"
AGENT_LOG = USER_LOGS + "/agent.log"
ENVIRONMENT_FILE = "/etc/environment"
APT_CONF = "/etc/apt/apt.conf.d/99agino-build"
CODEX_CONFIG = "/home/builder/.codex/config.toml"
# The AI session installs build dependencies and its own deb with apt-get and dpkg. Only this guest gets the rule
# (the image has the sudo program but no rule): the replay guests, whose steps come from a recipe, stay unprivileged.
SUDOERS_AI = "/etc/sudoers.d/agino-ai"
SUDOERS_AI_TEXT = "builder ALL=(root) NOPASSWD: ALL\n"

TEXT_FILES = ("recipe.yaml", "app.yaml", "upstream.yaml", "vendor.yaml", "shots.yaml", "NOTES.md")
DEMO_TAR = "demo.tar"          # the small demo data of the screenshot scenes; the server checks every member
DEMO_TAR_MAX = 4 << 20
DEB_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9+.~_-]{0,150}\.deb\Z")
MAX_TEXT_BYTES = 256 * 1024
MAX_LISTED = 500
CHUNK = 1 << 20
DEB_MAGIC = b"!<arch>\n"

AI_PREPARE_SCRIPT = """set -eu
uid=$(id -u builder)
[ "$uid" -ne 0 ]
install -d -m 0755 /opt/agino-build /var/log/agino-build
install -d -m 0755 -o builder -g builder /home/builder/logs /home/builder/.codex /work
"""

# The agent: $1 log (output and errors), $2 seconds, $3 the shell snippet from the configuration. `timeout` puts the
# command in its own process group and signals the whole group: TERM at the limit, KILL ten seconds later.
AI_RUN_SCRIPT = """#!/bin/sh
exec >>"$1" 2>&1
cd /work || exit 125
exec /usr/bin/timeout -k 10 "$2" /bin/sh -c "$3"
"""

# After the agent: remove everything that could still change the files, then list the regular files (not links)
# directly in /work/out as "size TAB name NUL". `find -type f` does not follow links, not even the directory.
AI_COLLECT_SCRIPT = """kill_all() { /usr/sbin/runuser -u builder -- /bin/sh -c 'kill -KILL -1' || true; }
kill_all
sleep 1
kill_all
sleep 1
find /work/out -mindepth 1 -maxdepth 1 -type f -printf '%s\\t%f\\0'
"""

AI_SCRIPTS = ((AI_RUN, AI_RUN_SCRIPT),)

APT_CONF_TEXT = ('Dir::Etc::sourcelist "%s";\nDir::Etc::sourceparts "-";\n' % APT_LIST)


class BadDeliverable(BuildStepError):
    """One delivered file is unusable (not a deb, not text); the others are still taken."""


def deliverable_kind(name):
    """"text", "demo", "deb" or None: whether the guest's file name is one we take."""
    if name in TEXT_FILES:
        return "text"
    if name == DEMO_TAR:
        return "demo"
    return "deb" if DEB_NAME_RE.match(name) else None


def parse_listing(raw):
    """[(name, size)] from the collect script's output; anything malformed or not white-listed is dropped."""
    found, seen = [], set()
    for record in raw.split(b"\0")[:MAX_LISTED]:
        size, tab, name = record.partition(b"\t")
        try:
            text_name = name.decode("ascii")
        except UnicodeDecodeError:
            continue
        if tab and size.isdigit() and len(size) < 16 and deliverable_kind(text_name) and text_name not in seen:
            seen.add(text_name)  # the first record of a name wins
            found.append((text_name, int(size)))
    return found


class AiGuest(GuestBuild):
    """The AI-mode steps of one guest. `sink` is the AiStream that turns agent output into log summaries;
    `log` is the real log sink for the runner's own lines."""

    def __init__(self, qga, sink, log, check, clock, sleep):
        super().__init__(qga, sink, check, clock, sleep)
        self.log = log

    # ---- setup ----

    def prepare_ai(self, apt_source, env_items, agent, codex_toml):
        res = self.qga.sh(AI_PREPARE_SCRIPT, timeout=60)
        if not res.ok:
            raise InfraError("cannot prepare the AI guest (exit %d)" % res.exitcode)
        for path, text in AI_SCRIPTS:
            self.qga.write_file(path, text.encode("utf-8"), "755")
        self.qga.write_file(SUDOERS_AI, SUDOERS_AI_TEXT.encode("utf-8"), "440")
        self.qga.write_file(APT_LIST, ("deb " + apt_source + "\n").encode("utf-8"), "644")
        self.qga.write_file(APT_CONF, APT_CONF_TEXT.encode("utf-8"), "644")
        self.qga.write_file(ENVIRONMENT_FILE, ("\n".join(env_items) + "\n").encode("utf-8"), "644")
        if agent == "codex":
            self.qga.write_file(CODEX_CONFIG, codex_toml.encode("utf-8"), "600")
            if not self.qga.exec(["/bin/chown", "builder:builder", CODEX_CONFIG], 10).ok:
                raise InfraError("cannot hand the Codex configuration to the builder user")

    def upload_workspace(self, fileobj, sha256):
        """Copy workspace.tar in, compare the guest's sha256 with the one the host computed, unpack it into /work as
        the unprivileged user and make sure the output directory exists."""
        self.qga.write_stream(WORKSPACE_TAR, fileobj, "644")
        res = self.qga.exec(["/usr/bin/sha256sum", WORKSPACE_TAR], 600)
        if not res.ok or res.text[:64] != sha256:
            raise InfraError("the workspace tarball arrived damaged in the guest")
        res = self.qga.exec(as_builder(["/bin/tar", "-xf", WORKSPACE_TAR, "-C", WORK, "--no-same-owner"]), 600)
        if not res.ok:
            self.log.feed(res.out + res.err)
            raise BuildStepError("cannot unpack workspace.tar (exit %d)" % res.exitcode, res.exitcode, "infra")
        res = self.qga.exec(as_builder(["/bin/mkdir", "-p", OUT_DIR]), 30)
        if not res.ok or not self.qga.exec(["/usr/bin/test", "-f", TASK_FILE], 10).ok:
            raise BuildStepError("workspace.tar has no TASK.md", 1, "infra")

    # ---- the agent ----

    def run_agent(self, snippet, seconds, env_items):
        """Start the CLI as `builder` and follow its output until it exits or the host backstop passes."""
        argv = as_builder([AI_RUN, AGENT_LOG, str(int(seconds)), snippet], env_items)
        return self.exec_logged(argv, AGENT_LOG, seconds + 10 + 45)

    # ---- deliverables ----

    def list_deliverables(self):
        """[(name, size)] of the white-listed regular files in /work/out, after the agent's processes are gone."""
        res = self.qga.sh(AI_COLLECT_SCRIPT, timeout=90)
        if not res.ok:
            raise InfraError("cannot list the agent's output (exit %d)" % res.exitcode)
        return parse_listing(res.out)

    def read_deliverable(self, name, size, writer):
        """Copy /work/out/<name> (exactly `size` bytes, as listed) into `writer`; the caller checked the limits."""
        path = "%s/%s" % (OUT_DIR, name)
        offset, kind = 0, deliverable_kind(name)
        while offset < size:
            self.check()
            data = self.qga.read_chunk(path, offset, min(CHUNK, size - offset))
            if not data:
                raise InfraError("%s ended after %d of %d bytes" % (name, offset, size))
            if offset == 0 and kind == "deb" and not data.startswith(DEB_MAGIC):
                raise BadDeliverable("the delivered deb is not a deb archive", 24)
            if kind == "demo" and offset == 0 and len(data) >= 262 and data[257:262] != b"ustar":
                raise BadDeliverable("demo.tar is not a tar archive", 1)
            if kind == "text" and b"\0" in data:
                raise BadDeliverable("%s is not a text file" % name, 1)
            writer.write(data)
            offset += len(data)
