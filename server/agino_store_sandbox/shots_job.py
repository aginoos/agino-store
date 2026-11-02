# SPDX-License-Identifier: GPL-3.0-or-later
"""Shots-only jobs: install the app in a fresh guest and take the store screenshots; no checks, no report."""

from .guest import Guest
from .shots import Phase
from .shots_guest import ShotsGuest
from .util import InfraError, SandboxError, clean, log
from .vm import Vm

BOOT_TIMEOUT = 180
APT_UPDATE_TIMEOUT = 300
APT_INSTALL_TIMEOUT = 900


class InstallError(SandboxError):
    """apt could not install the app; carries the exit code and a capped tail of the output."""

    def __init__(self, message, exitcode, tail):
        super().__init__(message)
        self.exitcode, self.tail = exitcode, tail


def install(guest, job):
    """Install job's app in the guest; raises InstallError when apt fails."""
    if job.source == "deb":
        guest.copy_deb(job)
        note = guest.apply_profile(job)
        if note:
            log("%s: install_profile: %s" % (job.stem, note))
        target = "/var/tmp/sbx/x.deb"
    else:
        target = job.package
    update = guest.apt(["update"], APT_UPDATE_TIMEOUT)
    if job.source == "milkyway" and not update.ok:
        raise InstallError("apt-get update failed (exit %d)" % update.exitcode, update.exitcode,
                           clean(update.text[-600:] + update.err.decode("utf-8", "replace")[-300:], 900))
    res = guest.apt(["install", "-y", target], APT_INSTALL_TIMEOUT)
    if not res.ok:
        raise InstallError("apt-get install failed (exit %d)" % res.exitcode, res.exitcode,
                           clean(res.text[-600:] + res.err.decode("utf-8", "replace")[-300:], 900))
    if not guest.is_installed(job.package):
        raise InstallError("package %s is not installed after apt-get" % job.package, res.exitcode, "")


def execute_shots(cfg, job, slot, sandbox):
    """Boot, install, take the screenshots; returns the shots details. InfraError/InstallError on failure."""
    vm = Vm(cfg, slot, job.deb_path, sandbox, job.demo_path)
    note = lambda m: log("%s: %s" % (job.stem, m))  # noqa: E731
    try:
        vm.start()
        vm.qga.wait_ready(BOOT_TIMEOUT)
        guest = Guest(vm.qga)
        note("waiting for the session")
        guest.wait_session()
        note("installing (%s)" % job.source)
        install(guest, job)
        desktop = guest.find_desktop(job)
        if not desktop:
            raise InstallError("no desktop file found for %s" % job.package, 0, "")
        base_pids = ShotsGuest(guest).tester_pids()
        return Phase(cfg, vm, guest, job, desktop, note).run(base_pids)
    finally:
        vm.stop()
