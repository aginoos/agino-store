# SPDX-License-Identifier: GPL-3.0-or-later
"""The build daemon (`agino-store-sandbox build-run`): scans the queue and runs build tasks.

queue/<id>/{task.json,source.tar[,cancel]} is written by agino-store (read-only for us); everything we write
goes to out/<id>/. A task with any status.json in out/ is never started again, so the queue entry may stay
as long as the server likes. At most `max_concurrent` tasks run at once, each in its own thread with its own
slot (tap agsbx-b<slot>); the daemon thread only starts, cancels and reaps.
"""

import fcntl
import os
import signal
import threading
import time

from .build_job import BuildJob, CANCEL_FILE, Deps
from .build_out import TERMINAL_STATES, OutDir, cleanup_old, read_status
from .build_task import TASK_ID_RE, TaskError, read_task_file
from .util import SandboxError, log, now

SCAN_INTERVAL = 5.0
ONCE_INTERVAL = 1.0
SETTLE_SECONDS = 3.0  # task.json and source.tar must have been quiet this long: the server may still be writing
CLEANUP_INTERVAL = 3600.0
STOP_JOIN_SECONDS = 60.0
LOCK_NAME = "build-run.lock"


def _job_thread(job):
    try:
        job.run()
    except BaseException as e:  # noqa: BLE001 - run() should never raise; the sweep fails the task if it did
        log("build %s: runner thread died: %s" % (job.task.id, type(e).__name__))


