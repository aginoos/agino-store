# SPDX-License-Identifier: GPL-3.0-or-later
"""The interactive screenshot session: one lock-protected state machine, its lifecycle and its limits.

States: queued -> starting -> installing -> starting -> running -> ending, or failed at any point
(starting again also happens during a rollback). All guest agent and QMP calls happen inside an
operation (one at a time, `_op`) or on the main thread, never on a VNC relay thread, and each call has a
deadline. A watcher thread enforces the limits; it only sets state and kills the qemu we started, the
main thread does the clean-up (docs/sandbox.md section 9).
"""

import contextlib
import dataclasses
import os
import re
import shutil
import threading
import time

from . import jobs, shots_job, shots_scene
from .guest import Guest
from .qga import Qga
from .runner import _sandbox_flag, slot_lock
from .session_capture import Capturer, CaptureStore, CaptureError, GuestLocator
from .session_vm import SessionVm
from .shots_guest import ShotsGuest
from .util import InfraError, SandboxError, clean, log

STATES = ("queued", "starting", "installing", "running", "ending", "failed")
ALLOWED = {"queued": {"starting", "failed", "ending"},
           "starting": {"installing", "running", "failed", "ending"},
           "installing": {"starting", "failed", "ending"},
           "running": {"starting", "failed", "ending"},
           "failed": {"ending"},
           "ending": set()}
SESSION_ID_RE = re.compile(r"\A[0-9a-f]{16}\Z")
APP_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,63}\Z")
BOOT_TIMEOUT = 180
SLOT_WAIT = 60
SLOT_POLL = 2
EXTEND_SECONDS = 15 * 60
ROLLBACK_SECONDS = 300
DISK_CHECK_SECONDS = 10
APP_EXTRA_SECONDS = 300
ERROR_LIMIT = 200
GIB = 1 << 30


class OpError(Exception):
    """A request that cannot be served; `code` is the error string of the control protocol."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def find_job(cfg, app):
    """The newest valid queue job of app (shots or check; its deb and hash are verified)."""
    pattern = re.compile(r"\A%s_[0-9a-f]{12}\.json\Z" % re.escape(app))
    try:
        names = [n for n in os.listdir(cfg.queue) if pattern.match(n)]
    except OSError as e:
        raise SandboxError("cannot list the queue: %s" % e.strerror) from None

    def age(name):
        try:
            return os.lstat(os.path.join(cfg.queue, name)).st_mtime
        except OSError:
            return 0.0
    for name in sorted(names, key=age, reverse=True):
        try:
            job = jobs.read_job(cfg.queue, name)
        except SandboxError:
            continue
        if job.app == app:
            return job
    raise SandboxError("no usable sandbox job for %s in the queue" % app)


def tap_bytes(slot, root="/sys/class/net"):
    """rx + tx bytes of the slot's tap device (world-readable sysfs counters)."""
    total = 0
    for name in ("rx_bytes", "tx_bytes"):
        try:
            with open("%s/agsbx-t%d/statistics/%s" % (root, slot, name), "rb") as f:
                total += int(f.read(32))
        except (OSError, ValueError):
            pass
    return total


@dataclasses.dataclass
class Deps:
    """Everything the session builds or reads from the outside; tests replace these."""
    vm_factory: object = SessionVm
    guest_factory: object = Guest
    sg_factory: object = ShotsGuest
    install: object = shots_job.install
    locator_factory: object = GuestLocator
    find_job: object = find_job
    sandbox_flag: object = _sandbox_flag
    tap_bytes: object = tap_bytes
    free_bytes: object = lambda path: shutil.disk_usage(path).free  # noqa: E731
    clock: object = time.monotonic
    sleep: object = time.sleep
    slot_wait: float = SLOT_WAIT


