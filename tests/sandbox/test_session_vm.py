# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from session_helpers import session_cfg  # noqa: E402

from agino_store_sandbox import session_vm as V  # noqa: E402
from agino_store_sandbox.util import InfraError  # noqa: E402


class ScriptedQmp:
    """Answers QMP commands from a table; `jobs` is the sequence of query-jobs answers."""

    def __init__(self):
        self.calls = []
        self.jobs = []
        self.status = "running"
        self.fail = {}
        self.busy_blockdev_del = 0

    def execute(self, command, arguments=None, timeout=30):
        self.calls.append((command, arguments))
        if command in self.fail:
            raise InfraError("QMP %s failed: %s" % (command, self.fail[command]))
        if command == "query-jobs":
            return self.jobs.pop(0) if len(self.jobs) > 1 else self.jobs[0]
        if command == "query-status":
            return {"status": self.status}
        if command == "blockdev-del" and self.busy_blockdev_del:
            self.busy_blockdev_del -= 1
            raise InfraError("QMP blockdev-del failed: Node 'deb' is in use")
        return {}

    def names(self):
        return [c for c, _ in self.calls]


class VmCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = session_cfg(self.tmp.name)
        self.now = 0.0
        self.sleeps = []
        self.vm = V.SessionVm(self.cfg, 1, os.path.join(self.tmp.name, "private"), False, sleep=self.sleep,
                              clock=lambda: self.now)
        self.vm.qmp = self.qmp = ScriptedQmp()

    def sleep(self, n):
        self.sleeps.append(n)
        self.now += n


class SnapshotTests(VmCase):
    def test_save_uses_a_job_and_dismisses_it(self):
        self.qmp.jobs = [[{"id": "snapshot-save1", "status": "running"}],
                         [{"id": "snapshot-save1", "status": "concluded"}]]
        self.vm.snapshot_save()
        names = self.qmp.names()
        self.assertEqual(names, ["snapshot-save", "query-jobs", "query-jobs", "job-dismiss"])
        args = self.qmp.calls[0][1]
        self.assertEqual(args, {"job-id": "snapshot-save1", "tag": "clean", "vmstate": "sysn",
                                "devices": ["sysn", "vars"]})
        self.assertEqual(self.qmp.calls[-1][1], {"id": "snapshot-save1"})
        self.assertEqual(self.sleeps, [V.JOB_POLL])

    def test_job_error_is_raised_after_dismissing(self):
        self.qmp.jobs = [[{"id": "snapshot-save1", "status": "concluded", "error": "no space left"}]]
        with self.assertRaises(InfraError) as ctx:
            self.vm.snapshot_save()
        self.assertIn("no space left", str(ctx.exception))
        self.assertIn("job-dismiss", self.qmp.names())

    def test_job_that_never_concludes_times_out(self):
        self.qmp.jobs = [[{"id": "snapshot-save1", "status": "running"}]]
        with self.assertRaises(InfraError) as ctx:
            self.vm.snapshot_save()
        self.assertIn("did not finish", str(ctx.exception))

    def test_unlisted_job_is_an_error(self):
        self.qmp.jobs = [[]]
        with self.assertRaises(InfraError):
            self.vm.snapshot_save()

    def test_job_ids_are_unique(self):
        self.qmp.jobs = [[{"id": "snapshot-load1", "status": "concluded"}]]
        self.vm.snapshot_load()
        self.qmp.jobs = [[{"id": "snapshot-load2", "status": "concluded"}]]
        self.vm.snapshot_load()
        ids = [a["job-id"] for c, a in self.qmp.calls if c == "snapshot-load"]
        self.assertEqual(ids, ["snapshot-load1", "snapshot-load2"])

    def test_load_continues_a_paused_vm_only(self):
        self.qmp.jobs = [[{"id": "snapshot-load1", "status": "concluded"}]]
        self.qmp.status = "paused"
        self.vm.snapshot_load()
        self.assertEqual(self.qmp.names()[-2:], ["query-status", "cont"])
        self.qmp.calls.clear()
        self.qmp.jobs = [[{"id": "snapshot-load2", "status": "concluded"}]]
        self.qmp.status = "running"
        self.vm.snapshot_load()
        self.assertNotIn("cont", self.qmp.names())

    def test_load_with_the_deb_disk_plugged_is_refused(self):
        self.vm.plug_deb("/q/x.deb")
        with self.assertRaises(InfraError):
            self.vm.snapshot_load()
        self.assertNotIn("snapshot-load", self.qmp.names())


