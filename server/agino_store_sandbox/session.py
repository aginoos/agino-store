# SPDX-License-Identifier: GPL-3.0-or-later
"""The interactive screenshot session: one VM per reviewer, reused for many apps, behind a lock-protected state machine.

States: queued -> starting -> idle -> installing -> running -> restoring -> idle (or installing again for a
rollback of the same app); failed_app (the app could not be installed or started; the VM is fine) leads to
restoring as well; ending or failed at any point. The VM boots once, and when the desktop is up its memory and
disk are saved as the snapshot `clean`; finishing an app and rolling back both load it (no reboot).
All guest agent and QMP calls happen inside an operation (one at a time, `_op`) or on the main thread, never on a
VNC relay thread, and each call has a deadline. A watcher thread enforces the limits; it only sets state and kills
the qemu we started, the main thread does the clean-up (docs/sandbox.md section 9).
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
from .runner import session_sandbox_flag, slot_lock
from .session_capture import Capturer, CaptureStore, CaptureError, GuestLocator
from .session_vm import SessionVm
from .shots_guest import ShotsGuest
from .util import InfraError, SandboxError, clean, log

STATES = ("queued", "starting", "idle", "installing", "running", "restoring", "failed_app", "ending", "failed")
ALLOWED = {"queued": {"starting", "failed", "ending"},
           "starting": {"idle", "failed", "ending"},
           "idle": {"installing", "failed", "ending"},
           "installing": {"running", "failed_app", "failed", "ending"},
           "running": {"restoring", "failed", "ending"},
           "failed_app": {"restoring", "failed", "ending"},
           "restoring": {"idle", "installing", "failed", "ending"},
           "failed": {"ending"},
           "ending": set()}
SLOT_RE = re.compile(r"\Avm[0-9]\Z")
SESSION_ID_RE = re.compile(r"\A[0-9a-f]{16}\Z")
APP_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,63}\Z")
DEFAULT_LANG = "zh_CN"
BOOT_TIMEOUT = 180
SLOT_WAIT = 60
SLOT_POLL = 2
EXTEND_SECONDS = 30 * 60
ROLLBACK_SECONDS = 300
SETTLE_SECONDS = 20  # the desktop gets this long to calm down before the clean snapshot is taken
DESKTOP_SETTLE_SECONDS = 9  # same after the display manager was restarted for a desktop language switch
QGA_REUSE_SECONDS = 20
QGA_RECONNECT_SECONDS = 60
DISK_CHECK_SECONDS = 10
APP_EXTRA_SECONDS = 300
ERROR_LIMIT = 200
GIB = 1 << 30
IDLE_STATES = ("idle", "running", "failed_app")  # the idle timeout runs only while the reviewer is expected to act
VNC_STATES = ("idle", "installing", "running", "failed_app")


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
    sandbox_flag: object = session_sandbox_flag
    tap_bytes: object = tap_bytes
    free_bytes: object = lambda path: shutil.disk_usage(path).free  # noqa: E731
    clock: object = time.monotonic
    sleep: object = time.sleep
    wall_ns: object = time.time_ns
    slot_wait: float = SLOT_WAIT
    settle: float = SETTLE_SECONDS
    desktop_settle: float = DESKTOP_SETTLE_SECONDS


class Session:
    def __init__(self, cfg, slot, session_id, private_dir, deps=None, publish=None, drop_vnc=None):
        self.cfg, self.slot, self.id = cfg, slot, session_id
        self.scfg = cfg.session
        self.private_dir = private_dir
        self.d = deps or Deps()
        self.publish = publish or (lambda session: None)
        self.drop_vnc = drop_vnc or (lambda: None)
        self.store = CaptureStore(os.path.join(self.scfg.captures, session_id), session_id, self.scfg.max_captures)
        self._lock = threading.RLock()
        self._op = threading.Lock()
        self.stop_event = threading.Event()
        self.finished = threading.Event()
        self.state, self.error = "queued", ""
        self.extended = False
        self.app, self.lang, self.desktop_lang = "", DEFAULT_LANG, DEFAULT_LANG
        self.vm = self.guest = self.sg = self.job = self.vm_slot = None
        self.argv = None
        self.base_pids = self.clean_pids = set()
        self._halting = False
        self._serving = False
        self._phase_end = None
        self._phase_reason = ""
        self._tap_base = 0
        self._last_disk_check = 0.0
        now = self.d.clock()
        self.t0 = now
        self.total_end = now + self.scfg.total_minutes * 60
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

    def _app_failed(self, reason):
        """The app could not be installed or started: the VM is fine, the reviewer finishes the app (restores)."""
        with self._lock:
            if self.state != "installing":
                return
            self.error = clean(reason, ERROR_LIMIT)
            self._phase_end = None
            self.last_input = self.d.clock()
        log("session %s: app %s failed: %s" % (self.id, self.app, self.error))
        self.set_state("failed_app")

    def status(self):
        with self._lock:
            now = self.d.clock()
            return {"ok": True, "state": self.state, "id": self.id, "slot": self.slot, "app": self.app,
                    "lang": self.lang, "desktop_lang": self.desktop_lang,
                    "remaining_s": int(max(0, self.total_end - now)),
                    "idle_s": int(max(0, now - self.last_input)) if self.state in IDLE_STATES else 0,
                    "captures": self.store.count(), "extended": self.extended, "error": self.error}

    # ---- main thread ----

    def run(self):
        """Queue, boot, snapshot, serve until stopped, clean up. Never raises."""
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
        os.makedirs(self.cfg.run, mode=0o755, exist_ok=True)
        if self.d.free_bytes(self.cfg.run) < scfg.min_free_gib * GIB:
            raise SandboxError("not enough free disk space")
        deadline = self.d.clock() + self.d.slot_wait
        while True:
            self._check_stop()
            with slot_lock(self.cfg, self.cfg.slots) as slot:
                if slot is not None:
                    self.vm_slot = slot
                    self._with_slot(slot)
                    return
            if self.d.clock() >= deadline:
                raise SandboxError("sandbox busy")
            self.d.sleep(SLOT_POLL)

    def _check_stop(self):
        if self.stop_event.is_set():
            raise _Stopped()

    def _with_slot(self, slot):
        scfg = self.scfg
        self._phase_end = self.d.clock() + scfg.install_minutes * 60
        self._phase_reason = "boot timed out"
        self._tap_base = self.d.tap_bytes(slot)
        self.vm = self.d.vm_factory(self.cfg, slot, self.private_dir, self.d.sandbox_flag(self.cfg))
        self.set_state("starting")
        self.vm.boot()
        self.vm.qga.wait_ready(BOOT_TIMEOUT)
        self.guest = self.d.guest_factory(self.vm.qga)
        self.guest.wait_session()
        self.sg = self.d.sg_factory(self.guest)
        self._check_stop()
        self.sg.ensure_locales()  # part of the clean state, so no app install has to generate them
        self._settle(self.d.settle)
        self.clean_pids = set(self.sg.tester_pids())
        self.base_pids = set(self.clean_pids)
        self.vm.snapshot_save()
        with self._lock:
            self._phase_end = None
            self.last_input = self.d.clock()
            self._serving = True
        self.set_state("idle")
        log("session %s idle on %s (VM slot %d)" % (self.id, self.slot, slot))
        while not self.stop_event.wait(1.0):
            pass
        self._serving = False

    def _settle(self, seconds):
        end = self.d.clock() + seconds
        while self.d.clock() < end:
            self._check_stop()
            self.d.sleep(min(1.0, max(0.0, end - self.d.clock())))
        self._check_stop()

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
        if state in IDLE_STATES and now - self.last_input >= self.scfg.idle_minutes * 60:
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
            if self.vm_slot is not None \
                    and self.d.tap_bytes(self.vm_slot) - self._tap_base > self.scfg.traffic_cap_gib * GIB:
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
    def operation(self, *states):
        """One operation at a time, only in one of `states` (bad_state otherwise, busy while another runs).
        An operation may pass the lock on to the install thread: it sets `.handed_over` on the yielded token."""
        self._require(states)
        if not self._op.acquire(blocking=False):
            raise OpError("busy")
        token = _Token()
        try:
            self._require(states)
            yield token
        finally:
            if not token.handed_over:
                self._op.release()

    def _require(self, states):
        with self._lock:
            if self.state not in states:
                raise OpError("bad_state")

    def op_capture(self, kind):
        with self.operation("running"):
            capturer = Capturer(self.store, self.vm.qmp, self.vm.files.path("shot.ppm"),
                                self.d.locator_factory(self.sg))
            try:
                return capturer.capture(kind, self.lang, self.app, self.desktop_lang)
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

    def op_select_app(self, app, lang):
        """idle -> installing (asynchronous: the install and the start run on their own thread)."""
        if not isinstance(app, str) or not APP_RE.match(app) or lang not in shots_scene.LANGS:
            raise OpError("bad_request")
        with self.operation("idle") as op:
            try:
                job = self.d.find_job(self.cfg, app)
            except SandboxError as e:
                log("session %s: no job for %s: %s" % (self.id, app, e))
                raise OpError("no_job") from None
            self._begin_install(op, app, lang, job)

    def _begin_install(self, op, app, lang, job):
        with self._lock:
            self.app, self.lang, self.job, self.error = app, lang, job, ""
            self._phase_end = self.d.clock() + self.scfg.install_minutes * 60
            self._phase_reason = "install timed out"
        if not self.set_state("installing"):
            raise OpError("bad_state")
        op.handed_over = True  # the install thread releases `_op`
        threading.Thread(target=self._install_thread, daemon=True).start()

    def _install_thread(self):
        """Install and start the app, then give up the operation lock and only then publish the outcome, so that a
        client that sees `running` (or `failed_app`) can use the next operation at once."""
        failure = None
        try:
            self._install_and_launch()
        except _Stopped:
            failure = ""
        except (SandboxError, OSError) as e:
            failure = str(e) or type(e).__name__
        except Exception as e:  # noqa: BLE001 - a bug here must not leave the session stuck in installing
            log("session %s: unexpected %s while installing: %s" % (self.id, type(e).__name__, e))
            failure = "internal error: %s" % type(e).__name__
        finally:
            self._op.release()
        if failure is None:
            with self._lock:
                self._phase_end = None
                self.last_input = self.d.clock()
            self.set_state("running")
        elif failure:
            self._app_failed(failure)

    def _install_and_launch(self):
        job, vm = self.job, self.vm
        if job.source == "deb":
            vm.plug_deb(job.deb_path)
        try:
            self._install(job)
        finally:
            if job.source == "deb" and not self.stop_event.is_set():
                vm.unplug_deb()
        self._check_stop()
        self._launch_app()

    def _install(self, job):
        left = max(60.0, self._phase_end - self.d.clock())
        update = min(shots_job.APT_UPDATE_TIMEOUT, left / 3)
        self.d.install(self.guest, job, update, max(60.0, left - update))
        desktop = self.guest.find_desktop(job)
        if not desktop:
            raise SandboxError("no desktop file found for %s" % job.package)
        exec_value = self.sg.read_desktop_exec(desktop)
        self.argv = shots_scene.build_command(exec_value, []) if exec_value else None
        if not self.argv:
            raise SandboxError("cannot read the Exec line of the desktop file")

    def _launch_app(self):
        """(Re)start the app in tester's session: whatever tester started since the clean state goes first."""
        self.sg.kill_new(self.base_pids)
        left = max(60.0, self.total_end - self.d.clock()) + EXTEND_SECONDS + APP_EXTRA_SECONDS
        self.sg.start_app(self.argv, self.lang, timeout=left, discard_stdout=True)

    def op_lang(self, lang, desktop=False):
        if lang not in shots_scene.LANGS or not isinstance(desktop, bool):
            raise OpError("bad_request")
        with self.operation("running"):
            old = self.lang
            self.lang = lang
            try:
                if desktop:
                    self._switch_desktop(lang)
                self._launch_app()
            except _Stopped:
                raise OpError("bad_state") from None
            except InfraError as e:
                self.lang = old
                log("session %s: switching the language failed: %s" % (self.id, e))
                self._reconnect_qga()
                raise OpError("guest_failed") from None

    def _switch_desktop(self, lang):
        """Whole-desktop language (docs/sandbox.md): clean up like the window switch, change the four layers and
        restart the display manager, wait for the new session, then take the new baseline of tester's processes.
        The recorded desktop language changes when the display manager was restarted, not before."""
        self.sg.kill_new(self.base_pids)
        self.sg.switch_desktop_locale(shots_scene.LOCALES[lang])
        self.desktop_lang = lang
        self.guest.wait_session()
        self._settle(self.d.desktop_settle)
        self.guest.wait_session()  # the session environment may have changed while the desktop settled
        self.base_pids = set(self.sg.tester_pids())

    def op_restart(self):
        with self.operation("running"):
            try:
                self._launch_app()
            except InfraError as e:
                log("session %s: restarting the app failed: %s" % (self.id, e))
                self._reconnect_qga()
                raise OpError("guest_failed") from None

    def op_extend(self):
        with self._lock:
            if self.state in ("ending", "failed"):
                raise OpError("bad_state")
            if self.extended:
                raise OpError("already_extended")
            self.extended = True
            self.total_end += EXTEND_SECONDS
        self.publish(self)

    def op_end(self):
        self.request_end("")

    def op_finish_app(self):
        """running (or failed_app) -> restoring -> idle; the captures stay."""
        with self.operation("running", "failed_app"):
            self._restore()
            with self._lock:
                self.app, self.lang, self.job, self.argv, self.error = "", DEFAULT_LANG, None, None, ""
                self.last_input = self.d.clock()
            self.set_state("idle")

    def op_rollback(self):
        """Back to the clean state and the same app again: the restore is done when this returns, the install of
        the app runs on after it (state installing)."""
        with self.operation("running", "failed_app") as op:
            app, lang, job = self.app, self.lang, self.job
            self._restore()
            self._begin_install(op, app, lang, job)

    def _restore(self):
        """snapshot-load `clean`, then agent, clock and session; the desktop is the clean Chinese one again."""
        self.drop_vnc()
        with self._lock:
            self._phase_end = self.d.clock() + ROLLBACK_SECONDS
            self._phase_reason = "rollback timed out"
        if not self.set_state("restoring"):
            raise OpError("bad_state")
        try:
            vm = self.vm
            vm.unplug_deb()
            vm.snapshot_load()
            try:
                vm.qga.wait_ready(QGA_REUSE_SECONDS)
            except InfraError:
                self._open_qga(QGA_RECONNECT_SECONDS)
            vm.qga.set_time(self.d.wall_ns())
            self.guest.wait_session()
            self._check_stop()
        except _Stopped:
            raise OpError("bad_state") from None
        except (SandboxError, OSError) as e:
            self.fail("rollback failed: %s" % e)
            raise OpError("rollback_failed") from None
        with self._lock:
            self._phase_end = None
            self.desktop_lang = DEFAULT_LANG
            self.base_pids = set(self.clean_pids)

    def _open_qga(self, timeout):
        """A new agent channel (the old one is closed) and resynchronise with guest-sync; InfraError if that fails."""
        vm = self.vm
        with contextlib.suppress(Exception):
            vm.qga.close()
        qga = Qga(vm.files.path("qga.sock"))
        qga.wait_ready(timeout)
        vm.qga = qga
        self.guest.qga = qga
        self.sg.qga = qga

    def _reconnect_qga(self):
        """The agent channel is stale after a timeout: open a new one and resynchronise with guest-sync."""
        try:
            self._open_qga(20)
        except SandboxError as e:
            log("session %s: cannot reconnect the guest agent: %s" % (self.id, e))

    def vnc_path(self):
        """Path of qemu's VNC socket while the reviewer can use the desktop, else None."""
        with self._lock:
            ok = self.state in VNC_STATES and self.vm is not None
            return self.vm.files.path("vnc.sock") if ok else None


class _Token:
    """Handed to the body of `operation()`; `handed_over` tells that another thread now owns the operation lock."""
    handed_over = False


class _Stopped(Exception):
    """The session was told to stop while a step was still pending."""
