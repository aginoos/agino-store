# SPDX-License-Identifier: GPL-3.0-or-later
import base64
import dataclasses
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_helpers import builder_cfg  # noqa: E402

from agino_store_sandbox import build_vm as B  # noqa: E402
from agino_store_sandbox import config  # noqa: E402
from agino_store_sandbox import vm as V  # noqa: E402
from agino_store_sandbox.qga import ExecResult, Qga  # noqa: E402
from agino_store_sandbox.util import InfraError, SandboxError  # noqa: E402

GIB = 1 << 30


class ArgvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = builder_cfg(self.tmp.name)

    def argv(self, sandbox=False, builder=None, slot=0):
        return V.build_argv(self.cfg, slot, V.RunFiles(self.tmp.name + "/run/b0"), None, sandbox, builder=builder)

    def test_builder_mode_uses_its_own_tap_mac_name_and_resources(self):
        argv = self.argv(builder=B.BuilderSpec(8192, 4, 1), slot=1)
        joined = " ".join(argv)
        self.assertIn("ifname=agsbx-b1,", joined)
        self.assertNotIn("agsbx-t", joined)
        self.assertIn("mac=52:54:00:5b:01:01", joined)
        self.assertEqual(argv[argv.index("-name") + 1], "agsbx-b1")
        self.assertEqual(argv[argv.index("-m") + 1], "8192")
        self.assertEqual(argv[argv.index("-smp") + 1], "4")

    def test_builder_mode_has_no_vnc_and_no_input_devices(self):
        argv = " ".join(self.argv(builder=B.BuilderSpec(1024, 2, 0)))
        for word in ("-vnc", "virtio-keyboard", "virtio-tablet", "-spice", "-usb"):
            self.assertNotIn(word, argv)
        self.assertIn("-display none", argv)
        self.assertIn("-nodefaults", argv)
        self.assertIn("tap,id=n0,ifname=agsbx-b0,script=no,downscript=no", argv)
        self.assertNotIn("user,", argv)  # never slirp

    def test_seccomp_flag(self):
        self.assertIn("-sandbox", self.argv(sandbox=True, builder=B.BuilderSpec(1024, 2, 0)))
        self.assertNotIn("-sandbox", self.argv(sandbox=False, builder=B.BuilderSpec(1024, 2, 0)))

    def test_normal_runs_are_unchanged(self):
        argv = self.argv(slot=2)
        self.assertEqual(argv[argv.index("-name") + 1], "agsbx-2")
        self.assertEqual(argv[argv.index("-m") + 1], str(self.cfg.memory_mb))
        self.assertEqual(argv[argv.index("-smp") + 1], str(self.cfg.cpus))
        self.assertIn("ifname=agsbx-t2,", " ".join(argv))
        self.assertIn("mac=52:54:00:5b:00:02", " ".join(argv))

    def test_names(self):
        self.assertEqual(V.builder_tap_name(3), "agsbx-b3")
        self.assertEqual(V.builder_mac_address(3), "52:54:00:5b:01:03")


class BuildVmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = builder_cfg(self.tmp.name)
        with open(self.cfg.builder.base_vars, "wb") as f:
            f.write(b"vars")

    def boot(self, base_size, limits, slot=1, sandbox=False):
        vm = B.BuildVm(self.cfg, slot, limits, sandbox)
        runs = []
        with mock.patch.object(vm, "_virtual_size", return_value=base_size), \
                mock.patch.object(vm, "_run", side_effect=runs.append), \
                mock.patch.object(vm, "_spawn") as spawn:
            vm.boot()
        return vm, runs, spawn.call_args[0][0]

    def test_overlay_sits_on_the_builder_base_with_the_disk_limit(self):
        vm, runs, argv = self.boot(10 * GIB, config.BuildLimits(90, 8, 20))
        create = runs[0]
        self.assertEqual(create[1:4], ["create", "-q", "-f"])
        self.assertEqual(create[create.index("-b") + 1], self.cfg.builder.base)
        self.assertEqual(create[-2], os.path.join(self.tmp.name, "run", "b1", "overlay.qcow2"))
        self.assertEqual(create[-1], str(20 * GIB))
        self.assertEqual(argv[argv.index("-m") + 1], str(8 * 1024))
        self.assertIn("ifname=agsbx-b1,", " ".join(argv))
        self.assertEqual(vm.files.directory, os.path.join(self.tmp.name, "run", "b1"))
        with open(vm.files.path("vars.fd"), "rb") as f:
            self.assertEqual(f.read(), b"vars")

    def test_overlay_is_never_smaller_than_the_base(self):
        _vm, runs, _argv = self.boot(32 * GIB, config.BuildLimits(90, 8, 20))
        self.assertEqual(runs[0][-1], str(32 * GIB))

    def test_memory_follows_the_recipe(self):
        _vm, _runs, argv = self.boot(GIB, config.BuildLimits(90, 2, 5))
        self.assertEqual(argv[argv.index("-m") + 1], "2048")

    def test_missing_vars_is_an_infra_error(self):
        os.unlink(self.cfg.builder.base_vars)
        vm = B.BuildVm(self.cfg, 0, config.BuildLimits(), False)
        with mock.patch.object(vm, "_virtual_size", return_value=0), mock.patch.object(vm, "_run"):
            with self.assertRaises(InfraError):
                vm.boot()

    def test_disk_bytes_exited_and_kill(self):
        vm, _runs, _argv = self.boot(GIB, config.BuildLimits())
        with open(vm.files.path("overlay.qcow2"), "wb") as f:
            f.write(b"x" * 1234)
        self.assertEqual(vm.disk_bytes(), 1234)
        self.assertFalse(vm.exited())
        vm.proc = mock.Mock()
        vm.proc.poll.return_value = None
        vm.kill()
        vm.proc.kill.assert_called_once_with()
        vm.proc.poll.return_value = 0
        self.assertTrue(vm.exited())
        vm.proc.kill.reset_mock()
        vm.kill()
        vm.proc.kill.assert_not_called()

    def test_stop_removes_only_the_registered_run_files(self):
        vm, _runs, _argv = self.boot(GIB, config.BuildLimits())
        bystander = os.path.join(vm.files.directory, "bystander")
        with open(bystander, "w") as f:
            f.write("keep")
        with open(vm.files.path("overlay.qcow2"), "w") as f:
            f.write("x")
        vm.stop()
        self.assertFalse(os.path.exists(os.path.join(vm.files.directory, "overlay.qcow2")))
        self.assertTrue(os.path.exists(bystander))

    def test_make_vm_fails_closed_without_seccomp(self):
        cfg = builder_cfg(self.tmp.name)
        cfg = dataclasses.replace(cfg, qemu_sandbox="auto")
        with mock.patch("agino_store_sandbox.runner.probe_sandbox", return_value=False):
            with self.assertRaises(SandboxError):
                B.make_vm(cfg, 0, config.BuildLimits())

    def test_make_vm_with_sandbox_on(self):
        cfg = dataclasses.replace(builder_cfg(self.tmp.name), qemu_sandbox="on")
        self.assertTrue(B.make_vm(cfg, 0, config.BuildLimits()).use_sandbox)


