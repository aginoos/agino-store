# SPDX-License-Identifier: GPL-3.0-or-later
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from session_helpers import FakeQmp, FakeShotsGuest, make_ppm, session_cfg  # noqa: E402

from agino_store_sandbox import session as S  # noqa: E402
from agino_store_sandbox.runner import slot_lock  # noqa: E402
from agino_store_sandbox.shots_job import InstallError  # noqa: E402
from agino_store_sandbox.util import InfraError, SandboxError  # noqa: E402

GIB = 1 << 30


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

    def wait_ready(self, timeout):
        self.calls.append("ready")

    def sh(self, script, args=(), timeout=60, input_data=None):
        self.calls.append(script)

    def close(self):
        pass


class FakeVm:
    instances = []

    def __init__(self, cfg, slot, private, deb_path, sandbox):
        self.slot, self.deb_path, self.sandbox = slot, deb_path, sandbox
        self.events, self.disk = [], 0
        self.killed = threading.Event()
        self.files = FakeFiles(private)
        self.qmp = FakeQmp(make_ppm(1920, 1080))
        self.qga = FakeQga()
        self._exited = False
        self.fail_boot_run = False
        FakeVm.instances.append(self)

    def boot_install(self):
        self.events.append("boot_install")

    def boot_run(self):
        if self.fail_boot_run:
            raise InfraError("qemu did not start")
        self.events.append("boot_run")

    def halt(self):
        self.events.append("halt")

    def rollback(self):
        self.halt()
        self.boot_run()

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
    def __init__(self, qga):
        self.qga = qga
        self.env = {"DISPLAY": ":0"}

    def wait_session(self, timeout=120):
        return self.env

    def find_desktop(self, job):
        return "/usr/share/applications/demo.desktop"


class FakeSg:
    def __init__(self):
        self.started, self.killed, self.pids_calls = [], [], 0
        self.block = None
        self.fail_start = False
        self.exec_value = "demo %U"

    def ensure_locales(self):
        pass

    def read_desktop_exec(self, path):
        return self.exec_value

    def tester_pids(self):
        self.pids_calls += 1
        return {1, 2}

    def kill_new(self, base):
        self.killed.append(set(base))

    def start_app(self, argv, lang, timeout=600, discard_stdout=False):
        if self.block is not None:
            self.block.wait(5)
        if self.fail_start:
            raise InfraError("guest agent exec timed out")
        self.started.append((list(argv), lang, timeout, discard_stdout))


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        FakeVm.instances = []
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
                      free_bytes=lambda path: self.free, clock=self.clock, sleep=self.clock.sleep, slot_wait=6)
        for k, v in over.items():
            setattr(deps, k, v)
        private = os.path.join(self.tmp.name, "private")
        os.makedirs(private, exist_ok=True)
        self.counter += 1
        session = S.Session(cfg, "demo", "%016x" % self.counter, "zh_CN", private, deps,
                            publish=lambda s: self.published.append(s.state), drop_vnc=self.drop)
        session.store.create_directory()
        return session

    def drop(self):
        self.dropped += 1

    def find_job(self, cfg, app):
        if self.job is None:
            raise SandboxError("no usable sandbox job for demo in the queue")
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

    def running(self, **kw):
        session = self.make(**kw)
        self.spawn(session)
        self.wait_for(session, "running")
        return session


class FakeLocator:
    def locate(self, screen):
        return (100, 100, 1000, 700), "Demo"


