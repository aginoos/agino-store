# SPDX-License-Identifier: GPL-3.0-or-later
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_helpers import (DEB, Clock, VmFactory, builder_cfg, read_json,  # noqa: E402
                           task_doc, write_task)

from agino_store_sandbox import build_job as J  # noqa: E402
from agino_store_sandbox import build_out as O  # noqa: E402
from agino_store_sandbox import build_serve as S  # noqa: E402
from agino_store_sandbox import cli  # noqa: E402
from agino_store_sandbox.util import SandboxError  # noqa: E402

IDS = ["%016x" % n for n in (0xa1, 0xa2, 0xa3, 0xa4)]


class FakeJob:
    """Stands in for BuildJob: records concurrency, waits for a gate or a stop request, writes a terminal status."""
    active = 0
    peak = 0
    lock = threading.Lock()
    slots = []

    def __init__(self, cfg, task, slot, gate=None, final="done"):
        self.cfg, self.task, self.slot, self.gate, self.final = cfg, task, slot, gate, final
        self.finished, self.stop = threading.Event(), threading.Event()
        self.state, self.stop_state, self.stop_reason = "starting", None, None
        self.cancel_path = os.path.join(cfg.builder.queue, task.id, "cancel")

    def cancel_requested(self):
        return os.path.lexists(self.cancel_path)

    def request_stop(self, state, reason):
        self.stop_state, self.stop_reason = state, reason
        self.stop.set()

    def run(self):
        with FakeJob.lock:
            FakeJob.active += 1
            FakeJob.peak = max(FakeJob.peak, FakeJob.active)
            FakeJob.slots.append(self.slot)
        try:
            if self.gate is not None:
                while not (self.gate.is_set() or self.stop.is_set()):
                    time.sleep(0.005)
            else:
                time.sleep(0.02)
            self.state = self.stop_state if self.stop.is_set() else self.final
            out = O.OutDir(self.cfg.builder.out, self.task.id, 1 << 20)
            out.create()
            out.write_result(self.state == "done", self.stop_reason or "", [], [])
            out.write_status(self.state, "t", 0, 0, "")
            out.close()
        finally:
            with FakeJob.lock:
                FakeJob.active -= 1
            self.finished.set()


class ServeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        FakeJob.active = FakeJob.peak = 0
        FakeJob.slots = []

    def cfg(self, **builder):
        self.config = builder_cfg(self.tmp.name, **builder)
        return self.config

    def daemon(self, cfg=None, once=True, factory=None, **kwargs):
        cfg = cfg or self.config
        return S.BuildDaemon(cfg, once=once, job_factory=factory or FakeJob, sleep=lambda s: time.sleep(0.005),
                             **kwargs)

    def status(self, task_id):
        return O.read_status(self.config.builder.out, task_id)

    def result(self, task_id):
        return read_json(os.path.join(self.config.builder.out, task_id, "result.json"))


class QueueScanTests(ServeCase):
    def test_candidates_are_oldest_first_and_well_formed_only(self):
        cfg = self.cfg()
        for i, task_id in enumerate(IDS[:3]):
            base = write_task(cfg, task_id)
            stamp = 1_000_000 - i * 100  # IDS[2] is the oldest
            os.utime(os.path.join(base, "task.json"), (stamp, stamp))
        os.makedirs(os.path.join(cfg.builder.queue, "not-a-task"))
        os.makedirs(os.path.join(cfg.builder.queue, "ABCDEF0123456789"))
        with open(os.path.join(cfg.builder.queue, "stray-file"), "w") as f:
            f.write("x")
        self.assertEqual(self.daemon().candidates(), [IDS[2], IDS[1], IDS[0]])

    def test_incomplete_and_recently_written_entries_wait(self):
        cfg = self.cfg()
        write_task(cfg, IDS[0], source=None)  # source.tar missing
        write_task(cfg, IDS[1])
        d = self.daemon(once=False)
        self.assertEqual(d.candidates(), [])  # IDS[1] is younger than the settle time
        d.wall = lambda: time.time() + 3600
        self.assertEqual(d.candidates(), [IDS[1]])

    def test_tasks_with_a_status_are_never_started_again(self):
        cfg = self.cfg()
        write_task(cfg, IDS[0])
        out = O.OutDir(cfg.builder.out, IDS[0], 100)
        out.create()
        out.write_status("done", "t")
        out.close()
        self.assertEqual(self.daemon().candidates(), [])

    def test_symlinked_queue_entries_are_ignored(self):
        cfg = self.cfg()
        real = write_task(cfg, IDS[0])
        os.symlink(real, os.path.join(cfg.builder.queue, IDS[1]))
        self.assertEqual(self.daemon().candidates(), [IDS[0]])

    def test_missing_queue_directory(self):
        self.cfg()
        self.assertEqual(self.daemon().candidates(), [])


