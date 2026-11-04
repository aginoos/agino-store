# SPDX-License-Identifier: GPL-3.0-or-later
"""The VM of an interactive session: one qemu that lives for the whole session and is reused.

One overlay (overlay.qcow2) sits on the read-only base image. Once the desktop is up, the memory and
disk state is saved as the snapshot `clean` (QMP snapshot-save over the system disk node `sysn` and the
OVMF variables node `vars`, which therefore is a qcow2 file); rolling back is snapshot-load of it, so no
reboot is needed. A vendor deb is hot-plugged as a read-only disk on a PCIe root port only while an app is
installed, and unplugged before the next snapshot-load. The sockets live in a private 0700 directory, away
from the group-visible web.sock directory.
"""

import os
import time

from .util import InfraError
from .vm import RunFiles, Vm, build_argv

PRIVATE_NAMES = ("qmp.sock", "qga.sock", "vnc.sock", "shot.ppm")
DISK_WRITE_BPS = 50 << 20
DISK_WRITE_IOPS = 3000
DRIVE_OPTS = ",throttling.bps-write=%d,throttling.iops-write=%d" % (DISK_WRITE_BPS, DISK_WRITE_IOPS)
INPUT_DEVICES = ["-device", "virtio-keyboard-pci", "-device", "virtio-tablet-pci"]
# Fixed high slot on pcie.0: auto-placed devices (and so the guest's interface names) keep their addresses.
DEB_PORT = ["-device", "pcie-root-port,id=debport,bus=pcie.0,addr=0x1e,chassis=1"]
SNAPSHOT_TAG = "clean"
SNAPSHOT_DEVICES = ["sysn", "vars"]
SNAPSHOT_VMSTATE = "sysn"
JOB_TIMEOUT = 180.0
JOB_POLL = 0.2
UNPLUG_TIMEOUT = 30.0


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
    """qemu arguments that only sessions add: the VNC unix socket (no TCP), virtio input, the deb hot-plug port."""
    return ["-vnc", "unix:%s" % files.path("vnc.sock")] + INPUT_DEVICES + DEB_PORT


class SessionVm(Vm):
    def __init__(self, cfg, slot, private_dir, use_sandbox=False, sleep=time.sleep, clock=time.monotonic):
        super().__init__(cfg, slot, None, use_sandbox, None)
        if "," in private_dir or "," in cfg.run:
            raise InfraError("run directories must not contain a comma")
        self.files = SessionRunFiles(os.path.join(cfg.run, str(slot)), private_dir)
        self.sleep, self.clock = sleep, clock
        self.deb_plugged = False
        self._job_ids = 0

    def boot(self):
        """Overlay on the base image, OVMF variables as qcow2, then qemu; run files of a crashed run are removed."""
        self.files.prepare()
        self._run([self.cfg.qemu_img, "create", "-q", "-f", "qcow2", "-b", self.cfg.base, "-F", "qcow2",
                   self.files.path("overlay.qcow2")])
        self._run([self.cfg.qemu_img, "convert", "-f", "raw", "-O", "qcow2", self.cfg.base_vars,
                   self.files.path("vars.qcow2")])
        argv = build_argv(self.cfg, self.slot, self.files, None, self.use_sandbox, None, vars_name="vars.qcow2",
                          drive_opts=DRIVE_OPTS, extra=session_extra(self.files), snapshot_vm=True)
        self._spawn(argv)

    # ---- snapshot ----

    def snapshot_save(self):
        self._job("snapshot-save")

    def snapshot_load(self):
        """Back to the saved state; the VM is running afterwards."""
        if self.deb_plugged:
            raise InfraError("the deb disk must be unplugged before a snapshot is loaded")
        self._job("snapshot-load")
        status = self.qmp.execute("query-status")
        if not isinstance(status, dict) or status.get("status") != "running":
            self.qmp.execute("cont")

    def _job(self, command):
        """Run a snapshot job and wait for it: query-jobs until concluded, job-dismiss, error check."""
        self._job_ids += 1
        job_id = "%s%d" % (command, self._job_ids)
        self.qmp.execute(command, {"job-id": job_id, "tag": SNAPSHOT_TAG, "vmstate": SNAPSHOT_VMSTATE,
                                   "devices": list(SNAPSHOT_DEVICES)})
        deadline = self.clock() + JOB_TIMEOUT
        while True:
            jobs = self.qmp.execute("query-jobs")
            job = next((j for j in jobs if isinstance(j, dict) and j.get("id") == job_id), None) \
                if isinstance(jobs, list) else None
            if job is None:
                raise InfraError("%s: job %s is not listed" % (command, job_id))
            if job.get("status") == "concluded":
                error = job.get("error")
                self.qmp.execute("job-dismiss", {"id": job_id})
                if error:
                    raise InfraError("%s failed: %s" % (command, str(error)[:200]))
                return
            if self.clock() > deadline:
                raise InfraError("%s did not finish within %d s" % (command, JOB_TIMEOUT))
            self.sleep(JOB_POLL)

    # ---- vendor deb disk ----

    def plug_deb(self, path):
        """Hot-plug the deb file as a read-only virtio disk with the serial the guest looks for."""
        if self.deb_plugged:
            raise InfraError("a deb disk is already plugged")
        self.qmp.execute("blockdev-add", {"node-name": "deb", "driver": "raw", "read-only": True,
                                          "file": {"driver": "file", "filename": path, "read-only": True}})
        try:
            self.qmp.execute("device_add", {"driver": "virtio-blk-pci", "id": "debdev", "drive": "deb",
                                            "serial": "sbxdeb", "bus": "debport"})
        except InfraError:
            self.qmp.execute("blockdev-del", {"node-name": "deb"})
            raise
        self.deb_plugged = True

    def unplug_deb(self):
        """Remove the deb disk again; blockdev-del succeeds only once the guest has let the device go."""
        if not self.deb_plugged:
            return
        self.qmp.execute("device_del", {"id": "debdev"})
        deadline = self.clock() + UNPLUG_TIMEOUT
        while True:
            try:
                self.qmp.execute("blockdev-del", {"node-name": "deb"})
                break
            except InfraError:
                if self.clock() > deadline:
                    raise InfraError("the guest did not release the deb disk") from None
                self.sleep(JOB_POLL)
        self.deb_plugged = False

    # ---- misc ----

    def disk_bytes(self):
        """Bytes the overlay occupies now (the saved snapshot state is inside it)."""
        try:
            return os.stat(self.files.path("overlay.qcow2")).st_size
        except OSError:
            return 0

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