class QgaHelperTests(unittest.TestCase):
    def make(self, responder):
        qga = Qga.__new__(Qga)
        qga.calls = []

        def call(command, arguments=None, timeout=30):
            qga.calls.append((command, arguments))
            return responder(command, arguments)
        qga._call = call
        qga.exec = lambda argv, timeout, input_data=None: ExecResult(0, b"", b"")
        return qga

    def test_read_chunk_seeks_reads_and_closes(self):
        data = b"hello world"

        def responder(command, args):
            if command == "guest-file-open":
                return 7
            if command == "guest-file-read":
                return {"count": 5, "buf-b64": base64.b64encode(data[6:11]).decode(), "eof": True}
            return {}
        qga = self.make(responder)
        self.assertEqual(qga.read_chunk("/f", 6, 100), b"world")
        self.assertEqual([c for c, _ in qga.calls], ["guest-file-open", "guest-file-seek", "guest-file-read",
                                                      "guest-file-close"])
        self.assertEqual(qga.calls[1][1], {"handle": 7, "offset": 6, "whence": 0})

    def test_read_chunk_without_offset_does_not_seek(self):
        qga = self.make(lambda c, a: 3 if c == "guest-file-open" else {"count": 0, "buf-b64": "", "eof": True})
        self.assertEqual(qga.read_chunk("/f", 0, 10), b"")
        self.assertNotIn("guest-file-seek", [c for c, _ in qga.calls])

    def test_read_chunk_rejects_bad_answers(self):
        for ret in ({"buf-b64": "!!!"}, {"buf-b64": base64.b64encode(b"x" * 20).decode()}, {}, "x"):
            qga = self.make(lambda c, a, ret=ret: 3 if c == "guest-file-open" else ret)
            with self.assertRaises(InfraError, msg=ret):
                qga.read_chunk("/f", 0, 10)
            self.assertEqual(qga.calls[-1][0], "guest-file-close")

    def test_read_chunk_closes_when_the_read_fails(self):
        def responder(command, args):
            if command == "guest-file-read":
                raise InfraError("guest agent guest-file-read timed out")
            return 3
        qga = self.make(responder)
        with self.assertRaises(InfraError):
            qga.read_chunk("/f", 0, 10)
        self.assertEqual(qga.calls[-1][0], "guest-file-close")

    def test_write_stream_chunks_and_chmods(self):
        written = []

        def responder(command, args):
            if command == "guest-file-write":
                written.append(base64.b64decode(args["buf-b64"]))
            return 5 if command == "guest-file-open" else {}
        qga = self.make(responder)
        chmods = []
        qga.exec = lambda argv, timeout, input_data=None: chmods.append(argv) or ExecResult(0, b"", b"")
        qga.write_stream("/dst", io.BytesIO(b"a" * 150000), "644")
        self.assertEqual([len(w) for w in written], [65536, 65536, 18928])
        self.assertEqual(chmods, [["/bin/chmod", "644", "/dst"]])
        self.assertEqual(qga.calls[0], ("guest-file-open", {"path": "/dst", "mode": "w"}))
        self.assertEqual(qga.calls[-1][0], "guest-file-close")

    def test_write_stream_closes_on_error(self):
        def responder(command, args):
            if command == "guest-file-write":
                raise InfraError("guest agent guest-file-write failed")
            return 5
        qga = self.make(responder)
        with self.assertRaises(InfraError):
            qga.write_stream("/dst", io.BytesIO(b"abc"))
        self.assertEqual(qga.calls[-1][0], "guest-file-close")


if __name__ == "__main__":
    unittest.main()