class RunTests(ServeCase):
    def test_max_concurrent_is_respected(self):
        cfg = self.cfg(max_concurrent=1)
        for task_id in IDS[:3]:
            write_task(cfg, task_id)
        d = self.daemon()
        self.assertEqual(d.run(), 0)
        self.assertEqual(FakeJob.peak, 1)
        self.assertEqual(sorted(FakeJob.slots), [0, 0, 0])
        for task_id in IDS[:3]:
            self.assertEqual(self.status(task_id)["state"], "done")

    def test_two_slots_run_in_parallel_with_distinct_slots(self):
        cfg = self.cfg(max_concurrent=2)
        for task_id in IDS[:4]:
            write_task(cfg, task_id)
        gate = threading.Event()
        threading.Timer(0.3, gate.set).start()
        d = self.daemon(factory=lambda c, t, s: FakeJob(c, t, s, gate=gate))
        d.run()
        self.assertEqual(FakeJob.peak, 2)
        self.assertEqual(sorted(FakeJob.slots[:2]), [0, 1])
        self.assertEqual(sorted(set(FakeJob.slots)), [0, 1])

    def test_never_more_active_than_slots_in_one_tick(self):
        cfg = self.cfg(max_concurrent=2)
        for task_id in IDS[:4]:
            write_task(cfg, task_id)
        gate = threading.Event()
        d = self.daemon(factory=lambda c, t, s: FakeJob(c, t, s, gate=gate))
        d.tick()
        self.assertEqual(len(d.active), 2)
        self.assertEqual(d.free_slots, [])
        gate.set()
        d.run()
        self.assertEqual(len(d.active), 0)

    def test_failed_task_makes_once_exit_nonzero(self):
        cfg = self.cfg()
        write_task(cfg, IDS[0])
        d = self.daemon(factory=lambda c, t, s: FakeJob(c, t, s, final="failed"))
        self.assertEqual(d.run(), 1)

    def test_invalid_task_json_is_recorded_as_failed(self):
        cfg = self.cfg()
        write_task(cfg, IDS[0], raw=b'{"app": "x"}')
        d = self.daemon()
        self.assertEqual(d.run(), 1)
        self.assertEqual(self.status(IDS[0])["state"], "failed")
        res = self.result(IDS[0])
        self.assertFalse(res["ok"])
        self.assertTrue(res["error"].startswith("invalid task:"))
        self.assertEqual(FakeJob.slots, [])

    def test_limits_above_the_ceiling_are_invalid(self):
        cfg = self.cfg(limits="cpu_minutes=10")
        write_task(cfg, IDS[0], doc=task_doc(limits={"cpu_minutes": 20}))
        self.daemon().run()
        self.assertIn("cpu_minutes", self.result(IDS[0])["error"])

    def test_cancel_before_the_start(self):
        cfg = self.cfg()
        base = write_task(cfg, IDS[0])
        open(os.path.join(base, "cancel"), "w").close()
        d = self.daemon()
        self.assertEqual(d.run(), 0)
        self.assertEqual(self.status(IDS[0])["state"], "cancelled")
        self.assertFalse(self.result(IDS[0])["ok"])
        self.assertEqual(FakeJob.slots, [])

    def test_cancel_of_a_running_task_reaches_the_job(self):
        cfg = self.cfg()
        base = write_task(cfg, IDS[0])
        gate = threading.Event()
        d = self.daemon(factory=lambda c, t, s: FakeJob(c, t, s, gate=gate))
        d.tick()
        job = d.active[IDS[0]][0]
        self.assertIsNone(job.stop_state)
        open(os.path.join(base, "cancel"), "w").close()
        d.tick()
        self.assertEqual((job.stop_state, job.stop_reason), ("cancelled", "cancelled"))
        d.run()
        self.assertEqual(self.status(IDS[0])["state"], "cancelled")

    def test_job_that_cannot_be_created_is_failed_not_fatal(self):
        cfg = self.cfg()
        write_task(cfg, IDS[0])
        write_task(cfg, IDS[1])

        def factory(c, t, s):
            if t.id == IDS[0]:
                raise RuntimeError("boom")
            return FakeJob(c, t, s)
        d = self.daemon(factory=factory)
        self.assertEqual(d.run(), 1)
        self.assertEqual(self.status(IDS[0])["state"], "failed")
        self.assertEqual(self.result(IDS[0])["error"], "internal error: RuntimeError")
        self.assertEqual(self.status(IDS[1])["state"], "done")
        self.assertEqual(d.free_slots, [0])

    def test_job_thread_that_dies_without_a_result_is_not_restarted(self):
        cfg = self.cfg()
        write_task(cfg, IDS[0])

        class Silent(FakeJob):
            def run(self):
                self.finished.set()

        starts = []
        d = self.daemon(factory=lambda c, t, s: starts.append(t.id) or Silent(c, t, s))
        d.run()
        self.assertEqual(starts, [IDS[0]])

    def test_stop_ends_running_jobs_as_failed(self):
        cfg = self.cfg()
        write_task(cfg, IDS[0])
        gate = threading.Event()
        d = self.daemon(once=False, factory=lambda c, t, s: FakeJob(c, t, s, gate=gate))
        d.wall = lambda: time.time() + 3600
        d.tick()
        self.assertEqual(len(d.active), 1)
        d.stop()
        d.run()
        self.assertEqual(self.status(IDS[0])["state"], "failed")
        self.assertEqual(self.result(IDS[0])["error"], "the build runner was stopped")


