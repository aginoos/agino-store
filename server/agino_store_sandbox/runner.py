# SPDX-License-Identifier: GPL-3.0-or-later
"""Job orchestration: slots, one VM per job, state and report files."""

import concurrent.futures
import contextlib
import fcntl
import os
import shutil

from . import jobs, report
from .guest import Guest, run_job
from .shots import Phase
from .util import InfraError, SandboxError, log, now
from .vm import Vm, probe_sandbox

BOOT_TIMEOUT = 180


@contextlib.contextmanager
def slot_lock(cfg, slots):
    """Hold the flock of any free slot; yields the slot number or None when all are busy."""
    os.makedirs(cfg.run, mode=0o755, exist_ok=True)
    for slot in range(slots):
        fd = os.open(os.path.join(cfg.run, "slot%d.lock" % slot), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            continue
        try:
            yield slot
        finally:
            os.close(fd)
        return
    yield None


def _sandbox_flag(cfg):
    if cfg.qemu_sandbox == "auto":
        return probe_sandbox(cfg)
    return cfg.qemu_sandbox == "on"


def _screenshot(cfg, vm, stem):
    def take():
        src = vm.files.path("shot.png")
        vm.qmp.screendump(src)
        os.makedirs(cfg.shot_dir, mode=0o755, exist_ok=True)
        tmp = os.path.join(cfg.shot_dir, ".%s.png.tmp" % stem)
        shutil.copyfile(src, tmp)
        os.replace(tmp, os.path.join(cfg.shot_dir, stem + ".png"))
    return take


def execute(cfg, job, slot, sandbox):
    """Boot a guest for job and run all checks; returns (checks, details, problems). Raises InfraError."""
    shots_on = cfg.shots and job.shots is not None
    vm = Vm(cfg, slot, job.deb_path, sandbox, job.demo_path if shots_on else None)
    note = lambda m: log("%s: %s" % (job.stem, m))  # noqa: E731
    try:
        vm.start()
        vm.qga.wait_ready(BOOT_TIMEOUT)
        guest = Guest(vm.qga)
        phase = None
        if shots_on:
            def phase(desktop, base_pids):
                return Phase(cfg, vm, guest, job, desktop, note).run(base_pids)
        checks, details, problems = run_job(guest, job, _screenshot(cfg, vm, job.stem), note, phase)
        if cfg.shots and job.shots_error:
            details["shots"] = {"error": job.shots_error, "images": 0, "problems": 0}
        return checks, details, problems
    finally:
        vm.stop()


def run_job_file(cfg, name, slot, sandbox, forced=False):
    """Run one queue entry and record state (and report); returns the state dict."""
    stem = jobs.stem_of(name)
    try:
        job = jobs.read_job(cfg.queue, name)
    except SandboxError as e:
        state = {"status": "invalid", "reason": str(e)[:200], "attempts": 0}
        jobs.write_state(cfg.state, stem, state)
        return state
    attempts = 0 if forced else jobs.previous_attempts(cfg.state, stem)
    attempts += 1
    started = now()
    try:
        checks, details, problems = execute(cfg, job, slot, sandbox)
        doc = report.build(job, checks, details, problems, started, now())
    except InfraError as e:
        reason = str(e)[:200]
        if attempts < jobs.MAX_ATTEMPTS:
            state = {"status": "error", "reason": reason, "attempts": attempts}
            jobs.write_state(cfg.state, stem, state)
            return state
        doc = report.build_failure(job, reason, started, now())
    written = report.write(cfg.reports, cfg.key, stem, doc)
    state = {"status": "done", "result": doc["status"], "report": written, "attempts": attempts}
    jobs.write_state(cfg.state, stem, state)
    return state


def _worker(cfg, name, sandbox, forced):
    with slot_lock(cfg, cfg.slots) as slot:
        if slot is None:
            return name, None
        if not forced and name not in jobs.pending(cfg.queue, cfg.state):
            return name, None  # another runner finished it meanwhile
        log("%s: slot %d" % (name, slot))
        try:
            return name, run_job_file(cfg, name, slot, sandbox, forced)
        except SandboxError as e:
            return name, {"status": "error", "reason": str(e)[:200]}


def run(cfg, only=None):
    """Run every pending job (or only one); returns {name: state}."""
    report.check_key_file(cfg.key)
    names = jobs.pending(cfg.queue, cfg.state, only)
    if not names:
        return {}
    sandbox = _sandbox_flag(cfg)
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.slots) as pool:
        for name, state in pool.map(lambda n: _worker(cfg, n, sandbox, only is not None), names):
            if state is not None:
                results[name] = state
                log("%s: %s" % (name, state.get("result") or state.get("status")))
    return results


def check_base(cfg):
    """Boot the base with no job; returns (ok, message)."""
    sandbox = _sandbox_flag(cfg)
    with slot_lock(cfg, cfg.slots) as slot:
        if slot is None:
            return False, "all slots are busy"
        vm = Vm(cfg, slot, None, sandbox)
        try:
            vm.start()
            vm.qga.wait_ready(BOOT_TIMEOUT)
            guest = Guest(vm.qga)
            guest.wait_session()
            count = len(guest.windows())
            return True, "guest agent, session and X11 work (%d windows)" % count
        except SandboxError as e:
            return False, str(e)
        finally:
            vm.stop()