class BuildDaemon:
    def __init__(self, cfg, once=False, deps=None, job_factory=None, sleep=None, wall=time.time):
        self.cfg, self.bcfg = cfg, cfg.builder
        self.once = once
        self.deps = deps or Deps()
        self.job_factory = job_factory or (lambda c, task, slot: BuildJob(c, task, slot, self.deps))
        self.wall = wall
        self.stopping = threading.Event()
        self.sleep = sleep or self.stopping.wait
        self.active = {}  # task id -> (job, thread, slot)
        self.free_slots = list(range(self.bcfg.max_concurrent))
        self.outcomes = []
        self.skip = set()  # ids whose terminal record could not be written; not retried in this process
        self._last_cleanup = None

    # ---- queue ----

    def candidates(self):
        """Ids of queue entries without any status yet, oldest first."""
        queue = self.bcfg.queue
        try:
            names = os.listdir(queue)
        except FileNotFoundError:
            return []
        settle = 0.0 if self.once else SETTLE_SECONDS
        found = []
        for name in names:
            if not TASK_ID_RE.match(name) or name in self.active or name in self.skip:
                continue
            base = os.path.join(queue, name)
            try:
                if not os.path.isdir(base) or os.path.islink(base):
                    continue
                times = [os.lstat(os.path.join(base, f)).st_mtime for f in ("task.json", "source.tar")]
            except OSError:
                continue  # not complete yet
            if read_status(self.bcfg.out, name) != {} or self.wall() - max(times) < settle:
                continue
            found.append((times[0], name))
        return [name for _, name in sorted(found)]

    def _record(self, task_id, state, error):
        """Terminal record for a task that never ran (invalid, cancelled before the start, orphan)."""
        out = OutDir(self.bcfg.out, task_id, self.bcfg.max_log_mib << 20)
        try:
            out.create()
            out.sink.note("== %s: %s ==" % (state, error))
            out.write_result(False, error, [], [])
            out.write_status(state, now(), 0, 0, error)
        except OSError as e:
            log("build %s: cannot record %s: %s" % (task_id, state, e.strerror))
            self.skip.add(task_id)
        finally:
            out.close()
        if state != "cancelled":
            self.outcomes.append(state)

    def _start(self, task_id):
        if os.path.lexists(os.path.join(self.bcfg.queue, task_id, CANCEL_FILE)):
            return self._record(task_id, "cancelled", "cancelled")
        try:
            task = read_task_file(self.bcfg.queue, task_id, self.bcfg.limits)
        except TaskError as e:
            return self._record(task_id, "failed", "invalid task: %s" % e)
        slot = self.free_slots.pop(0)
        try:
            job = self.job_factory(self.cfg, task, slot)
            thread = threading.Thread(target=_job_thread, args=(job,), name="build-" + task_id)
            self.active[task_id] = (job, thread, slot)
            log("build %s: %s %s on slot %d" % (task_id, task.app, task.version, slot))
            thread.start()
        except Exception as e:  # noqa: BLE001 - one broken task must not stop the daemon
            self.active.pop(task_id, None)
            self.free_slots = sorted(self.free_slots + [slot])
            log("build %s: cannot start: %s" % (task_id, type(e).__name__))
            self._record(task_id, "failed", "internal error: %s" % type(e).__name__)

    def _reap(self):
        for task_id, (job, thread, slot) in list(self.active.items()):
            if job.finished.is_set() or not thread.is_alive():
                thread.join()
                del self.active[task_id]
                self.free_slots = sorted(self.free_slots + [slot])
                self.outcomes.append(job.state if job.state in TERMINAL_STATES else "failed")
                log("build %s: %s" % (task_id, job.state))
                status = read_status(self.bcfg.out, task_id)
                if not status or status.get("state") not in TERMINAL_STATES:
                    self.skip.add(task_id)  # no terminal record exists; never start it again in this process

    def _cancels(self):
        for job, _thread, _slot in list(self.active.values()):
            if job.cancel_requested():
                job.request_stop("cancelled", "cancelled")

    def sweep_orphans(self):
        """A task whose status is not terminal and that nobody is running (runner restarted, thread died) is failed."""
        try:
            names = os.listdir(self.bcfg.out)
        except OSError:
            return
        for name in names:
            if not TASK_ID_RE.match(name) or name in self.active or name in self.skip:
                continue
            status = read_status(self.bcfg.out, name)
            if status is None or (status and status.get("state") not in TERMINAL_STATES):
                self._record(name, "failed", "the build runner stopped while this task was running")

    def _cleanup(self):
        clock = time.monotonic()
        if self._last_cleanup is None or clock - self._last_cleanup >= CLEANUP_INTERVAL:
            self._last_cleanup = clock
            for name in cleanup_old(self.bcfg.out, self.bcfg.queue, self.wall):
                log("build %s: old output removed" % name)

    # ---- loop ----

    def tick(self):
        self._reap()
        self.sweep_orphans()
        self._cancels()
        for task_id in self.candidates():
            if self.stopping.is_set():
                break
            if os.path.lexists(os.path.join(self.bcfg.queue, task_id, CANCEL_FILE)) or self.free_slots:
                self._start(task_id)
        self._cleanup()

    def stop(self):
        self.stopping.set()

    def run(self):
        """Serve until stopped (or, with `once`, until the queue is processed).

        Returns 1 if a task failed in once mode, else 0."""
        try:
            while not self.stopping.is_set():
                self.tick()
                if self.once and not self.active and not self.candidates():
                    break
                self.sleep(ONCE_INTERVAL if self.once else SCAN_INTERVAL)
        finally:
            self._shutdown()
        return 1 if self.once and "failed" in self.outcomes else 0

    def _shutdown(self):
        for job, _thread, _slot in list(self.active.values()):
            job.request_stop("failed", "the build runner was stopped")
        for job, thread, _slot in list(self.active.values()):
            thread.join(STOP_JOIN_SECONDS)
        self._reap()


def acquire_lock(cfg):
    """flock of run/build-run.lock for the life of the process: only one daemon scans the queue."""
    os.makedirs(cfg.run, mode=0o755, exist_ok=True)
    fd = os.open(os.path.join(cfg.run, LOCK_NAME), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise SandboxError("another build-run is already running") from None
    return fd


def serve(cfg, once=False):
    """Entry point of `build-run`; returns the exit code."""
    os.umask(0o027)  # out/<id>/ must come out 0750 / 0640 for the console's group; no chmod may touch setgid bits
    lock = acquire_lock(cfg)
    daemon = BuildDaemon(cfg, once=once)
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda _num, _frame: daemon.stop())
    try:
        return daemon.run()
    finally:
        os.close(lock)