class DebDiskTests(VmCase):
    def test_plug_and_unplug(self):
        self.vm.plug_deb("/q/x.deb")
        (c1, a1), (c2, a2) = self.qmp.calls
        self.assertEqual(c1, "blockdev-add")
        self.assertEqual((a1["node-name"], a1["read-only"], a1["file"]["filename"]), ("deb", True, "/q/x.deb"))
        self.assertEqual(c2, "device_add")
        self.assertEqual((a2["serial"], a2["bus"], a2["drive"]), ("sbxdeb", "debport", "deb"))
        self.assertTrue(self.vm.deb_plugged)
        self.vm.unplug_deb()
        self.assertEqual(self.qmp.names()[2:], ["device_del", "blockdev-del"])
        self.assertFalse(self.vm.deb_plugged)

    def test_unplug_waits_until_the_guest_lets_go(self):
        self.vm.plug_deb("/q/x.deb")
        self.qmp.busy_blockdev_del = 3
        self.vm.unplug_deb()
        self.assertEqual(self.qmp.names().count("blockdev-del"), 4)
        self.assertFalse(self.vm.deb_plugged)

    def test_unplug_gives_up(self):
        self.vm.plug_deb("/q/x.deb")
        self.qmp.busy_blockdev_del = 10 ** 6
        with self.assertRaises(InfraError):
            self.vm.unplug_deb()
        self.assertTrue(self.vm.deb_plugged)

    def test_unplug_without_a_disk_does_nothing(self):
        self.vm.unplug_deb()
        self.assertEqual(self.qmp.calls, [])

    def test_failed_device_add_removes_the_node(self):
        self.qmp.fail["device_add"] = "no such bus"
        with self.assertRaises(InfraError):
            self.vm.plug_deb("/q/x.deb")
        self.assertEqual(self.qmp.names(), ["blockdev-add", "device_add", "blockdev-del"])
        self.assertFalse(self.vm.deb_plugged)

    def test_second_plug_is_refused(self):
        self.vm.plug_deb("/q/x.deb")
        with self.assertRaises(InfraError):
            self.vm.plug_deb("/q/y.deb")


class BootTests(VmCase):
    def test_boot_commands_and_qemu_arguments(self):
        ran, spawned = [], []
        self.vm._run = ran.append
        self.vm._spawn = spawned.append
        self.vm.boot()
        create, convert = ran
        self.assertEqual(create[:5], [self.cfg.qemu_img, "create", "-q", "-f", "qcow2"])
        self.assertEqual(convert[1:6], ["convert", "-f", "raw", "-O", "qcow2"])
        self.assertEqual(convert[6], self.cfg.base_vars)
        self.assertTrue(convert[7].endswith("/1/vars.qcow2"))
        argv = spawned[0]
        joined = " ".join(argv)
        for want in ("pflash0=code,pflash1=vars", "node-name=sysn", "throttling.bps-write=%d" % V.DISK_WRITE_BPS,
                     "-device virtio-keyboard-pci", "-device virtio-tablet-pci", "pcie-root-port,id=debport",
                     "addr=0x1e", "-vnc unix:" + os.path.join(self.tmp.name, "private", "vnc.sock")):
            self.assertIn(want, joined)
        self.assertNotIn("serial=sbxdeb", joined)  # the deb is hot-plugged only when an app needs it
        blockdevs = [json.loads(argv[i + 1]) for i, a in enumerate(argv) if a == "-blockdev"]
        self.assertEqual(blockdevs[1]["file"]["filename"], os.path.join(self.cfg.run, "1", "vars.qcow2"))

    def test_disk_bytes_counts_the_one_overlay(self):
        self.assertEqual(self.vm.disk_bytes(), 0)
        os.makedirs(os.path.join(self.cfg.run, "1"))
        with open(os.path.join(self.cfg.run, "1", "overlay.qcow2"), "wb") as f:
            f.write(b"x" * 1234)
        self.assertEqual(self.vm.disk_bytes(), 1234)

    def test_commas_in_run_directories_are_refused(self):
        with self.assertRaises(InfraError):
            V.SessionVm(self.cfg, 0, "/a,b")


if __name__ == "__main__":
    unittest.main()
