# SPDX-License-Identifier: GPL-3.0-or-later
"""Everything the host does inside a build guest, all through the guest agent.

The scripts below are fixed texts. Recipe data (step text, package names, paths) only ever travels as separate
argv elements, never inside a script and never through a shell the host builds. The guest is untrusted: its
output is copied into the log through LogSink, sizes are checked before anything is read, and the artifact
is read into a file whose name the host chose.
"""

import re
import time

from .qga import ExecResult
from .util import InfraError, SandboxError

SCRIPT_DIR = "/opt/agino-build"
RUN_STEP = SCRIPT_DIR + "/run-step.sh"
LOGGED = SCRIPT_DIR + "/logged.sh"
PACKAGE = SCRIPT_DIR + "/package.sh"
PLACE_FILE = SCRIPT_DIR + "/place-file.sh"
RUN_TEST = SCRIPT_DIR + "/run-test.sh"
HOME = "/home/builder"
SRC_TAR = HOME + "/source.tar"
SRC_DIR = HOME + "/src"
CONTROL = HOME + "/control"
ARTIFACT = HOME + "/out/pkg.deb"
USER_LOGS = HOME + "/logs"
ROOT_LOGS = "/var/log/agino-build"
APT_LIST = "/etc/apt/agino-build.list"
RUNUSER = "/usr/sbin/runuser"
ENV_BIN = "/usr/bin/env"
APT_GET = "/usr/bin/apt-get"
USER_ENV = ["HOME=" + HOME, "USER=builder", "LOGNAME=builder", "LANG=C.UTF-8", "TERM=dumb",
            "PATH=/usr/local/bin:/usr/bin:/bin", "DEBIAN_FRONTEND=noninteractive"]
ROOT_ENV = ["HOME=/root", "LANG=C.UTF-8", "TERM=dumb", "PATH=/usr/sbin:/usr/bin:/sbin:/bin",
            "DEBIAN_FRONTEND=noninteractive"]

STATUS_POLL = 1.0
LOG_INTERVAL = 2.0
CHUNK = 1 << 20
PUMP_ROUNDS = 4
DRAIN_ROUNDS = 16
DEB_MAGIC = b"!<arch>\n"

PREPARE_SCRIPT = """set -eu
uid=$(id -u builder)
[ "$uid" -ne 0 ]
install -d -m 0755 /opt/agino-build /var/log/agino-build
install -d -m 0755 -o builder -g builder /home/builder/src /home/builder/logs /home/builder/out
"""

# Runs a command as root with stdout/stderr appended to the log named by $1.
LOGGED_SCRIPT = """#!/bin/sh
log=$1
shift
exec >>"$log" 2>&1
exec "$@"
"""

# Runs one recipe step as the unprivileged user: $1 log, $2 seconds, $3 the step text (a single argument).
RUN_STEP_SCRIPT = """#!/bin/sh
exec >>"$1" 2>&1
cd /home/builder/src || exit 125
exec /usr/bin/timeout -k 10 "$2" /bin/sh -c "$3"
"""

# The fixed packaging step. The control file comes from the host (built from the recipe's package section);
# an upstream DEBIAN directory, or an upstream control file, is never accepted.
PACKAGE_SCRIPT = """#!/bin/sh
exec >>"$1" 2>&1
set -eu
cd /home/builder/src
if [ -L pkgroot ] || [ ! -d pkgroot ]; then
    echo "pkgroot is missing or not a directory"
    exit 20
fi
if [ -e pkgroot/DEBIAN ] || [ -L pkgroot/DEBIAN ]; then
    echo "pkgroot contains a DEBIAN directory: maintainer scripts and control files are not accepted"
    exit 21
fi
size=$(du -sk --apparent-size pkgroot | cut -f1)
mkdir -m 0755 pkgroot/DEBIAN
{ cat /home/builder/control; echo "Installed-Size: $size"; } > pkgroot/DEBIAN/control
chmod 0644 pkgroot/DEBIAN/control
rm -f /home/builder/out/pkg.deb
dpkg-deb --root-owner-group -Zxz --build pkgroot /home/builder/out/pkg.deb
"""

# Recipe v2 `files`: one file under the source root, written as the unprivileged user (the source tree came from the
# untrusted upstream, root must never follow its links). $1 relative path (validated by the host), $2 mode, the
# content arrives on stdin.
PLACE_FILE_SCRIPT = """#!/bin/sh
set -eu
cd /home/builder/src
mkdir -p -- "$(dirname -- "$1")"
cat > "$1"
chmod "$2" "$1"
"""

# Recipe v2 `test`: one command under a virtual X server as the unprivileged user: $1 log, $2 seconds, $3 the command.
RUN_TEST_SCRIPT = """#!/bin/sh
exec >>"$1" 2>&1
cd /home/builder || exit 125
exec /usr/bin/timeout -k 10 "$2" /usr/bin/xvfb-run -a /bin/sh -c "$3"
"""