class LifecycleTests(Harness):
    def test_happy_path_and_order(self):
        session = self.running()
        vm = FakeVm.instances[0]
        self.assertEqual(vm.events, ["boot_install", "halt", "boot_run"])
        self.assertIsNone(vm.deb_path)
        self.assertEqual(len(self.installed), 1)
        self.assertEqual(self.sg.started, [(["demo"], "zh_CN", 3600 + 300, True)])
        self.assertEqual(session.status()["state"], "running")
        self.assertEqual(vm.qga.calls.count("sync"), 1)
        self.assertEqual(self.published[:1], ["starting"])
        session.op_end()
        self.wait_for(session, "ending")
        self.threads[0][0].join(5)
        self.assertEqual(vm.events[-1], "stop")
        self.assertTrue(session.finished.is_set())

    def test_deb_job_gets_the_deb_disk(self):
        self.job = FakeJob()
        self.job.source, self.job.deb_path = "deb", "/q/x.deb"
        self.running()
        self.assertEqual(FakeVm.instances[0].deb_path, "/q/x.deb")

    def test_install_phase_has_its_own_budget(self):
        self.running()
        job, update, install = self.installed[0]
        self.assertAlmostEqual(update + install, 15 * 60, delta=1)

    def test_install_failure(self):
        def boom():
            raise InstallError("apt-get install failed (exit 100)", 100, "tail")
        self.install_hook = boom
        session = self.make()
        self.spawn(session)
        self.wait_for(session, "failed")
        self.threads[0][0].join(5)
        self.assertIn("apt-get install failed", session.status()["error"])
        self.assertEqual(FakeVm.instances[0].events[-1], "stop")

    def test_no_desktop_file(self):
        FakeGuest.find_desktop = lambda self, job: None
        self.addCleanup(setattr, FakeGuest, "find_desktop", lambda self, job: "/usr/share/applications/demo.desktop")
        session = self.make()
        self.spawn(session)
        self.wait_for(session, "failed")
        self.assertIn("no desktop file", session.error)

    def test_unusable_exec_line(self):
        self.sg.exec_value = None
        session = self.make()
        self.spawn(session)
        self.wait_for(session, "failed")

    def test_no_job(self):
        self.job = None
        session = self.make()
        self.spawn(session)
        self.wait_for(session, "failed")
        self.assertEqual(FakeVm.instances, [])

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
        self.wait_for(session, "running")

    def test_end_while_installing_interrupts_the_install(self):
        entered = threading.Event()

        def hang():
            entered.set()
            FakeVm.instances[0].killed.wait(5)
            raise InfraError("peer closed the socket")
        self.install_hook = hang
        session = self.make()
        t = self.spawn(session)
        entered.wait(5)
        self.assertEqual(session.state, "installing")
        session.op_end()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(session.state, "ending")
        self.assertIn("kill", FakeVm.instances[0].events)

    def test_boot_failure_of_the_second_layer(self):
        def make_fail(*a):
            vm = FakeVm(*a)
            vm.fail_boot_run = True
            return vm
        session = self.make(vm_factory=make_fail)
        self.spawn(session)
        self.wait_for(session, "failed")
        self.assertIn("qemu did not start", session.error)

    def test_illegal_transitions_are_ignored(self):
        session = self.make()
        self.assertFalse(session.set_state("running"))
        self.assertEqual(session.state, "queued")
        self.assertTrue(session.set_state("starting"))
        session.request_end("x")
        self.assertFalse(session.set_state("running"))
        session.fail("late")
        self.assertEqual((session.state, session.error), ("ending", "x"))


