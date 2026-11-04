# SPDX-License-Identifier: GPL-3.0-or-later
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from session_helpers import FakeQmp, make_ppm, session_cfg  # noqa: E402

from agino_store_sandbox import session as S  # noqa: E402
from agino_store_sandbox.runner import slot_lock  # noqa: E402
from agino_store_sandbox.shots_job import InstallError  # noqa: E402
from agino_store_sandbox.util import InfraError, SandboxError  # noqa: E402

GIB = 1 << 30
WALL_NS = 1_700_000_000_123_456_789


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, n):
        self.t += n


class FakeJob:
    app, package, source, deb_path, l10n = "demo", "demo-pkg", "milkyway", None, ()
    launchable = None


class FakeFiles:
    def __init__(self, private):
        self.private = private

    def path(self, name):
        return os.path.join(self.private, name)


class FakeQga:
    def __init__(self):
        self.calls = []
        self.ready_failures = 0
        self.fail_set_time = False

    def wait_ready(self, timeout):
        self.calls.append(("ready", timeout))
        if self.ready_failures:
            self.ready_failures -= 1
            raise InfraError("guest agent did not answer")

    def set_time(self, ns):
        if self.fail_set_time:
            raise InfraError("guest-set-time failed")
        self.calls.append(("set_time", ns))

    def sh(self, script, args=(), timeout=60, input_data=None):
        self.calls.append(("sh", script))

    def close(self):
        pass


class FakeVm:
    instances = []

    def __init__(self, cfg, slot, private, sandbox):
        self.slot, self.sandbox = slot, sandbox
        self.events, self.disk = [], 0
        self.killed = threading.Event()
        self.files = FakeFiles(private)
        self.qmp = FakeQmp(make_ppm(1920, 1080))
        self.qga = FakeQga()
        self._exited = False
        self.fail_boot = self.fail_save = self.fail_load = self.fail_unplug = False
        FakeVm.instances.append(self)

    def boot(self):
        if self.fail_boot:
            raise InfraError("qemu did not start")
        self.events.append("boot")

    def snapshot_save(self):
        if self.fail_save:
            raise InfraError("snapshot-save failed: no space")
        self.events.append("save")

    def snapshot_load(self):
        if self.fail_load:
            raise InfraError("snapshot-load failed: bad state")
        self.events.append("load")

    def plug_deb(self, path):
        self.events.append("plug " + path)

    def unplug_deb(self):
        if self.fail_unplug:
            raise InfraError("the guest did not release the deb disk")
        self.events.append("unplug")

    def stop(self):
        self.events.append("stop")

    def kill(self):
        self.events.append("kill")
        self.killed.set()

    def exited(self):
        return self._exited

    def disk_bytes(self):
        return self.disk

    def log_tail(self):
        return "boom"


class FakeGuest:
    sessions = 0

    def __init__(self, qga):
        self.qga = qga
        self.env = {"DISPLAY": ":0"}

    def wait_session(self, timeout=120):
        FakeGuest.sessions += 1
        return self.env

    def find_desktop(self, job):
        return "/usr/share/applications/demo.desktop"


class FakeSg:
    def __init__(self):
        self.started, self.killed, self.pids_calls = [], [], 0
        self.block = None
        self.fail_start = False
        self.fail_switch = False
        self.exec_value = "demo %U"
        self.locales, self.switched = 0, []
        self.pids = {1, 2}
        self.qga = None

    def ensure_locales(self):
        self.locales += 1

    def read_desktop_exec(self, path):
        return self.exec_value

    def tester_pids(self):
        self.pids_calls += 1
        return set(self.pids)

    def kill_new(self, base):
        self.killed.append(set(base))

    def switch_desktop_locale(self, locale):
        if self.fail_switch:
            raise InfraError("switching the desktop language failed at the user manager (exit 1)")
        self.switched.append(locale)
        self.pids = {1, 2, 40, 41}  # a new desktop session has new processes

    def start_app(self, argv, lang, timeout=600, discard_stdout=False):
        if self.block is not None:
            self.block.wait(5)
        if self.fail_start:
            raise InfraError("guest agent exec timed out")
        self.started.append((list(argv), lang, timeout, discard_stdout))