SCRIPTS = ((LOGGED, LOGGED_SCRIPT), (RUN_STEP, RUN_STEP_SCRIPT), (PACKAGE, PACKAGE_SCRIPT))
EXTRA_SCRIPTS = ((PLACE_FILE, PLACE_FILE_SCRIPT), (RUN_TEST, RUN_TEST_SCRIPT))
_SHA_LINE = re.compile(r"\A([0-9a-f]{64})  ")


class BuildStepError(SandboxError):
    """The build itself failed (as opposed to the sandbox): a step, the packaging or the artifact is bad.

    `klass` is the failure class of result.json: "recipe" (the default: a step, test, packaging or source problem),
    "infra" (VM, resources), "timeout" or "model"."""

    def __init__(self, message, exitcode=1, klass="recipe"):
        super().__init__(message)
        self.exitcode = exitcode
        self.klass = klass


def render_control(task):
    """The DEBIAN/control text, from validated recipe fields only (Installed-Size is added in the guest)."""
    pkg = task.package
    lines = ["Package: " + pkg.name, "Version: " + task.version, "Architecture: amd64",
             "Maintainer: " + pkg.maintainer, "Section: " + pkg.section, "Priority: optional"]
    if pkg.depends:
        lines.append("Depends: " + ", ".join(pkg.depends))
    lines.append("Description: %s built from source by the agino app store" % task.app)
    return "\n".join(lines) + "\n"


def as_builder(inner, env=()):
    """argv that runs `inner` as the unprivileged user with a fixed minimal environment (plus `NAME=value` items)."""
    return [RUNUSER, "-u", "builder", "--", ENV_BIN, "-i", *USER_ENV, *env, *inner]