class LimitTests(Harness):
    def test_idle_counts_only_input(self):
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

    def test_run_limit_and_one_extension(self):
        session = self.running()
        self.assertEqual(session.status()["remaining_s"], 30 * 60)
        session.touch_input()
        session.op_extend()
        self.assertTrue(session.status()["extended"])
        self.assertEqual(session.status()["remaining_s"], 45 * 60)
        with self.assertRaises(S.OpError) as ctx:
            session.op_extend()
        self.assertEqual(ctx.exception.code, "already_extended")
        for _ in range(4):  # keep the session non-idle until the extended run limit
            self.clock.t += 9 * 60
            session.touch_input()
            session.tick()
        self.assertEqual(session.state, "running")
        self.clock.t += 9 * 60
        session.touch_input()
        session.tick()
        self.assertEqual((session.state, session.error), ("ending", "run time limit reached"))

    def test_run_limit_without_extension(self):
        session = self.running()
        for _ in range(3):
            self.clock.t += 10 * 60 - 1
            session.touch_input()
            session.tick()
        self.assertEqual(session.state, "running")
        self.clock.t += 4
        session.tick()
        self.assertEqual(session.error, "run time limit reached")

    def test_total_limit_wins_over_extension(self):
        session = self.running()
        session.op_extend()
        session.run_end = self.clock.t + 3600 * 5
        self.clock.t += 61 * 60
        session.tick()
        self.assertEqual(session.error, "total time limit reached")
        self.assertEqual(session.state, "ending")

    def test_remaining_never_exceeds_total(self):
        session = self.running(cfg=session_cfg(self.tmp.name, total_minutes=40))
        self.clock.t += 25 * 60
        session.touch_input()
        session.op_extend()
        self.assertEqual(session.status()["remaining_s"], 15 * 60)  # 5 + 15 min of run time, but the total ends first

    def test_overlay_cap_is_checked_every_ten_seconds(self):
        session = self.running()
        vm = FakeVm.instances[0]
        session.tick()  # first check, small
        vm.disk = 9 * GIB
        self.clock.t += 5
        session.tick()
        self.assertEqual(session.state, "running")  # not yet 10 s since the last check
        self.clock.t += 10
        session.touch_input()
        session.tick()
        self.assertEqual((session.state, session.error), ("failed", "disk usage limit reached"))
        self.threads[0][0].join(5)
        self.assertEqual(vm.events[-1], "stop")  # the main thread wakes up and cleans up

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

    def test_install_phase_timeout(self):
        entered = threading.Event()

        def hang():
            entered.set()
            FakeVm.instances[0].killed.wait(5)
            raise InfraError("peer closed the socket")
        self.install_hook = hang
        session = self.make()
        t = self.spawn(session)
        entered.wait(5)
        self.clock.t += 15 * 60 + 1
        session.tick()
        t.join(5)
        self.assertEqual((session.state, session.error), ("failed", "install timed out"))

    def test_halting_vm_is_not_a_failure(self):
        session = self.running()
        vm = FakeVm.instances[0]
        vm._exited = True
        session._halting = True
        session.tick()
        self.assertEqual(session.state, "running")


