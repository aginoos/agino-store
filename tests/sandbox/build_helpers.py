# SPDX-License-Identifier: GPL-3.0-or-later
"""Fakes shared by the build tests: config, task files, guest agent, VM, clock."""

import hashlib
import json
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server"))

from agino_store_sandbox import build_guest as G  # noqa: E402
from agino_store_sandbox import config  # noqa: E402
from agino_store_sandbox.qga import ExecResult  # noqa: E402
from agino_store_sandbox.util import InfraError  # noqa: E402

TASK_ID = "0123456789abcdef"
SOURCE = b"fake source tarball\n" * 50
DEB = b"!<arch>\ndebian-binary   2.0\n" + b"x" * 1000


def builder_cfg(tmp, **builder):
    values = {"queue": tmp + "/queue", "state": tmp + "/state", "run": tmp + "/run", "shot_dir": tmp + "/shots",
              "shots_out": tmp + "/shots-out", "reports": tmp + "/reports", "base": tmp + "/base.qcow2",
              "base_vars": tmp + "/base.vars", "ovmf_code": tmp + "/code.fd", "key": tmp + "/key.pem",
              "qemu_sandbox": "off"}
    opts = {"queue": tmp + "/bq", "out": tmp + "/bo", "base": tmp + "/builder.qcow2",
            "base_vars": tmp + "/builder.vars"}
    opts.update({k: str(v) for k, v in builder.items()})
    sess = {"runtime_dir": tmp + "/rt", "captures": tmp + "/captures", "min_free_gib": "0"}
    return config.from_dict(values, sess, opts)


def task_doc(**over):
    doc = {"app": "demo", "version": "1.0+agino1",
           "source": {"sha256": hashlib.sha256(SOURCE).hexdigest(), "size": len(SOURCE)},
           "recipe": {"base": "debian-trixie", "build_depends": ["build-essential", "libfoo-dev"],
                      "steps": ["./configure --prefix=/usr", "make -j4", "make install DESTDIR=$PWD/pkgroot"],
                      "package": {"name": "demo-agino", "section": "utils", "depends": ["libfoo1 (>= 1.2)"],
                                  "maintainer": "agino app store <store@aginoos.com>"}}}
    doc.update(over)
    return doc


def write_task(cfg, task_id=TASK_ID, doc=None, source=SOURCE, raw=None):
    """queue/<id>/{task.json,source.tar}; raw replaces the task.json bytes."""
    base = os.path.join(cfg.builder.queue, task_id)
    os.makedirs(base, exist_ok=True)
    with open(os.path.join(base, "task.json"), "wb") as f:
        f.write(raw if raw is not None else json.dumps(doc or task_doc()).encode())
    if source is not None:
        with open(os.path.join(base, "source.tar"), "wb") as f:
            f.write(source)
    return base


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.lock = threading.Lock()

    def __call__(self):
        return self.t

    def sleep(self, n):
        with self.lock:
            self.t += n


