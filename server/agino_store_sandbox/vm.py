# SPDX-License-Identifier: GPL-3.0-or-later
"""One throwaway KVM guest: qemu command line, run files, lifecycle."""

import os
import shutil
import subprocess
import time

from .qga import Qga
from .qmp import Qmp
from .util import InfraError, log

RUN_FILE_NAMES = ("overlay.qcow2", "vars.fd", "qmp.sock", "qga.sock", "shot.png", "shot.ppm", "qemu.log")


class RunFiles:
    """The explicit files one run creates inside its slot directory; only these are ever deleted."""

    def __init__(self, directory):
        self.directory = directory
        self.created = []

    def path(self, name):
        if name not in RUN_FILE_NAMES:
            raise ValueError("not a run file: %s" % name)
        if name not in self.created:
            self.created.append(name)
        return os.path.join(self.directory, name)

    def prepare(self):
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        for name in RUN_FILE_NAMES:  # leftovers of a crashed run carry our fixed names
            self._unlink(name)

    def _unlink(self, name):
        try:
            os.unlink(os.path.join(self.directory, name))
        except FileNotFoundError:
            pass

    def cleanup(self):
        for name in self.created:
            try:
                self._unlink(name)
            except OSError as e:
                log("cannot remove run file %s: %s" % (name, e.strerror))
        self.created = []


def tap_name(slot):
    return "agsbx-t%d" % slot


def mac_address(slot):
    return "52:54:00:5b:00:%02x" % slot


def build_argv(cfg, slot, files, deb_path, sandbox, demo_path=None):
    argv = [cfg.qemu, "-name", "agsbx-%d" % slot, "-enable-kvm", "-machine", "q35,accel=kvm", "-cpu", "host",
            "-m", str(cfg.memory_mb), "-smp", str(cfg.cpus), "-display", "none", "-no-user-config", "-nodefaults",
            "-device", "virtio-vga,xres=%d,yres=%d" % tuple(cfg.screen), "-rtc", "base=utc", "-no-reboot",
            "-drive", "if=pflash,format=raw,readonly=on,file=%s" % cfg.ovmf_code,
            "-drive", "if=pflash,format=raw,file=%s" % files.path("vars.fd"),
            "-drive", "file=%s,format=qcow2,if=none,id=sys,discard=unmap" % files.path("overlay.qcow2"),
            "-device", "virtio-blk-pci,drive=sys,bootindex=0"]
    if deb_path is not None:
        argv += ["-drive", "file=%s,format=raw,if=none,readonly=on,id=deb" % deb_path,
                 "-device", "virtio-blk-pci,drive=deb,serial=sbxdeb"]
    if demo_path is not None:
        argv += ["-drive", "file=%s,format=raw,if=none,readonly=on,id=demo" % demo_path,
                 "-device", "virtio-blk-pci,drive=demo,serial=sbxdemo"]
    argv += ["-netdev", "tap,id=n0,ifname=%s,script=no,downscript=no" % tap_name(slot),
             "-device", "virtio-net-pci,netdev=n0,mac=%s" % mac_address(slot),
             "-device", "virtio-serial-pci",
             "-chardev", "socket,id=qga,path=%s,server=on,wait=off" % files.path("qga.sock"),
             "-device", "virtserialport,chardev=qga,name=org.qemu.guest_agent.0",
             "-qmp", "unix:%s,server=on,wait=off" % files.path("qmp.sock")]
    if sandbox:
        argv += ["-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny"]
    return argv


def probe_sandbox(cfg):
    """True if this qemu build accepts -sandbox on (a machine-less dry run)."""
    try:
        done = subprocess.run([cfg.qemu, "-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,"
                               "resourcecontrol=deny", "-machine", "none", "-S", "-display", "none", "-nodefaults",
                               "-no-user-config", "-qmp", "stdio"], input=b"", capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


class Vm:
    def __init__(self, cfg, slot, deb_path=None, use_sandbox=False, demo_path=None):
        self.cfg, self.slot, self.deb_path, self.use_sandbox = cfg, slot, deb_path, use_sandbox
        self.demo_path = demo_path
        self.files = RunFiles(os.path.join(cfg.run, str(slot)))
        self.proc = None
        self.qmp = None
        self.qga = None

    def start(self):
        cfg, files = self.cfg, self.files
        files.prepare()
        self._run([cfg.qemu_img, "create", "-q", "-f", "qcow2", "-b", cfg.base, "-F", "qcow2",
                   files.path("overlay.qcow2")])
        try:
            shutil.copyfile(cfg.base_vars, files.path("vars.fd"))
        except OSError as e:
            raise InfraError("cannot copy OVMF vars: %s" % e.strerror) from None
        argv = build_argv(cfg, self.slot, files, self.deb_path, self.use_sandbox, self.demo_path)
        logf = open(files.path("qemu.log"), "wb")
        try:
            self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf)
        except OSError as e:
            raise InfraError("cannot start qemu: %s" % e.strerror) from None
        finally:
            logf.close()
        self.qmp = Qmp(self._wait_socket("qmp.sock"))
        self.qga = Qga(self._wait_socket("qga.sock"))

    def _run(self, argv):
        try:
            done = subprocess.run(argv, capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise InfraError("%s failed: %s" % (os.path.basename(argv[0]), e)) from None
        if done.returncode != 0:
            raise InfraError("%s failed: %s" % (os.path.basename(argv[0]), done.stderr.decode("utf-8", "replace")[:200]))

    def _wait_socket(self, name, timeout=30):
        path = self.files.path(name)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise InfraError("qemu exited early: %s" % self.log_tail())
            if os.path.exists(path):
                return path
            time.sleep(0.2)
        raise InfraError("qemu did not create %s" % name)

    def log_tail(self):
        try:
            with open(os.path.join(self.files.directory, "qemu.log"), "rb") as f:
                return f.read(400).decode("utf-8", "replace").strip().replace("\n", " ")
        except OSError:
            return ""

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        """Quit through QMP, then make sure the exact qemu process we started is gone; remove our run files."""
        try:
            if self.qmp is not None:
                self.qmp.quit()
            for client in (self.qga, self.qmp):
                if client is not None:
                    client.close()
            if self.proc is not None and self.proc.poll() is None:
                try:
                    self.proc.wait(10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
        finally:
            self.files.cleanup()