class OperationTests(Harness):
    def test_everything_needs_a_running_session(self):
        session = self.make()
        for call in (lambda: session.op_capture("screen"), lambda: session.op_lang("en_US"), session.op_restart,
                     session.op_rollback, session.op_extend):
            with self.assertRaises(S.OpError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, "not_running")
        self.assertEqual(session.op_captures(), [])
        with self.assertRaises(S.OpError) as ctx:
            session.op_get(1)
        self.assertEqual(ctx.exception.code, "no_such_capture")

    def test_capture_screen_and_window(self):
        session = self.running()
        a = session.op_capture("screen")
        b = session.op_capture("window")
        self.assertEqual((a["n"], a["width"], a["lang"]), (1, 1920, "zh_CN"))
        self.assertEqual((b["n"], b["width"], b["height"], b["title"]), (2, 1000, 700, "Demo"))
        self.assertEqual(session.status()["captures"], 2)
        self.assertEqual([i["n"] for i in session.op_captures()], [1, 2])
        self.assertEqual(session.op_get(2)[:8], b"\x89PNG\r\n\x1a\n")
        self.assertTrue(FakeVm.instances[0].qmp.calls[0][0].endswith("private/shot.ppm"))

    def test_capture_limits_and_errors(self):
        cfg = session_cfg(self.tmp.name, max_captures=2)
        session = self.running(cfg=cfg)
        session.op_capture("screen")
        session.op_capture("screen")
        with self.assertRaises(S.OpError) as ctx:
            session.op_capture("screen")
        self.assertEqual(ctx.exception.code, "too_many")

    def test_capture_too_big(self):
        session = self.running()
        FakeVm.instances[0].qmp.data = make_ppm(1024, 1100, b"") + os.urandom(1024 * 1100 * 3)
        with self.assertRaises(S.OpError) as ctx:
            session.op_capture("screen")
        self.assertEqual(ctx.exception.code, "too_big")
        self.assertEqual(session.status()["captures"], 0)

    def test_capture_guest_failure_reconnects_the_agent(self):
        class Broken:
            def locate(self, screen):
                raise InfraError("timed out")
        calls = []
        session = self.running(locator_factory=lambda sg: Broken())
        session._reconnect_qga = lambda: calls.append("reconnect")
        with self.assertRaises(S.OpError) as ctx:
            session.op_capture("window")
        self.assertEqual(ctx.exception.code, "guest_failed")
        self.assertEqual(calls, ["reconnect"])
        session.op_capture("screen")  # a screen capture needs no guest agent

    def test_language_switch_restarts_the_app(self):
        session = self.running()
        session.op_lang("en_US")
        self.assertEqual(session.status()["lang"], "en_US")
        self.assertEqual([s[1] for s in self.sg.started], ["zh_CN", "en_US"])
        self.assertEqual(self.sg.killed, [{1, 2}])  # only processes that were not there at the start
        self.assertEqual(session.op_capture("screen")["lang"], "en_US")
        with self.assertRaises(S.OpError) as ctx:
            session.op_lang("fr_FR")
        self.assertEqual(ctx.exception.code, "bad_request")

    def test_restart_keeps_the_language(self):
        session = self.running()
        session.op_restart()
        self.assertEqual([s[1] for s in self.sg.started], ["zh_CN", "zh_CN"])

    def test_failed_switch_keeps_the_old_language(self):
        calls = []
        session = self.running()
        session._reconnect_qga = lambda: calls.append(1)
        self.sg.fail_start = True
        with self.assertRaises(S.OpError) as ctx:
            session.op_lang("en_US")
        self.assertEqual((ctx.exception.code, session.lang, calls), ("guest_failed", "zh_CN", [1]))

    def test_operations_do_not_overlap(self):
        session = self.running()
        self.sg.block = threading.Event()
        t = threading.Thread(target=session.op_restart)
        t.start()
        time.sleep(0.1)
        with self.assertRaises(S.OpError) as ctx:
            session.op_capture("screen")
        self.assertEqual(ctx.exception.code, "busy")
        self.assertEqual(session.status()["state"], "running")  # status is never blocked
        self.sg.block.set()
        t.join(5)
        session.op_capture("screen")

    def test_rollback(self):
        session = self.running()
        session.op_capture("screen")
        states = []
        session.publish = lambda s: states.append(s.state)
        session.op_rollback()
        vm = FakeVm.instances[0]
        self.assertEqual(vm.events, ["boot_install", "halt", "boot_run", "halt", "boot_run"])
        self.assertEqual(session.state, "running")
        self.assertIn("starting", states)
        self.assertEqual(self.dropped, 1)
        self.assertEqual(len(self.sg.started), 2)
        self.assertEqual(self.sg.pids_calls, 2)  # new baseline after the rollback
        self.assertEqual(session.status()["captures"], 1)  # captures survive
        self.assertEqual(self.sg.killed, [])

    def test_rollback_failure_fails_the_session(self):
        session = self.running()
        FakeVm.instances[0].fail_boot_run = True
        with self.assertRaises(S.OpError) as ctx:
            session.op_rollback()
        self.assertEqual(ctx.exception.code, "rollback_failed")
        self.assertEqual(session.state, "failed")

    def test_vnc_only_while_running(self):
        session = self.make()
        self.assertIsNone(session.vnc_path())
        session = self.running()
        self.assertTrue(session.vnc_path().endswith("private/vnc.sock"))
        session.op_end()
        self.assertIsNone(session.vnc_path())

    def test_end_works_in_every_state(self):
        session = self.make()
        session.op_end()
        self.assertEqual(session.state, "ending")
        self.assertTrue(session.stop_event.is_set())

    def test_status_shape(self):
        session = self.running()
        self.assertEqual(sorted(session.status()), sorted(
            ["ok", "state", "id", "app", "lang", "remaining_s", "idle_s", "captures", "extended", "error"]))


if __name__ == "__main__":
    unittest.main()