class FakeQga:
    """A guest agent that understands the exact argv shapes build_guest sends."""

    def __init__(self, vm):
        self.vm = vm
        self.files = {}
        self.modes = {}
        self.started = []          # argv of every start()
        self.results = {}          # pid -> ExecResult or None (still running)
        self.step_exit = {}        # step text -> exit code
        self.step_out = {}         # step text -> output bytes written to the step log
        self.hang_steps = set()
        self.apt_exit = {"update": 0, "install": 0}
        self.package_exit = 0
        self.deb = DEB
        self.corrupt_source = False
        self.stat_override = None
        self.fail_on = {}          # method name -> exception to raise
        self.execs = []

    def _guard(self, name):
        if self.vm.killed:
            raise InfraError("peer closed the socket")
        if name in self.fail_on:
            raise self.fail_on[name]

    def close(self):
        pass

    def wait_ready(self, timeout):
        self._guard("wait_ready")

    def sh(self, script, args=(), timeout=60, input_data=None):
        self._guard("sh")
        self.execs.append(("sh", script))
        return ExecResult(0, b"", b"")

    def write_file(self, path, data, mode="755"):
        self._guard("write_file")
        self.files[path] = data
        self.modes[path] = mode

    def write_stream(self, path, reader, mode="644", chunk=1 << 16):
        self._guard("write_stream")
        self.files[path] = reader.read()
        self.modes[path] = mode

    def exec(self, argv, timeout, input_data=None):
        self._guard("exec")
        self.execs.append(("exec", list(argv)))
        if argv[0] == "/usr/bin/sha256sum":
            data = self.files.get(argv[1], b"")
            if self.corrupt_source:
                data += b"!"
            return ExecResult(0, ("%s  %s\n" % (hashlib.sha256(data).hexdigest(), argv[1])).encode(), b"")
        if argv[0] == "/usr/bin/stat":
            if self.stat_override is not None:
                return ExecResult(0, self.stat_override.encode(), b"")
            return ExecResult(0, ("%d regular file\n" % len(self.deb)).encode(), b"")
        return ExecResult(0, b"", b"")

    def _finish(self, pid, code, log_path=None, out=b""):
        if log_path is not None:
            self.files[log_path] = self.files.get(log_path, b"") + out
        self.results[pid] = ExecResult(code, b"", b"")

    def start(self, argv, input_data=None, timeout=60):
        self._guard("start")
        self.started.append(list(argv))
        pid = 100 + len(self.started)
        if G.APT_GET in argv:
            label = "install" if "install" in argv else "update"
            log_path = argv[1]
            self._finish(pid, self.apt_exit[label], log_path, b"apt %s done\n" % label.encode())
        elif G.RUN_STEP in argv:
            i = argv.index(G.RUN_STEP)
            log_path, cmd = argv[i + 1], argv[i + 3]
            if cmd in self.hang_steps:
                self.results[pid] = None
            else:
                out = self.step_out.get(cmd, b"ran: " + cmd.encode() + b"\n")
                self._finish(pid, self.step_exit.get(cmd, 0), log_path, out)
        elif G.PACKAGE in argv:
            self._finish(pid, self.package_exit, argv[argv.index(G.PACKAGE) + 1], b"packaging\n")
        else:
            self.results[pid] = ExecResult(0, b"", b"")
        return pid

    def status(self, pid):
        self._guard("status")
        return self.results[pid]

    def read_chunk(self, path, offset, count):
        self._guard("read_chunk")
        if path == G.ARTIFACT:
            return self.deb[offset:offset + count]
        if path not in self.files:
            raise InfraError("guest agent guest-file-open failed: No such file or directory")
        return self.files[path][offset:offset + count]

    def step_commands(self):
        out = []
        for argv in self.started:
            if G.RUN_STEP in argv:
                out.append(argv[argv.index(G.RUN_STEP) + 3])
        return out


class FakeVm:
    def __init__(self, cfg=None, slot=0, limits=None):
        self.cfg, self.slot, self.limits = cfg, slot, limits
        self.killed = self.stopped = self.booted = False
        self.exit_now = False
        self.disk = 0
        self.qga = FakeQga(self)
        self.boot_error = None

    def boot(self):
        if self.boot_error:
            raise self.boot_error
        self.booted = True

    def exited(self):
        return self.exit_now

    def disk_bytes(self):
        return self.disk

    def log_tail(self):
        return "qemu: out of memory"

    def kill(self):
        self.killed = True

    def stop(self):
        self.stopped = True


class VmFactory:
    """Callable for Deps.vm_factory that remembers the VMs it made and lets a test configure each."""

    def __init__(self, configure=None):
        self.vms, self.configure, self.calls = [], configure, 0

    def __call__(self, cfg, slot, limits):
        self.calls += 1
        vm = FakeVm(cfg, slot, limits)
        if self.configure:
            self.configure(vm)
        self.vms.append(vm)
        return vm


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)