class Session:
    def __init__(self, cfg, app, session_id, lang, private_dir, deps=None, publish=None, drop_vnc=None):
        self.cfg, self.app, self.id, self.lang = cfg, app, session_id, lang
        self.scfg = cfg.session
        self.private_dir = private_dir
        self.d = deps or Deps()
        self.publish = publish or (lambda session: None)
        self.drop_vnc = drop_vnc or (lambda: None)
        self.store = CaptureStore(os.path.join(self.scfg.captures, session_id), session_id, app,
                                  self.scfg.max_captures)
        self._lock = threading.RLock()
        self._op = threading.Lock()
        self.stop_event = threading.Event()
        self.finished = threading.Event()
        self.state, self.error = "queued", ""
        self.extended = False
        self.vm = self.guest = self.sg = self.job = self.slot = None
        self.argv = None
        self.base_pids = set()
        self._halting = False
        self._serving = False
        self._phase_end = None
        self._phase_reason = ""
        self._tap_base = 0
        self._last_disk_check = 0.0
        now = self.d.clock()
        self.t0 = now
        self.total_end = now + self.scfg.total_minutes * 60
        self.run_end = None
        self.last_input = now

    # ---- state ----

    def set_state(self, new):
        with self._lock:
            if new != self.state and new not in ALLOWED[self.state]:
                return False
            self.state = new
        self.publish(self)
        return True

    def fail(self, reason):
        """Fault: state failed with a reason; the main thread cleans up."""
        with self._lock:
            if self.state in ("failed", "ending"):
                return
            self.error = clean(reason, ERROR_LIMIT)
            self.state = "failed"
        log("session %s failed: %s" % (self.id, self.error))
        self._interrupt()

    def request_end(self, reason=""):
        """Orderly end (reviewer, limit, signal): state ending."""
        with self._lock:
            if self.state == "ending":
                return
            if reason and not self.error:
                self.error = clean(reason, ERROR_LIMIT)
            self.state = "ending"
        log("session %s ending: %s" % (self.id, reason or "requested"))
        self._interrupt()

    def _interrupt(self):
        self.stop_event.set()
        if self.vm is not None and (not self._serving or self._op.locked()):
            self.vm.kill()  # a guest agent call that is blocked on the dying VM returns at once
        self.drop_vnc()
        self.publish(self)

    def touch_input(self, count=1):
        self.last_input = self.d.clock()

    def status(self):
        with self._lock:
            now = self.d.clock()
            end = self.total_end
            if self.state == "running" or self.run_end is not None:
                end = min(end, self.run_end)
            else:
                end = min(end, now + self.scfg.run_minutes * 60)
            return {"ok": True, "state": self.state, "id": self.id, "app": self.app, "lang": self.lang,
                    "remaining_s": int(max(0, end - now)),
                    "idle_s": int(max(0, now - self.last_input)) if self.state == "running" else 0,
                    "captures": self.store.count(), "extended": self.extended, "error": self.error}

    # ---- main thread ----

    def run(self):
        """Queue, boot, install, serve until stopped, clean up. Never raises."""
        try:
            self._run()
        except _Stopped:
            pass
        except (SandboxError, OSError) as e:
            self.fail(str(e) or type(e).__name__)
        except Exception as e:  # noqa: BLE001 - the daemon must clean up whatever happened
            log("session %s: unexpected %s: %s" % (self.id, type(e).__name__, e))
            self.fail("internal error: %s" % type(e).__name__)
        finally:
            self._teardown()

    def _run(self):
        scfg = self.scfg
        self.job = self.d.find_job(self.cfg, self.app)
        os.makedirs(self.cfg.run, mode=0o755, exist_ok=True)
        if self.d.free_bytes(self.cfg.run) < scfg.min_free_gib * GIB:
            raise SandboxError("not enough free disk space")
        deadline = self.d.clock() + self.d.slot_wait
        while True:
            self._check_stop()
            with slot_lock(self.cfg, self.cfg.slots) as slot:
                if slot is not None:
                    self.slot = slot
                    self._with_slot(slot)
                    return
            if self.d.clock() >= deadline:
                raise SandboxError("sandbox busy")
            self.d.sleep(SLOT_POLL)

    def _check_stop(self):
        if self.stop_event.is_set():
            raise _Stopped()

    def _with_slot(self, slot):
        job, scfg = self.job, self.scfg
        self._phase_end = self.d.clock() + scfg.install_minutes * 60
        self._phase_reason = "install timed out"
        self._tap_base = self.d.tap_bytes(slot)
        self.vm = self.d.vm_factory(self.cfg, slot, self.private_dir, job.deb_path if job.source == "deb" else None,
                                    self.d.sandbox_flag(self.cfg))
        self.set_state("starting")
        self.vm.boot_install()
        self._attach()
        self.set_state("installing")
        self._install()
        self._check_stop()
        self.guest.qga.sh("sync", timeout=120)
        with self._lock:
            self._halting = True
        self.set_state("starting")
        self.vm.halt()
        self.vm.boot_run()
        with self._lock:
            self._halting = False
        self._attach()
        self._launch_app(initial=True)
        with self._lock:
            now = self.d.clock()
            self._phase_end = None
            self.run_end = now + scfg.run_minutes * 60
            self.last_input = now
            self.state = "running"
            self._serving = True
        self.publish(self)
        log("session %s running (slot %d)" % (self.id, slot))
        while not self.stop_event.wait(1.0):
            pass
        self._serving = False

    def _attach(self):
        """Wait for the freshly booted guest (agent, then the tester's session) and bind the helpers to it."""
        self.vm.qga.wait_ready(BOOT_TIMEOUT)
        self.guest = self.d.guest_factory(self.vm.qga)
        self.guest.wait_session()
        self.sg = self.d.sg_factory(self.guest)
        self._check_stop()

    def _install(self):
        left = max(60.0, self._phase_end - self.d.clock())
        update = min(shots_job.APT_UPDATE_TIMEOUT, left / 3)
        self.d.install(self.guest, self.job, update, max(60.0, left - update))
        desktop = self.guest.find_desktop(self.job)
        if not desktop:
            raise SandboxError("no desktop file found for %s" % self.job.package)
        self.sg.ensure_locales()
        exec_value = self.sg.read_desktop_exec(desktop)
        self.argv = shots_scene.build_command(exec_value, []) if exec_value else None
        if not self.argv:
            raise SandboxError("cannot read the Exec line of the desktop file")

    def _launch_app(self, initial=False):
        if initial:
            self.base_pids = self.sg.tester_pids()
        else:
            self.sg.kill_new(self.base_pids)
        self.sg.start_app(self.argv, self.lang, timeout=self.scfg.total_minutes * 60 + APP_EXTRA_SECONDS,
                          discard_stdout=True)

    def _teardown(self):
        got = self._op.acquire(timeout=10)  # an operation in progress is interrupted by the kill below
        try:
            vm = self.vm
            if vm is not None:
                self._halting = True
                try:
                    vm.stop()
                except Exception as e:  # noqa: BLE001 - keep cleaning up
                    log("session %s: stopping the VM failed: %s" % (self.id, e))
                    vm.kill()
        finally:
            if got:
                self._op.release()
        with self._lock:
            if self.state not in ("failed", "ending"):
                self.state = "ending"
        self.finished.set()
        self.publish(self)

    # ---- watcher ----

    def tick(self):
        """One pass of the limit checks (called about once a second by the watcher thread)."""
        now = self.d.clock()
        with self._lock:
            state, halting = self.state, self._halting
        if state in ("ending", "failed"):
            return
        if now >= self.total_end:
            return self.request_end("total time limit reached")
        if state == "running":
            if now >= self.run_end:
                return self.request_end("run time limit reached")
            if now - self.last_input >= self.scfg.idle_minutes * 60:
                return self.request_end("idle timeout")
        if self._phase_end is not None and now >= self._phase_end:
            return self.fail(self._phase_reason)
        vm = self.vm
        if vm is None:
            return
        if not halting and vm.exited():
            return self.fail("the VM exited: %s" % vm.log_tail())
        if now - self._last_disk_check >= DISK_CHECK_SECONDS:
            self._last_disk_check = now
            if vm.disk_bytes() > self.scfg.overlay_cap_gib * GIB:
                return self.fail("disk usage limit reached")
            if self.slot is not None and self.d.tap_bytes(self.slot) - self._tap_base > self.scfg.traffic_cap_gib * GIB:
                return self.fail("network traffic limit reached")

    def watch(self):
        while not self.finished.wait(1.0):
            try:
                self.tick()
                self.publish(self)
            except Exception as e:  # noqa: BLE001 - the watcher must not die
                log("session %s: watcher error: %s: %s" % (self.id, type(e).__name__, e))

    # ---- operations (control protocol) ----

    @contextlib.contextmanager
    def operation(self):
        if not self._op.acquire(blocking=False):
            raise OpError("busy")
        try:
            if self.state != "running":
                raise OpError("not_running")
            yield
        finally:
            self._op.release()

    def op_capture(self, kind):
        with self.operation():
            capturer = Capturer(self.store, self.vm.qmp, self.vm.files.path("shot.ppm"),
                                self.d.locator_factory(self.sg))
            try:
                return capturer.capture(kind, self.lang)
            except CaptureError as e:
                log("session %s: capture %s failed: %s" % (self.id, kind, e))
                if e.qga_broken:
                    self._reconnect_qga()
                raise OpError(e.code) from None

    def op_captures(self):
        return self.store.snapshot()

    def op_get(self, n):
        try:
            data = self.store.read(n)
        except OSError:
            data = None
        if data is None:
            raise OpError("no_such_capture")
        return data

    def op_lang(self, lang):
        if lang not in shots_scene.LANGS:
            raise OpError("bad_request")
        with self.operation():
            old, self.lang = self.lang, lang
            try:
                self._relaunch()
            except OpError:
                self.lang = old
                raise

    def op_restart(self):
        with self.operation():
            self._relaunch()

    def _relaunch(self):
        try:
            self._launch_app()
        except InfraError as e:
            log("session %s: restarting the app failed: %s" % (self.id, e))
            self._reconnect_qga()
            raise OpError("guest_failed") from None

    def op_extend(self):
        with self._lock:
            if self.state != "running":
                raise OpError("not_running")
            if self.extended:
                raise OpError("already_extended")
            self.extended = True
            self.run_end += EXTEND_SECONDS
        self.publish(self)

    def op_end(self):
        self.request_end("")

    def op_rollback(self):
        with self.operation():
            self.drop_vnc()
            with self._lock:
                self._halting = True
                self._phase_end = self.d.clock() + ROLLBACK_SECONDS
                self._phase_reason = "rollback timed out"
            self.set_state("starting")
            try:
                self.vm.rollback()
                with self._lock:
                    self._halting = False
                self._attach()
                self._launch_app(initial=True)
            except _Stopped:
                raise OpError("not_running") from None
            except (SandboxError, OSError) as e:
                self.fail("rollback failed: %s" % e)
                raise OpError("rollback_failed") from None
            with self._lock:
                self._phase_end = None
                if self.state == "starting":
                    self.state = "running"
                self.last_input = self.d.clock()
            self.publish(self)

    def _reconnect_qga(self):
        """The agent channel is stale after a timeout: open a new one and resynchronise with guest-sync."""
        vm = self.vm
        try:
            with contextlib.suppress(Exception):
                vm.qga.close()
            qga = Qga(vm.files.path("qga.sock"))
            qga.wait_ready(20)
        except SandboxError as e:
            log("session %s: cannot reconnect the guest agent: %s" % (self.id, e))
            return
        vm.qga = qga
        self.guest.qga = qga
        self.sg.qga = qga

    def vnc_path(self):
        """Path of qemu's VNC socket while the session is running, else None."""
        with self._lock:
            return self.vm.files.path("vnc.sock") if self.state == "running" and self.vm is not None else None


class _Stopped(Exception):
    """The session was told to stop while a step was still pending."""