class GuestBuild:
    """Drives one build guest. `check` is called often and raises to stop the job (cancel, limits, dead VM)."""

    def __init__(self, qga, sink, check, clock=time.monotonic, sleep=time.sleep):
        self.qga, self.sink, self.check, self.clock, self.sleep = qga, sink, check, clock, sleep
        self.apt_updated = False

    # ---- setup ----

    def prepare(self, control_text, apt_source, extra_scripts=()):
        res = self.qga.sh(PREPARE_SCRIPT, timeout=60)
        if not res.ok:
            raise InfraError("cannot prepare the build guest (exit %d)" % res.exitcode)
        for path, text in SCRIPTS + tuple(extra_scripts):
            self.qga.write_file(path, text.encode("utf-8"), "755")
        self.qga.write_file(CONTROL, control_text.encode("utf-8"), "644")
        self.qga.write_file(APT_LIST, ("deb " + apt_source + "\n").encode("utf-8"), "644")

    def upload_source(self, fileobj, sha256):
        """Copy the tarball in and compare the guest's sha256 with the one the host verified."""
        self.qga.write_stream(SRC_TAR, fileobj, "644")
        res = self.qga.exec(["/usr/bin/sha256sum", SRC_TAR], 600)
        m = _SHA_LINE.match(res.text) if res.ok else None
        if not m or m.group(1) != sha256:
            raise InfraError("the source tarball arrived damaged in the guest")

    def extract_source(self):
        res = self.qga.exec(as_builder(["/bin/tar", "-xf", SRC_TAR, "-C", SRC_DIR, "--no-same-owner"]), 600)
        if not res.ok:
            self.sink.feed(res.out + res.err)
            raise BuildStepError("cannot unpack the source tarball (exit %d)" % res.exitcode, res.exitcode)

    def place_files(self, files):
        """Recipe v2 `files`: write each into /home/builder/src as the unprivileged user and set its mode."""
        for item in files:
            argv = as_builder([PLACE_FILE, item.path, item.mode])
            res = self.qga.exec(argv, 60, input_data=item.content.encode("utf-8"))
            if not res.ok:
                self.sink.feed(res.out + res.err)
                raise BuildStepError("cannot write the recipe file %s (exit %d)" % (item.path[:100], res.exitcode),
                                     res.exitcode)

    # ---- logged commands ----

    def exec_logged(self, argv, log_path, hard_timeout):
        """Start argv (absolute path first) and follow its log file into the build log until it exits.

        The command itself is wrapped in `timeout` by the caller; `hard_timeout` is the host's own backstop."""
        pid = self.qga.start(argv)
        offset, last = 0, self.clock()
        deadline = self.clock() + hard_timeout
        while True:
            self.check()
            res = self.qga.status(pid)
            if res is not None:
                self._pump(log_path, offset, DRAIN_ROUNDS)
                if res.out or res.err:
                    self.sink.feed(res.out + res.err)
                return res
            if self.clock() - last >= LOG_INTERVAL:
                offset, last = self._pump(log_path, offset, PUMP_ROUNDS), self.clock()
            if self.clock() > deadline:
                return ExecResult(-1, b"", b"", timed_out=True)
            self.sleep(STATUS_POLL)

    def _pump(self, path, offset, rounds):
        """Copy new bytes of the guest log file to the build log; returns the new offset."""
        while rounds > 0 and not self.sink.full:
            try:
                data = self.qga.read_chunk(path, offset, CHUNK)
            except InfraError as e:
                if offset == 0 and "guest-file-open" in str(e):
                    return offset  # the command has not created its log yet
                raise
            if not data:
                break
            self.sink.feed(data)
            offset += len(data)
            rounds -= 1
        return offset

    def install_deps(self, depends, timeout):
        """apt-get update and install against the one configured source; returns the failing ExecResult or None."""
        base = [APT_GET, "-o", "Dir::Etc::sourcelist=" + APT_LIST, "-o", "Dir::Etc::sourceparts=-",
                "-o", "Acquire::Languages=none"]
        steps = [("update", base + ["update"])]
        if depends:
            steps.append(("install", base + ["install", "-y", "--no-install-recommends", *depends]))
        for label, argv in steps:
            log_path = "%s/apt-%s.log" % (ROOT_LOGS, label)
            wrapped = [LOGGED, log_path, "/usr/bin/timeout", "-k", "10", str(int(timeout)), ENV_BIN, "-i",
                       *ROOT_ENV, *argv]
            self.sink.note("== apt-get %s ==" % label)
            res = self.exec_logged(wrapped, log_path, timeout + 60)
            if not res.ok:
                return label, res
            self.apt_updated = self.apt_updated or label == "update"
        return None, None

    def run_step(self, n, cmd, timeout, env=()):
        """One recipe step as the builder user; the text is a single argv element of `sh -c`."""
        log_path = "%s/step-%d.log" % (USER_LOGS, n)
        argv = as_builder([RUN_STEP, log_path, str(int(timeout)), cmd], env)
        return self.exec_logged(argv, log_path, timeout + 60)

    def install_package(self, timeout):
        """Test phase: install the built deb as root (dependencies from the one configured source).

        Returns (label, ExecResult) of the first failing apt call, else (None, None)."""
        base = [APT_GET, "-o", "Dir::Etc::sourcelist=" + APT_LIST, "-o", "Dir::Etc::sourceparts=-",
                "-o", "Acquire::Languages=none"]
        calls = [] if self.apt_updated else [("update", base + ["update"])]
        calls.append(("install-package", base + ["-o", "APT::Sandbox::User=root", "install", "-y",
                                                 "--no-install-recommends", ARTIFACT]))
        for label, argv in calls:
            log_path = "%s/apt-%s.log" % (ROOT_LOGS, label)
            wrapped = [LOGGED, log_path, "/usr/bin/timeout", "-k", "10", str(int(timeout)), ENV_BIN, "-i",
                       *ROOT_ENV, *argv]
            self.sink.note("== apt-get %s ==" % label)
            res = self.exec_logged(wrapped, log_path, timeout + 60)
            if not res.ok:
                return label, res
            self.apt_updated = self.apt_updated or label == "update"
        return None, None

    def run_test(self, n, cmd, timeout, env=()):
        """One recipe `test` command under xvfb-run as the builder user."""
        log_path = "%s/test-%d.log" % (USER_LOGS, n)
        argv = as_builder([RUN_TEST, log_path, str(int(timeout)), cmd], env)
        return self.exec_logged(argv, log_path, timeout + 60)

    def package(self, timeout):
        log_path = USER_LOGS + "/package.log"
        argv = ["/usr/bin/timeout", "-k", "10", str(int(timeout))] + as_builder([PACKAGE, log_path])
        return self.exec_logged(argv, log_path, timeout + 60)

    # ---- artifact ----

    def artifact_size(self):
        """Size of the packaged deb, which must be a regular file."""
        res = self.qga.exec(["/usr/bin/stat", "-c", "%s %F", ARTIFACT], 30)
        parts = res.text.strip().split(" ", 1)
        if not res.ok or len(parts) != 2 or not parts[0].isdigit() or parts[1] != "regular file":
            raise BuildStepError("the build produced no deb", 22)
        return int(parts[0])

    def read_artifact(self, writer, size, max_bytes):
        """Copy the deb into `writer` in chunks; refuses oversized files before reading and bad data after."""
        if size > max_bytes:
            raise BuildStepError("the deb is %d MiB, over the limit of %d MiB" % (size >> 20, max_bytes >> 20), 23)
        offset = 0
        while offset < size:
            self.check()
            data = self.qga.read_chunk(ARTIFACT, offset, min(CHUNK, size - offset))
            if not data:
                raise InfraError("the deb ended after %d of %d bytes" % (offset, size))
            if offset == 0 and not data.startswith(DEB_MAGIC):
                raise BuildStepError("the build output is not a deb archive", 24)
            writer.write(data)
            offset += len(data)