class OrphanTests(ServeCase):
    def test_a_task_stuck_in_building_after_a_restart_is_failed(self):
        cfg = self.cfg()
        write_task(cfg, IDS[0])
        out = O.OutDir(cfg.builder.out, IDS[0], 100)
        out.create()
        out.write_status("building", "t", 2, 5)
        out.close()
        d = self.daemon()
        d.run()
        self.assertEqual(self.status(IDS[0])["state"], "failed")
        self.assertIn("stopped while this task was running", self.result(IDS[0])["error"])
        self.assertEqual(FakeJob.slots, [])  # and it did not start again

    def test_terminal_tasks_are_left_alone(self):
        cfg = self.cfg()
        out = O.OutDir(cfg.builder.out, IDS[0], 100)
        out.create()
        out.write_result(True, "", [], [])
        out.write_status("done", "t")
        out.close()
        self.daemon().sweep_orphans()
        self.assertEqual(self.status(IDS[0])["state"], "done")
        self.assertTrue(self.result(IDS[0])["ok"])

    def test_unreadable_status_is_replaced(self):
        cfg = self.cfg()
        os.makedirs(os.path.join(cfg.builder.out, IDS[0]))
        with open(os.path.join(cfg.builder.out, IDS[0], "status.json"), "w") as f:
            f.write("{broken")
        self.daemon().sweep_orphans()
        self.assertEqual(self.status(IDS[0])["state"], "failed")

    def test_active_tasks_are_not_swept(self):
        cfg = self.cfg()
        write_task(cfg, IDS[0])
        gate = threading.Event()
        d = self.daemon(factory=lambda c, t, s: FakeJob(c, t, s, gate=gate))
        d.tick()
        out = O.OutDir(cfg.builder.out, IDS[0], 100)
        out.create()
        out.write_status("building", "t")
        out.close()
        d.sweep_orphans()
        self.assertEqual(self.status(IDS[0])["state"], "building")
        gate.set()
        d.run()


