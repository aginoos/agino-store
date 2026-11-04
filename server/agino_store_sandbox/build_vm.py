# SPDX-License-Identifier: GPL-3.0-or-later
"""The VM of one build task: a fresh overlay on the read-only builder base, build-only tap, no display input.

Like a session VM it fails closed on seccomp (`session_sandbox_flag`): only an explicit `qemu_sandbox = off`
runs qemu without -sandbox. It differs from the check VMs in the run directory (`<run>/b<slot>`), the tap
(`agsbx-b<slot>`), the overlay's virtual size (the task's disk limit) and the memory/CPU taken from the recipe.
"""

import dataclasses
import json
import os
import shutil
import subprocess

from .runner import session_sandbox_flag
from .util import InfraError
from .vm import RunFiles, Vm, build_argv

GIB = 1 << 30


@dataclasses.dataclass(frozen=True)
class BuilderSpec:
    memory_mb: int
    cpus: int
    slot: int


class BuildVm(Vm):
    def __init__(self, cfg, slot, limits, use_sandbox, cpus=None):
        super().__init__(cfg, slot, None, use_sandbox, None)
        self.limits = limits
        self.cpus = cpus  # AI tasks bring their own vCPU count; builds use the configured one
        self.files = RunFiles(os.path.join(cfg.run, "b%d" % slot))
        if "," in self.files.directory:
            raise InfraError("run directories must not contain a comma")

    def _virtual_size(self, path):
        """Virtual size in bytes of an image (0 if unknown); read-only query that shares the image lock."""
        try:
            done = subprocess.run([self.cfg.qemu_img, "info", "--output=json", "-U", path], capture_output=True,
                                  timeout=60)
            size = json.loads(done.stdout.decode("utf-8")).get("virtual-size", 0) if done.returncode == 0 else 0
        except (OSError, subprocess.TimeoutExpired, ValueError, UnicodeDecodeError):
            return 0
        return size if isinstance(size, int) and not isinstance(size, bool) else 0

    def boot(self):
        """Overlay (virtual size = the disk limit, never below the base), OVMF variables, then qemu."""
        bcfg, files = self.cfg.builder, self.files
        files.prepare()
        size = max(self.limits.disk_gib * GIB, self._virtual_size(bcfg.base))
        self._run([self.cfg.qemu_img, "create", "-q", "-f", "qcow2", "-b", bcfg.base, "-F", "qcow2",
                   files.path("overlay.qcow2"), str(size)])
        try:
            shutil.copyfile(bcfg.base_vars, files.path("vars.fd"))
        except OSError as e:
            raise InfraError("cannot copy the builder OVMF vars: %s" % e.strerror) from None
        spec = BuilderSpec(self.limits.memory_gib * 1024, self.cpus or self.cfg.cpus, self.slot)
        self._spawn(build_argv(self.cfg, self.slot, files, None, self.use_sandbox, builder=spec))

    def disk_bytes(self):
        """Bytes the overlay occupies on the host now."""
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


def make_vm(cfg, slot, limits, cpus=None):
    """A BuildVm with the seccomp decision applied (raises SandboxError when it cannot be had)."""
    return BuildVm(cfg, slot, limits, session_sandbox_flag(cfg), cpus)
