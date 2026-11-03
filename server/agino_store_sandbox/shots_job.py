# SPDX-License-Identifier: GPL-3.0-or-later
"""Shots-only jobs: install the app in a fresh guest and take the store screenshots; no checks, no report."""

import os
import shutil

from . import jobs
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


def install(guest, job, update_timeout=APT_UPDATE_TIMEOUT, install_timeout=APT_INSTALL_TIMEOUT):
    """Install job's app in the guest; raises InstallError when apt fails."""
    if job.source == "deb":
        guest.copy_deb(job)
        note = guest.apply_profile(job)
        if note:
            log("%s: install_profile: %s" % (job.stem, note))
        target = "/var/tmp/sbx/x.deb"
    else:
        target = job.package
    update = guest.apt(["update"], update_timeout)
    if job.source == "milkyway" and not update.ok:
        raise InstallError("apt-get update failed (exit %d)" % update.exitcode, update.exitcode,
                           clean(update.text[-600:] + update.err.decode("utf-8", "replace")[-300:], 900))
    extra = list(job.l10n) if job.source == "milkyway" else []
    res = guest.apt(["install", "-y", target, *extra], install_timeout)
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


def reset(cfg, name):
    """Move shots_out/<name> to <name>.prev (replacing an older one) and drop the shots state of the job."""
    stem = jobs.stem_of(name)
    if not jobs.JOB_RE.match(stem + ".json"):
        raise SandboxError("not a job name: %s" % clean(name, 60))
    out = os.path.join(cfg.shots_out, stem)
    prev = out + ".prev"
    done = []
    if os.path.isdir(out) and not os.path.islink(out):
        if os.path.lexists(prev):
            shutil.rmtree(prev)
        os.rename(out, prev)
        done.append("output kept as %s" % os.path.basename(prev))
    state = os.path.join(cfg.state, jobs.state_key(stem, "shots") + ".json")
    if os.path.lexists(state):
        os.unlink(state)
        done.append("state removed")
    return "%s: %s" % (stem, ", ".join(done) if done else "nothing to reset")