class CleanupTests(ServeCase):
    def test_old_outputs_are_removed_by_the_daemon(self):
        cfg = self.cfg()
        out = O.OutDir(cfg.builder.out, IDS[0], 100)
        out.create()
        out.write_status("done", "t")
        out.close()
        old = time.time() - 8 * 24 * 3600
        os.utime(os.path.join(cfg.builder.out, IDS[0], "status.json"), (old, old))
        self.daemon().tick()
        self.assertFalse(os.path.exists(os.path.join(cfg.builder.out, IDS[0])))


class LockAndCliTests(ServeCase):
    def test_only_one_daemon_may_run(self):
        cfg = self.cfg()
        fd = S.acquire_lock(cfg)
        try:
            with self.assertRaises(SandboxError):
                S.acquire_lock(cfg)
        finally:
            os.close(fd)
        os.close(S.acquire_lock(cfg))

    def test_build_run_command(self):
        path = os.path.join(self.tmp.name, "sandbox.conf")
        with open(path, "w") as f:
            f.write("[builder]\nqueue = %s/bq\nout = %s/bo\n" % (self.tmp.name, self.tmp.name))
        with mock.patch.object(S, "serve", return_value=0) as serve:
            self.assertEqual(cli.main(["--config", path, "build-run", "--once"]), 0)
        self.assertEqual(serve.call_args[0][1], True)
        with mock.patch.object(S, "serve", return_value=0) as serve:
            cli.main(["--config", path, "build-run"])
        self.assertEqual(serve.call_args[0][1], False)


class EndToEndTests(ServeCase):
    """The real BuildJob behind the real daemon, with the fake VM."""

    def test_a_queued_task_becomes_a_deb(self):
        cfg = self.cfg()
        write_task(cfg, IDS[0])
        clock = Clock()
        factory = VmFactory()
        deps = J.Deps(factory, clock, clock.sleep, lambda path: 1 << 50)
        d = S.BuildDaemon(cfg, once=True, deps=deps, sleep=lambda s: time.sleep(0.005))
        self.assertEqual(d.run(), 0)
        self.assertEqual(self.status(IDS[0])["state"], "done")
        res = self.result(IDS[0])
        self.assertTrue(res["ok"])
        self.assertEqual(res["artifacts"][0]["size"], len(DEB))
        self.assertTrue(factory.vms[0].stopped)
        with open(os.path.join(cfg.builder.out, IDS[0], "demo-agino_1.0+agino1_amd64.deb"), "rb") as f:
            self.assertEqual(f.read(), DEB)
        # the same queue entry is not built again
        d2 = S.BuildDaemon(cfg, once=True, deps=deps, sleep=lambda s: time.sleep(0.005))
        d2.run()
        self.assertEqual(factory.calls, 1)

    def test_result_json_has_exactly_the_contract_keys(self):
        cfg = self.cfg()
        write_task(cfg, IDS[0])
        clock = Clock()
        deps = J.Deps(VmFactory(), clock, clock.sleep, lambda path: 1 << 50)
        S.BuildDaemon(cfg, once=True, deps=deps, sleep=lambda s: time.sleep(0.005)).run()
        res = self.result(IDS[0])
        self.assertEqual(sorted(res), ["artifacts", "class", "error", "ok", "steps"])
        self.assertEqual(res["class"], "ok")
        self.assertEqual(sorted(res["steps"][0]), ["cmd", "exit", "n", "seconds"])
        self.assertEqual(sorted(res["artifacts"][0]), ["file", "sha256", "size"])
        self.assertEqual(len(res["artifacts"][0]["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