class FakeLocator:
    def locate(self, screen):
        return (100, 100, 1000, 700), "Demo"


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        FakeVm.instances = []
        FakeGuest.sessions = 0
        self.clock = Clock()
        self.sg = FakeSg()
        self.installed = []
        self.dropped = 0
        self.published = []
        self.install_hook = None
        self.traffic = 0
        self.free = 100 * GIB
        self.job = FakeJob()
        self.cfg = session_cfg(self.tmp.name)
        self.threads = []
        self.counter = 0
        self.addCleanup(self.stop_all)

    def make(self, cfg=None, **over):
        cfg = cfg or self.cfg
        deps = S.Deps(vm_factory=FakeVm, guest_factory=FakeGuest, sg_factory=lambda g: self.sg,
                      install=self.fake_install, locator_factory=lambda sg: FakeLocator(),
                      find_job=self.find_job, sandbox_flag=lambda c: False, tap_bytes=lambda slot: self.traffic,
                      free_bytes=lambda path: self.free, clock=self.clock, sleep=self.clock.sleep,
                      wall_ns=lambda: WALL_NS, slot_wait=6, settle=0, desktop_settle=0)
        for k, v in over.items():
            setattr(deps, k, v)
        private = os.path.join(self.tmp.name, "private")
        os.makedirs(private, exist_ok=True)
        self.counter += 1
        session = S.Session(cfg, "vm0", "%016x" % self.counter, private, deps,
                            publish=lambda s: self.published.append(s.state), drop_vnc=self.drop)
        session.store.create_directory()
        return session

    def drop(self):
        self.dropped += 1

    def find_job(self, cfg, app):
        if self.job is None:
            raise SandboxError("no usable sandbox job for %s in the queue" % app)
        return self.job

    def fake_install(self, guest, job, update_timeout, install_timeout):
        self.installed.append((job, update_timeout, install_timeout))
        if self.install_hook:
            self.install_hook()

    def spawn(self, session):
        t = threading.Thread(target=session.run, daemon=True)
        t.start()
        self.threads.append((t, session))
        return t

    def stop_all(self):
        for t, session in self.threads:
            session.request_end("test over")
            t.join(5)

    def wait_for(self, session, state, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if session.state == state:
                return
            time.sleep(0.01)
        self.fail("state is %s, expected %s (error: %s)" % (session.state, state, session.error))

    def idle(self, **kw):
        session = self.make(**kw)
        self.spawn(session)
        self.wait_for(session, "idle")
        return session

    def running(self, app="demo", lang="zh_CN", **kw):
        session = self.idle(**kw)
        session.op_select_app(app, lang)
        self.wait_for(session, "running")
        return session

    def code(self, call, *args):
        with self.assertRaises(S.OpError) as ctx:
            call(*args)
        return ctx.exception.code


class BootTests(Harness):
    def test_boot_snapshot_then_idle(self):
        session = self.idle()
        vm = FakeVm.instances[0]
        self.assertEqual(vm.events, ["boot", "save"])
        self.assertEqual(self.sg.locales, 1)  # generated before the snapshot, so it is part of the clean state
        self.assertEqual(self.published[:1], ["starting"])
        status = session.status()
        self.assertEqual((status["state"], status["app"], status["slot"], status["desktop_lang"]),
                         ("idle", "", "vm0", "zh_CN"))
        self.assertEqual(self.installed, [])
        self.assertEqual(self.sg.started, [])
        session.op_end()
        self.wait_for(session, "ending")
        self.threads[0][0].join(5)
        self.assertEqual(vm.events[-1], "stop")
        self.assertTrue(session.finished.is_set())

    def test_snapshot_waits_for_the_desktop_to_settle(self):
        session = self.make(settle=20)
        self.spawn(session)
        self.wait_for(session, "idle")
        self.assertGreaterEqual(self.clock.t, 1000 + 20)

    def test_boot_failure(self):
        def make_fail(*a):
            vm = FakeVm(*a)
            vm.fail_boot = True
            return vm
        session = self.make(vm_factory=make_fail)
        self.spawn(session)
        self.wait_for(session, "failed")
        self.assertIn("qemu did not start", session.error)

    def test_snapshot_failure_fails_the_session(self):
        def make_fail(*a):
            vm = FakeVm(*a)
            vm.fail_save = True
            return vm
        session = self.make(vm_factory=make_fail)
        self.spawn(session)
        self.wait_for(session, "failed")
        self.assertIn("snapshot-save failed", session.error)
        self.threads[0][0].join(5)
        self.assertEqual(FakeVm.instances[0].events[-1], "stop")

    def test_low_disk_refuses_to_start(self):
        self.free = 19 * GIB
        session = self.make()
        self.spawn(session)
        self.wait_for(session, "failed")
        self.assertEqual(session.error, "not enough free disk space")
        self.assertEqual(FakeVm.instances, [])

    def test_slot_busy_after_waiting(self):
        cfg = session_cfg(self.tmp.name, sandbox={"slots": "1"})
        with slot_lock(cfg, 1) as slot:
            self.assertEqual(slot, 0)
            session = self.make(cfg=cfg)
            self.spawn(session)
            self.wait_for(session, "failed")
        self.assertEqual(session.error, "sandbox busy")
        self.assertEqual(FakeVm.instances, [])
        self.assertGreaterEqual(self.clock.t, 1000 + 6)  # it did wait (fake clock advanced by the retries)

    def test_slot_becomes_free(self):
        cfg = session_cfg(self.tmp.name, sandbox={"slots": "1"})
        lock = slot_lock(cfg, 1)
        lock.__enter__()
        released = []

        def sleeper(n):
            if not released:
                released.append(1)
                lock.__exit__(None, None, None)
            self.clock.sleep(n)
        session = self.make(cfg=cfg, sleep=sleeper)
        self.spawn(session)
        self.wait_for(session, "idle")

    def test_illegal_transitions_are_ignored(self):
        session = self.make()
        self.assertFalse(session.set_state("running"))
        self.assertFalse(session.set_state("idle"))
        self.assertEqual(session.state, "queued")
        self.assertTrue(session.set_state("starting"))
        self.assertFalse(session.set_state("installing"))
        session.request_end("x")
        self.assertFalse(session.set_state("idle"))
        session.fail("late")
        self.assertEqual((session.state, session.error), ("ending", "x"))

    def test_end_works_in_every_state(self):
        session = self.make()
        session.op_end()
        self.assertEqual(session.state, "ending")
        self.assertTrue(session.stop_event.is_set())


class InstallTests(Harness):
    def test_select_app_installs_and_starts(self):
        session = self.idle()
        session.op_select_app("demo", "en_US")
        self.wait_for(session, "running")
        self.assertEqual(len(self.installed), 1)
        self.assertEqual(self.sg.started, [(["demo"], "en_US", self.sg.started[0][2], True)])
        self.assertEqual(self.sg.killed, [{1, 2}])
        status = session.status()
        self.assertEqual((status["app"], status["lang"], status["desktop_lang"]), ("demo", "en_US", "zh_CN"))
        self.assertEqual(FakeVm.instances[0].events, ["boot", "save"])  # no reboot, no second layer
        self.assertIn("installing", self.published)

    def test_app_timeout_covers_the_session_with_one_extension(self):
        self.running()
        timeout = self.sg.started[0][2]
        self.assertGreaterEqual(timeout, 120 * 60 + 30 * 60)

    def test_install_budget_is_per_app(self):
        self.running()
        job, update, install = self.installed[0]
        self.assertAlmostEqual(update + install, 15 * 60, delta=1)

    def test_deb_disk_is_plugged_for_the_install_and_unplugged_after_it(self):
        self.job = FakeJob()
        self.job.source, self.job.deb_path = "deb", "/q/x.deb"
        seen = []
        self.install_hook = lambda: seen.append(list(FakeVm.instances[0].events))
        self.running()
        self.assertEqual(seen[0][-1], "plug /q/x.deb")  # attached while the install runs
        self.assertEqual(FakeVm.instances[0].events[-1], "unplug")

    def test_apt_job_gets_no_deb_disk(self):
        self.running()
        self.assertFalse([e for e in FakeVm.instances[0].events if e.startswith("plug")])

    def test_select_app_needs_idle_and_a_job_and_valid_input(self):
        session = self.idle()
        self.assertEqual(self.code(session.op_select_app, "../x", "zh_CN"), "bad_request")
        self.assertEqual(self.code(session.op_select_app, "Demo", "zh_CN"), "bad_request")
        self.assertEqual(self.code(session.op_select_app, "demo", "fr_FR"), "bad_request")
        self.assertEqual(self.code(session.op_select_app, 5, "zh_CN"), "bad_request")
        self.job = None
        self.assertEqual(self.code(session.op_select_app, "demo", "zh_CN"), "no_job")
        self.assertEqual(session.state, "idle")  # still usable
        self.job = FakeJob()
        session.op_select_app("demo", "zh_CN")
        self.wait_for(session, "running")
        self.assertEqual(self.code(session.op_select_app, "demo", "zh_CN"), "bad_state")

    def test_select_app_in_the_wrong_states(self):
        session = self.make()
        self.assertEqual(self.code(session.op_select_app, "demo", "zh_CN"), "bad_state")  # queued

    def test_select_app_is_asynchronous(self):
        gate = threading.Event()
        self.install_hook = lambda: gate.wait(5)
        session = self.idle()
        session.op_select_app("demo", "zh_CN")
        self.assertEqual(session.state, "installing")
        self.assertEqual(self.code(session.op_select_app, "demo", "zh_CN"), "bad_state")
        self.assertEqual(self.code(session.op_capture, "screen"), "bad_state")
        self.assertEqual(self.code(session.op_finish_app), "bad_state")
        gate.set()
        self.wait_for(session, "running")

    def test_install_failure_leaves_the_vm_usable(self):
        def boom():
            raise InstallError("apt-get install failed (exit 100)", 100, "tail")
        self.install_hook = boom
        session = self.idle()
        session.op_select_app("demo", "zh_CN")
        self.wait_for(session, "failed_app")
        self.assertIn("apt-get install failed", session.status()["error"])
        self.assertEqual(session.status()["app"], "demo")
        self.assertNotIn("stop", FakeVm.instances[0].events)
        self.install_hook = None
        session.op_finish_app()
        self.assertEqual((session.state, session.error, session.app), ("idle", "", ""))
        session.op_select_app("demo", "zh_CN")
        self.wait_for(session, "running")

    def test_no_desktop_file(self):
        FakeGuest.find_desktop = lambda self, job: None
        self.addCleanup(setattr, FakeGuest, "find_desktop", lambda self, job: "/usr/share/applications/demo.desktop")
        session = self.idle()
        session.op_select_app("demo", "zh_CN")
        self.wait_for(session, "failed_app")
        self.assertIn("no desktop file", session.error)

    def test_unusable_exec_line(self):
        self.sg.exec_value = None
        session = self.idle()
        session.op_select_app("demo", "zh_CN")
        self.wait_for(session, "failed_app")

    def test_app_start_failure(self):
        self.sg.fail_start = True
        session = self.idle()
        session.op_select_app("demo", "zh_CN")
        self.wait_for(session, "failed_app")
        self.assertIn("timed out", session.error)

    def test_deb_that_cannot_be_unplugged_fails_the_app(self):
        self.job = FakeJob()
        self.job.source, self.job.deb_path = "deb", "/q/x.deb"

        def make(*a):
            vm = FakeVm(*a)
            vm.fail_unplug = True
            return vm
        session = self.idle(vm_factory=make)
        session.op_select_app("demo", "zh_CN")
        self.wait_for(session, "failed_app")
        self.assertIn("did not release", session.error)

    def test_end_while_installing_interrupts_the_install(self):
        entered = threading.Event()

        def hang():
            entered.set()
            FakeVm.instances[0].killed.wait(5)
            raise InfraError("peer closed the socket")
        self.install_hook = hang
        session = self.idle()
        session.op_select_app("demo", "zh_CN")
        entered.wait(5)
        self.assertEqual(session.state, "installing")
        session.op_end()
        self.threads[0][0].join(5)
        self.assertFalse(self.threads[0][0].is_alive())
        self.assertEqual(session.state, "ending")
        self.assertIn("kill", FakeVm.instances[0].events)

    def test_install_phase_timeout_fails_the_session(self):
        entered = threading.Event()

        def hang():
            entered.set()
            FakeVm.instances[0].killed.wait(5)
            raise InfraError("peer closed the socket")
        self.install_hook = hang
        session = self.idle()
        session.op_select_app("demo", "zh_CN")
        entered.wait(5)
        self.clock.t += 15 * 60 + 1
        session.tick()
        self.threads[0][0].join(5)
        self.assertEqual((session.state, session.error), ("failed", "install timed out"))


class RestoreTests(Harness):
    def test_finish_app_loads_the_snapshot_and_fixes_the_clock(self):
        session = self.running()
        session.op_capture("screen")
        session.op_lang("en_US", True)
        vm = FakeVm.instances[0]
        sessions = FakeGuest.sessions
        states = []
        session.publish = lambda s: states.append(s.state)
        session.op_finish_app()
        self.assertEqual(vm.events[-2:], ["unplug", "load"])
        self.assertEqual(vm.qga.calls[-1], ("set_time", WALL_NS))
        self.assertEqual(vm.qga.calls[-2][0], "ready")
        self.assertEqual(FakeGuest.sessions, sessions + 1)  # the session environment is read again
        self.assertEqual(session.state, "idle")
        self.assertIn("restoring", states)
        status = session.status()
        self.assertEqual((status["app"], status["lang"], status["desktop_lang"], status["captures"], status["error"]),
                         ("", "zh_CN", "zh_CN", 1, ""))
        self.assertEqual(self.dropped, 1)
        self.assertEqual(vm.events.count("boot"), 1)  # never rebooted

    def test_finish_app_then_next_app(self):
        session = self.running()
        session.op_finish_app()
        session.op_select_app("demo", "en_US")
        self.wait_for(session, "running")
        self.assertEqual([s[1] for s in self.sg.started], ["zh_CN", "en_US"])
        self.assertEqual(session.status()["lang"], "en_US")

    def test_finish_app_needs_an_app(self):
        session = self.idle()
        self.assertEqual(self.code(session.op_finish_app), "bad_state")

    def test_baseline_of_processes_is_the_clean_one_again(self):
        session = self.running()
        session.op_lang("en_US", True)
        self.assertEqual(session.base_pids, {1, 2, 40, 41})
        session.op_finish_app()
        self.assertEqual(session.base_pids, {1, 2})

    def test_rollback_restores_and_installs_the_same_app_again(self):
        session = self.running(lang="en_US")
        session.op_capture("screen")
        session.op_rollback()
        self.wait_for(session, "running")
        vm = FakeVm.instances[0]
        self.assertIn("load", vm.events)
        self.assertEqual(len(self.installed), 2)
        self.assertEqual([s[1] for s in self.sg.started], ["en_US", "en_US"])
        self.assertEqual(session.status()["captures"], 1)  # captures survive
        self.assertEqual(session.status()["app"], "demo")

    def test_rollback_from_a_failed_app(self):
        calls = []
        self.install_hook = lambda: (calls.append(1), (_ for _ in ()).throw(InstallError("x", 1, "")))[1] \
            if len(calls) == 0 else None
        session = self.idle()
        session.op_select_app("demo", "zh_CN")
        self.wait_for(session, "failed_app")
        session.op_rollback()
        self.wait_for(session, "running")

    def test_rollback_states_are_checked(self):
        session = self.idle()
        self.assertEqual(self.code(session.op_rollback), "bad_state")

    def test_reconnects_the_agent_when_the_old_channel_is_dead(self):
        session = self.running()
        vm = FakeVm.instances[0]
        vm.qga.ready_failures = 1
        opened = []
        session._open_qga = opened.append
        session.op_finish_app()
        self.assertEqual(opened, [S.QGA_RECONNECT_SECONDS])
        self.assertEqual(session.state, "idle")

    def test_agent_that_never_comes_back_fails_the_session(self):
        session = self.running()
        FakeVm.instances[0].qga.ready_failures = 1

        def dead(timeout):
            raise InfraError("guest agent did not answer within %d s" % timeout)
        session._open_qga = dead
        self.assertEqual(self.code(session.op_finish_app), "rollback_failed")
        self.assertEqual(session.state, "failed")
        self.assertIn("rollback failed", session.error)

    def test_snapshot_load_failure_fails_the_session(self):
        session = self.running()
        FakeVm.instances[0].fail_load = True
        self.assertEqual(self.code(session.op_rollback), "rollback_failed")
        self.assertEqual(session.state, "failed")

    def test_clock_failure_fails_the_session(self):
        session = self.running()
        FakeVm.instances[0].qga.fail_set_time = True
        self.assertEqual(self.code(session.op_finish_app), "rollback_failed")
        self.assertEqual(session.state, "failed")

    def test_restore_has_its_own_budget(self):
        session = self.running()
        gate = threading.Event()
        vm = FakeVm.instances[0]
        original = vm.snapshot_load
        vm.snapshot_load = lambda: (gate.wait(5), original())[1]
        t = threading.Thread(target=session.op_finish_app)
        t.start()
        self.wait_for(session, "restoring")
        self.clock.t += S.ROLLBACK_SECONDS + 1
        session.tick()
        self.assertEqual((session.state, session.error), ("failed", "rollback timed out"))
        gate.set()
        t.join(5)


class LanguageTests(Harness):
    def test_window_switch_restarts_only_the_app(self):
        session = self.running()
        session.op_lang("en_US")
        status = session.status()
        self.assertEqual((status["lang"], status["desktop_lang"]), ("en_US", "zh_CN"))
        self.assertEqual([s[1] for s in self.sg.started], ["zh_CN", "en_US"])
        self.assertEqual(self.sg.killed, [{1, 2}, {1, 2}])  # only processes that were not there at the start
        self.assertEqual(self.sg.switched, [])
        self.assertEqual(session.op_capture("screen")["lang"], "en_US")
        self.assertEqual(self.code(session.op_lang, "fr_FR"), "bad_request")
        self.assertEqual(self.code(session.op_lang, "en_US", "yes"), "bad_request")

    def test_desktop_switch_runs_the_fixed_steps_and_restarts_the_app(self):
        session = self.running()
        sessions = FakeGuest.sessions
        session.op_lang("en_US", True)
        self.assertEqual(self.sg.switched, ["en_US.UTF-8"])
        status = session.status()
        self.assertEqual((status["lang"], status["desktop_lang"]), ("en_US", "en_US"))
        self.assertEqual(self.sg.started[-1][1], "en_US")
        self.assertEqual(FakeGuest.sessions, sessions + 2)  # after the restart and after the desktop settled
        self.assertEqual(self.sg.killed[1], {1, 2})  # the cleanup of the window switch comes first
        self.assertEqual(self.sg.killed[-1], {1, 2, 40, 41})  # the app start uses the new session's baseline
        shot = session.op_capture("screen")
        self.assertEqual((shot["lang"], shot["desktop_lang"], shot["app"]), ("en_US", "en_US", "demo"))

    def test_desktop_switch_waits_for_the_desktop_to_settle(self):
        session = self.running(desktop_settle=9)
        t0 = self.clock.t
        session.op_lang("en_US", True)
        self.assertGreaterEqual(self.clock.t - t0, 9)

    def test_failed_desktop_switch_keeps_the_recorded_state(self):
        calls = []
        session = self.running()
        session._reconnect_qga = lambda: calls.append(1)
        self.sg.fail_switch = True
        self.assertEqual(self.code(session.op_lang, "en_US", True), "guest_failed")
        status = session.status()
        self.assertEqual((status["lang"], status["desktop_lang"], calls), ("zh_CN", "zh_CN", [1]))
        self.assertEqual(session.state, "running")

    def test_failed_app_start_after_the_desktop_switch_records_the_new_desktop(self):
        session = self.running()
        session._reconnect_qga = lambda: None
        self.sg.fail_start = True
        self.assertEqual(self.code(session.op_lang, "en_US", True), "guest_failed")
        self.assertEqual(session.status()["desktop_lang"], "en_US")  # lightdm was restarted: the desktop is English
        self.assertEqual(session.status()["lang"], "zh_CN")

    def test_rollback_returns_the_desktop_to_chinese(self):
        session = self.running()
        session.op_lang("en_US", True)
        session.op_rollback()
        self.wait_for(session, "running")
        self.assertEqual(session.status()["desktop_lang"], "zh_CN")

    def test_failed_window_switch_keeps_the_old_language(self):
        calls = []
        session = self.running()
        session._reconnect_qga = lambda: calls.append(1)
        self.sg.fail_start = True
        self.assertEqual(self.code(session.op_lang, "en_US"), "guest_failed")
        self.assertEqual((session.status()["lang"], calls), ("zh_CN", [1]))

    def test_restart_keeps_the_language(self):
        session = self.running(lang="en_US")
        session.op_restart()
        self.assertEqual([s[1] for s in self.sg.started], ["en_US", "en_US"])

    def test_language_operations_need_a_running_app(self):
        session = self.idle()
        self.assertEqual(self.code(session.op_lang, "en_US"), "bad_state")
        self.assertEqual(self.code(session.op_lang, "en_US", True), "bad_state")
        self.assertEqual(self.code(session.op_restart), "bad_state")


class LimitTests(Harness):
    def test_idle_counts_only_input_while_the_reviewer_is_expected_to_act(self):
        session = self.running()
        self.clock.t += 9 * 60
        session.tick()
        self.assertEqual(session.state, "running")
        self.assertEqual(session.status()["idle_s"], 540)
        session.touch_input()
        self.clock.t += 9 * 60
        session.tick()
        self.assertEqual(session.state, "running")
        self.clock.t += 61
        session.tick()
        self.assertEqual((session.state, session.error), ("ending", "idle timeout"))

    def test_idle_timeout_also_applies_to_an_idle_vm(self):
        session = self.idle()
        self.clock.t += 10 * 60 + 1
        session.tick()
        self.assertEqual((session.state, session.error), ("ending", "idle timeout"))

    def test_no_idle_timeout_while_installing(self):
        gate = threading.Event()
        self.install_hook = lambda: gate.wait(5)
        session = self.idle()
        session.op_select_app("demo", "zh_CN")
        self.clock.t += 14 * 60
        session.tick()
        self.assertEqual(session.state, "installing")
        self.assertEqual(session.status()["idle_s"], 0)
        gate.set()
        self.wait_for(session, "running")

    def test_idle_clock_restarts_when_the_app_is_ready(self):
        gate = threading.Event()
        self.install_hook = lambda: (gate.wait(5), setattr(self.clock, "t", self.clock.t + 14 * 60))
        session = self.idle()
        session.touch_input()
        session.op_select_app("demo", "zh_CN")
        gate.set()
        self.wait_for(session, "running")
        session.tick()
        self.assertEqual(session.state, "running")

    def test_total_limit_is_120_minutes_and_one_extension_adds_30(self):
        session = self.running()
        self.assertEqual(session.status()["remaining_s"], 120 * 60 - 0)
        session.op_extend()
        self.assertTrue(session.status()["extended"])
        self.assertEqual(session.status()["remaining_s"], 150 * 60)
        self.assertEqual(self.code(session.op_extend), "already_extended")
        for _ in range(16):  # keep the session non-idle until just before the extended limit
            self.clock.t += 9 * 60
            session.touch_input()
            session.tick()
        self.assertEqual(session.state, "running")
        self.clock.t += 9 * 60
        session.touch_input()
        session.tick()
        self.assertEqual((session.state, session.error), ("ending", "total time limit reached"))

    def test_total_limit_without_extension(self):
        session = self.running()
        for _ in range(13):
            self.clock.t += 9 * 60
            session.touch_input()
            session.tick()
        self.assertEqual(session.state, "running")
        self.clock.t += 9 * 60
        session.touch_input()
        session.tick()
        self.assertEqual(session.error, "total time limit reached")

    def test_extend_works_before_an_app_is_chosen(self):
        session = self.idle()
        session.op_extend()
        self.assertEqual(session.status()["remaining_s"], 150 * 60)

    def test_overlay_cap_is_12_gib_checked_every_ten_seconds(self):
        session = self.running()
        vm = FakeVm.instances[0]
        session.tick()  # first check, small
        vm.disk = 13 * GIB
        self.clock.t += 5
        session.tick()
        self.assertEqual(session.state, "running")  # not yet 10 s since the last check
        self.clock.t += 10
        session.touch_input()
        session.tick()
        self.assertEqual((session.state, session.error), ("failed", "disk usage limit reached"))
        self.threads[0][0].join(5)
        self.assertEqual(vm.events[-1], "stop")  # the main thread wakes up and cleans up

    def test_11_gib_is_fine(self):
        session = self.running()
        FakeVm.instances[0].disk = 11 * GIB
        self.clock.t += 11
        session.touch_input()
        session.tick()
        self.assertEqual(session.state, "running")

    def test_traffic_cap(self):
        session = self.running()
        self.traffic = 2 * GIB + 1
        self.clock.t += 11
        session.touch_input()
        session.tick()
        self.assertEqual(session.error, "network traffic limit reached")

    def test_vm_exit_is_a_failure(self):
        session = self.running()
        FakeVm.instances[0]._exited = True
        session.tick()
        self.assertEqual(session.state, "failed")
        self.assertIn("the VM exited", session.error)

    def test_halting_vm_is_not_a_failure(self):
        session = self.running()
        vm = FakeVm.instances[0]
        vm._exited = True
        session._halting = True
        session.tick()
        self.assertEqual(session.state, "running")


class OperationTests(Harness):
    def test_operations_need_the_right_state(self):
        session = self.idle()
        for call in (lambda: session.op_capture("screen"), lambda: session.op_lang("en_US"), session.op_restart,
                     session.op_rollback, session.op_finish_app):
            self.assertEqual(self.code(call), "bad_state")
        self.assertEqual(session.op_captures(), [])
        self.assertEqual(self.code(session.op_get, 1), "no_such_capture")

    def test_extend_is_refused_once_the_session_is_over(self):
        session = self.make()
        session.request_end("x")
        self.assertEqual(self.code(session.op_extend), "bad_state")

    def test_capture_screen_and_window(self):
        session = self.running(app="demo")
        a = session.op_capture("screen")
        b = session.op_capture("window")
        self.assertEqual((a["n"], a["width"], a["lang"], a["app"], a["desktop_lang"]), (1, 1920, "zh_CN", "demo", "zh_CN"))
        self.assertEqual((b["n"], b["width"], b["height"], b["title"]), (2, 1000, 700, "Demo"))
        self.assertEqual(session.status()["captures"], 2)
        self.assertEqual([i["n"] for i in session.op_captures()], [1, 2])
        self.assertEqual(session.op_get(2)[:8], b"\x89PNG\r\n\x1a\n")
        self.assertTrue(FakeVm.instances[0].qmp.calls[0][0].endswith("private/shot.ppm"))

    def test_captures_of_several_apps_share_one_numbering(self):
        session = self.running()
        session.op_capture("screen")
        session.op_finish_app()
        self.job = FakeJob()
        session.op_select_app("other", "en_US")
        self.wait_for(session, "running")
        item = session.op_capture("screen")
        self.assertEqual((item["n"], item["app"], item["lang"]), (2, "other", "en_US"))
        self.assertEqual([(i["n"], i["app"]) for i in session.op_captures()], [(1, "demo"), (2, "other")])

    def test_capture_limits_and_errors(self):
        cfg = session_cfg(self.tmp.name, max_captures=2)
        session = self.running(cfg=cfg)
        session.op_capture("screen")
        session.op_capture("screen")
        self.assertEqual(self.code(session.op_capture, "screen"), "too_many")

    def test_capture_too_big(self):
        session = self.running()
        FakeVm.instances[0].qmp.data = make_ppm(1024, 1100, b"") + os.urandom(1024 * 1100 * 3)
        self.assertEqual(self.code(session.op_capture, "screen"), "too_big")
        self.assertEqual(session.status()["captures"], 0)

    def test_capture_guest_failure_reconnects_the_agent(self):
        class Broken:
            def locate(self, screen):
                raise InfraError("timed out")
        calls = []
        session = self.running(locator_factory=lambda sg: Broken())
        session._reconnect_qga = lambda: calls.append("reconnect")
        self.assertEqual(self.code(session.op_capture, "window"), "guest_failed")
        self.assertEqual(calls, ["reconnect"])
        session.op_capture("screen")  # a screen capture needs no guest agent

    def test_operations_do_not_overlap(self):
        session = self.running()
        self.sg.block = threading.Event()
        t = threading.Thread(target=session.op_restart)
        t.start()
        time.sleep(0.1)
        self.assertEqual(self.code(session.op_capture, "screen"), "busy")
        self.assertEqual(session.status()["state"], "running")  # status is never blocked
        self.sg.block.set()
        t.join(5)
        session.op_capture("screen")

    def test_vnc_is_available_while_the_reviewer_can_use_the_desktop(self):
        session = self.make()
        self.assertIsNone(session.vnc_path())
        session = self.idle()
        self.assertTrue(session.vnc_path().endswith("private/vnc.sock"))
        session.op_select_app("demo", "zh_CN")
        self.wait_for(session, "running")
        self.assertTrue(session.vnc_path().endswith("private/vnc.sock"))
        session.op_end()
        self.assertIsNone(session.vnc_path())

    def test_vnc_is_closed_while_restoring(self):
        session = self.running()
        gate = threading.Event()
        vm = FakeVm.instances[0]
        original = vm.snapshot_load
        vm.snapshot_load = lambda: (gate.wait(5), original())[1]
        t = threading.Thread(target=session.op_finish_app)
        t.start()
        self.wait_for(session, "restoring")
        self.assertIsNone(session.vnc_path())
        gate.set()
        t.join(5)
        self.assertIsNotNone(session.vnc_path())

    def test_status_shape(self):
        session = self.running()
        self.assertEqual(sorted(session.status()), sorted(
            ["ok", "state", "id", "slot", "app", "lang", "desktop_lang", "remaining_s", "idle_s", "captures",
             "extended", "error"]))


if __name__ == "__main__":
    unittest.main()
