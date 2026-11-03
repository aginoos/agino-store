# SPDX-License-Identifier: GPL-3.0-or-later
"""The VM of an interactive session: qemu arguments for VNC and input devices, and two overlay layers.

Layer 1 (overlay.qcow2) sits on the read-only base image and receives the installation.
Layer 2 (overlay2.qcow2) sits on layer 1 and is what the reviewer works in; dropping it and creating a
new one is the rollback (back to "just installed"). The sockets live in a private 0700 directory,
away from the group-visible web.sock directory.
"""

import os
import shutil

from .util import InfraError
from .vm import RunFiles, Vm, build_argv

PRIVATE_NAMES = ("qmp.sock", "qga.sock", "vnc.sock", "shot.ppm")
DISK_WRITE_BPS = 50 << 20
DISK_WRITE_IOPS = 3000
DRIVE_OPTS = ",throttling.bps-write=%d,throttling.iops-write=%d" % (DISK_WRITE_BPS, DISK_WRITE_IOPS)
INPUT_DEVICES = ["-device", "virtio-keyboard-pci", "-device", "virtio-tablet-pci"]


class SessionRunFiles(RunFiles):
    """RunFiles with the sockets and the screendump in a second (private) directory."""

    def __init__(self, directory, private):
        super().__init__(directory)
        self.private = private

    def where(self, name):
        return self.private if name in PRIVATE_NAMES else self.directory

    def prepare(self):
        os.makedirs(self.private, mode=0o700, exist_ok=True)
        os.chmod(self.private, 0o700)
        super().prepare()


def session_extra(files):
    """qemu arguments that only sessions add: the VNC unix socket (no TCP) and virtio input devices."""
    return ["-vnc", "unix:%s" % files.path("vnc.sock")] + INPUT_DEVICES


class SessionVm(Vm):
    def __init__(self, cfg, slot, private_dir, deb_path=None, use_sandbox=False):
        super().__init__(cfg, slot, deb_path, use_sandbox, None)
        if "," in private_dir or "," in cfg.run:
            raise InfraError("run directories must not contain a comma")
        self.files = SessionRunFiles(os.path.join(cfg.run, str(slot)), private_dir)
        self.layer = 0

    def _spawn_layer(self, drive, vars_name, deb_path):
        argv = build_argv(self.cfg, self.slot, self.files, deb_path, self.use_sandbox, None, drive=drive,
                          vars_name=vars_name, drive_opts=DRIVE_OPTS, extra=session_extra(self.files))
        self._spawn(argv)

    def boot_install(self):
        """Layer 1 on the base image, with the deb attached; run files of an earlier crashed run are removed."""
        self.files.prepare()
        self._run([self.cfg.qemu_img, "create", "-q", "-f", "qcow2", "-b", self.cfg.base, "-F", "qcow2",
                   self.files.path("overlay.qcow2")])
        self._copy(self.cfg.base_vars, "vars.fd")
        self.layer = 1
        self._spawn_layer("overlay.qcow2", "vars.fd", self.deb_path)

    def boot_run(self):
        """A fresh layer 2 on the installed layer 1 (the rollback point); any earlier layer 2 is dropped."""
        if self.alive():
            raise InfraError("the VM must be stopped before a new layer is created")
        for name in ("overlay2.qcow2", "vars2.fd"):
            self.files._unlink(name)
        self._run([self.cfg.qemu_img, "create", "-q", "-f", "qcow2", "-b", self.files.path("overlay.qcow2"),
                   "-F", "qcow2", self.files.path("overlay2.qcow2")])
        self._copy(self.files.path("vars.fd"), "vars2.fd")
        self.layer = 2
        self._spawn_layer("overlay2.qcow2", "vars2.fd", None)

    def rollback(self):
        self.halt()
        self.boot_run()

    def _copy(self, source, name):
        try:
            shutil.copyfile(source, self.files.path(name))
        except OSError as e:
            raise InfraError("cannot copy OVMF vars: %s" % e.strerror) from None

    def disk_bytes(self):
        """Bytes both overlay layers occupy now."""
        total = 0
        for name in ("overlay.qcow2", "overlay2.qcow2"):
            try:
                total += os.stat(os.path.join(self.files.where(name), name)).st_size
            except OSError:
                pass
        return total

    def exited(self):
        return self.proc is not None and self.proc.poll() is not None

    def kill(self):
        """Kill exactly the qemu process we started (used to interrupt a blocked call)."""
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
