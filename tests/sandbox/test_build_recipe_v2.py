# SPDX-License-Identifier: GPL-3.0-or-later
"""Recipe v2 in the build job: source layout, files, AGINO_* variables, the test phase and the failure class."""
import dataclasses
import hashlib
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_helpers import DEB, TASK_ID, task_doc  # noqa: E402
from test_build_job import GIB, JobCase  # noqa: E402

from agino_store_sandbox import build_guest as G  # noqa: E402
from agino_store_sandbox.build_out import OutDir  # noqa: E402
from agino_store_sandbox.util import InfraError  # noqa: E402

SHA = "a" * 64
COMMIT = "c" * 40
SOURCES = [{"name": "src", "kind": "git", "url": "https://github.com/x/y.git", "ref": "v1.0"},
           {"name": "vendor", "kind": "tarball", "url": "https://dl.example.com/app.tar.gz", "sha256": SHA}]


def v2_doc(**recipe):
    doc = task_doc(upstream_version="1.0", tag="v1.0", commit=COMMIT)
    doc["recipe"]["sources"] = SOURCES
    doc["recipe"].update(recipe)
    return doc


FILES = [{"path": "agino/launcher.sh", "mode": "0755", "content": "#!/bin/sh\nexec app \"$@\"\n"},
         {"path": "agino/app.desktop", "mode": "0644", "content": "[Desktop Entry]\nName=App\n"}]


class LayoutAndFilesTests(JobCase):
    def test_files_are_written_as_builder_in_order_before_the_dependencies(self):
        self.assertEqual(self.make(v2_doc(files=FILES)).run(), "done")
        qga = self.factory.vms[0].qga
        self.assertEqual(qga.placed, [(f["path"], f["mode"], f["content"].encode()) for f in FILES])
        argv = [a for kind, a in qga.execs if kind == "exec" and G.PLACE_FILE in a][-1]
        self.assertEqual(argv[:4], [G.RUNUSER, "-u", "builder", "--"])
        self.assertEqual(argv[-2:], ["agino/app.desktop", "0644"])
        self.assertEqual(qga.files[G.PLACE_FILE].decode(), G.PLACE_FILE_SCRIPT)
        text = self.log_text()
        self.assertLess(text.index("writing 2 recipe file(s)"), text.index("== apt-get update =="))

    def test_the_place_script_runs_in_the_source_root_and_never_as_root(self):
        text = G.PLACE_FILE_SCRIPT
        self.assertIn("cd /home/builder/src", text)
        self.assertIn('cat > "$1"', text)
        self.assertIn('chmod "$2" "$1"', text)

    def test_a_failing_file_write_fails_the_build_as_recipe(self):
        def configure(vm):
            vm.qga.place_exit = 1
        self.assertEqual(self.make(v2_doc(files=FILES), configure=configure).run(), "failed")
        self.assertTrue(self.result()["error"].startswith("cannot write the recipe file agino/launcher.sh"))
        self.assertEqual(self.result()["class"], "recipe")
        self.assertEqual(self.factory.vms[0].qga.step_commands(), [])

    def test_source_tar_is_still_unpacked_into_the_source_root(self):
        self.make(v2_doc()).run()
        qga = self.factory.vms[0].qga
        tar = next(a for kind, a in qga.execs if kind == "exec" and "/bin/tar" in a)
        self.assertEqual(tar[tar.index("-C") + 1], G.SRC_DIR)
        self.assertEqual(G.SRC_DIR, "/home/builder/src")

    def test_a_v1_recipe_writes_no_v2_scripts_and_places_nothing(self):
        self.make().run()
        qga = self.factory.vms[0].qga
        self.assertNotIn(G.PLACE_FILE, qga.files)
        self.assertNotIn(G.RUN_TEST, qga.files)
        self.assertEqual(qga.placed, [])

    def test_v2_scripts_are_installed_when_files_or_tests_exist(self):
        self.make(v2_doc(test=["true"])).run()
        qga = self.factory.vms[0].qga
        self.assertEqual(qga.files[G.RUN_TEST].decode(), G.RUN_TEST_SCRIPT)
        self.assertEqual(qga.modes[G.RUN_TEST], "755")


class EnvironmentTests(JobCase):
    def test_steps_get_the_agino_variables(self):
        self.make(v2_doc()).run()
        env = self.factory.vms[0].qga.step_env()
        self.assertEqual(env, G.USER_ENV + ["AGINO_VERSION=1.0", "AGINO_TAG=v1.0", "AGINO_COMMIT=" + COMMIT])

    def test_missing_values_are_empty(self):
        self.make(task_doc(version="2.0+agino3")).run()
        env = self.factory.vms[0].qga.step_env()
        self.assertEqual(env[-3:], ["AGINO_VERSION=2.0", "AGINO_TAG=", "AGINO_COMMIT="])

    def test_tests_get_the_same_variables(self):
        self.make(v2_doc(test=["true"])).run()
        argv = next(a for a in self.factory.vms[0].qga.started if G.RUN_TEST in a)
        env = [a for a in argv[argv.index(G.ENV_BIN) + 2:argv.index(G.RUN_TEST)] if "=" in a]
        self.assertEqual(env, G.USER_ENV + ["AGINO_VERSION=1.0", "AGINO_TAG=v1.0", "AGINO_COMMIT=" + COMMIT])

    def test_the_variables_are_not_expanded_by_the_host(self):
        doc = v2_doc(steps=["echo $AGINO_VERSION ${AGINO_TAG}"])
        self.make(doc).run()
        self.assertEqual(self.factory.vms[0].qga.step_commands(), ["echo $AGINO_VERSION ${AGINO_TAG}"])


class TestPhaseTests(JobCase):
    TESTS = ["app --version", "timeout 20 app >/dev/null 2>&1 || test $? -eq 124"]

    def test_tests_run_after_packaging_and_before_the_collection(self):
        self.assertEqual(self.make(v2_doc(test=self.TESTS)).run(), "done")
        self.assertEqual(self.states, ["starting", "starting", "installing_deps", "building", "building", "building",
                                       "building", "packaging", "testing", "collecting", "done"])
        qga = self.factory.vms[0].qga
        self.assertEqual(qga.test_commands(), self.TESTS)
        text = self.log_text()
        self.assertIn("test: app --version", text)
        self.assertLess(text.index("== packaging =="), text.index("== test 1/2: app --version =="))
        self.assertLess(text.index("== test 1/2"), text.index("== test 2/2"))
        res = self.result()
        self.assertTrue(res["ok"])
        self.assertEqual(res["class"], "ok")
        self.assertEqual(res["artifacts"][0]["sha256"], hashlib.sha256(DEB).hexdigest())

    def test_each_test_runs_under_xvfb_as_builder_with_a_120_second_limit(self):
        self.make(v2_doc(test=self.TESTS)).run()
        argv = next(a for a in self.factory.vms[0].qga.started if G.RUN_TEST in a)
        self.assertEqual(argv[:4], [G.RUNUSER, "-u", "builder", "--"])
        i = argv.index(G.RUN_TEST)
        self.assertEqual(argv[i + 2], "120")
        self.assertEqual(argv[i + 3], self.TESTS[0])
        self.assertIn("/usr/bin/xvfb-run -a /bin/sh -c", G.RUN_TEST_SCRIPT)
        self.assertIn("/usr/bin/timeout -k 10", G.RUN_TEST_SCRIPT)

    def test_the_test_text_is_one_argument(self):
        nasty = "echo $(id) `id`; rm -rf / # 'q' \"x\""
        self.make(v2_doc(test=[nasty])).run()
        argv = next(a for a in self.factory.vms[0].qga.started if G.RUN_TEST in a)
        self.assertEqual(argv[-1], nasty)
        self.assertEqual(argv.count(nasty), 1)

    def test_the_package_is_installed_as_root_before_the_first_test(self):
        self.make(v2_doc(test=["true"])).run()
        started = self.factory.vms[0].qga.started
        install = next(a for a in started if G.APT_GET in a and G.ARTIFACT in a)
        self.assertEqual(install[0], G.LOGGED)
        self.assertNotIn("builder", install)
        tail = install[install.index("install"):]
        self.assertEqual(tail, ["install", "-y", "--no-install-recommends", G.ARTIFACT])
        self.assertIn("Dir::Etc::sourcelist=" + G.APT_LIST, install)
        self.assertLess(started.index(install), started.index(next(a for a in started if G.RUN_TEST in a)))

    def test_apt_is_updated_first_when_no_build_dependency_did_it(self):
        doc = v2_doc(test=["true"], build_depends=[])
        self.make(doc).run()
        apt = [a for a in self.factory.vms[0].qga.started if G.APT_GET in a]
        self.assertEqual(["update" if "update" in a else "install" for a in apt], ["update", "install"])

    def test_apt_is_not_updated_twice(self):
        self.make(v2_doc(test=["true"])).run()
        apt = [a for a in self.factory.vms[0].qga.started if G.APT_GET in a]
        self.assertEqual(sum("update" in a for a in apt), 1)

    def test_a_failing_test_fails_the_build_as_recipe(self):
        def configure(vm):
            vm.qga.test_exit["app --help"] = 3
        job = self.make(v2_doc(test=["app --version", "app --help", "app --other"]), configure=configure)
        self.assertEqual(job.run(), "failed")
        res = self.result()
        self.assertEqual((res["ok"], res["error"], res["class"]), (False, "test 2 failed", "recipe"))
        self.assertEqual(self.factory.vms[0].qga.test_commands(), ["app --version", "app --help"])
        self.assertEqual(res["artifacts"], [])
        self.assertIn("test 2: exit code 3", self.log_text())
        self.assertFalse([f for f in os.listdir(os.path.dirname(self.out("x"))) if f.endswith(".deb")])
        self.assertEqual(self.states[-2:], ["testing", "failed"])
        self.assertNotIn("collecting", self.states)

    def test_a_test_that_exits_124_is_a_recipe_failure_noted_as_timed_out(self):
        def configure(vm):
            vm.qga.test_exit["true"] = 124
        self.make(v2_doc(test=["true"]), configure=configure).run()
        self.assertEqual((self.result()["error"], self.result()["class"]), ("test 1 failed", "recipe"))
        self.assertIn("timed out", self.log_text())

    def test_a_hanging_test_is_given_up_by_the_host(self):
        def configure(vm):
            vm.qga.hang_tests.add("sleep 9999")
        self.make(v2_doc(test=["sleep 9999"]), configure=configure).run()
        self.assertEqual((self.result()["error"], self.result()["class"]), ("test 1 failed", "recipe"))
        self.assertTrue(self.factory.vms[0].stopped)

    def test_installing_the_package_can_fail(self):
        def configure(vm):
            vm.qga.apt_exit["install-package"] = 100
        self.make(v2_doc(test=["true"]), configure=configure).run()
        self.assertEqual(self.result()["error"], "apt-get install-package failed (exit 100)")
        self.assertEqual(self.result()["class"], "recipe")
        self.assertEqual(self.factory.vms[0].qga.test_commands(), [])

    def test_a_failing_update_in_the_test_phase_is_infra(self):
        def configure(vm):
            vm.qga.apt_exit["update"] = 100
        self.make(v2_doc(test=["true"], build_depends=[]), configure=configure).run()
        self.assertEqual(self.result()["error"], "apt-get update failed (exit 100)")
        self.assertEqual(self.result()["class"], "infra")

    def test_no_tests_means_no_testing_state_and_no_install(self):
        self.make(v2_doc()).run()
        self.assertNotIn("testing", self.states)
        self.assertFalse([a for a in self.factory.vms[0].qga.started if G.ARTIFACT in a])

    def test_the_whole_job_fails_when_the_testing_allowance_is_used_up(self):
        def configure(vm):
            vm.qga.hang_tests.add("true")
        job = self.make(v2_doc(test=["true"]), configure=configure)

        def hook():
            if job.state == "testing":
                job.phase_end = 0.0  # the allowance is used up
        self.sleep_hook = hook
        job.run()
        self.assertEqual((self.result()["error"], self.result()["class"]), ("testing timed out", "timeout"))


class FailureClassTests(JobCase):
    def assertFailed(self, error_part, klass):
        res = self.result()
        self.assertFalse(res["ok"])
        self.assertIn(error_part, res["error"])
        self.assertEqual(res["class"], klass)

    def test_ok(self):
        self.make().run()
        self.assertEqual(self.result()["class"], "ok")
        self.assertEqual(sorted(self.result()), ["artifacts", "class", "error", "ok", "steps"])

    def test_a_failing_step_is_recipe(self):
        def configure(vm):
            vm.qga.step_exit["make -j4"] = 2
        self.make(configure=configure).run()
        self.assertFailed("step 2 failed", "recipe")

    def test_a_step_timeout_is_timeout(self):
        def configure(vm):
            vm.qga.step_exit["make -j4"] = 124
        self.make(configure=configure).run()
        self.assertFailed("step 2 timed out", "timeout")

    def test_the_time_limit_is_timeout(self):
        doc = task_doc()
        doc["limits"] = {"cpu_minutes": 1}

        def configure(vm):
            vm.qga.hang_steps.add("make -j4")
        self.make(doc, configure=configure).run()
        self.assertFailed("time limit of 1 minutes", "timeout")

    def test_packaging_failures_are_recipe(self):
        for code, text in ((20, "pkgroot"), (21, "DEBIAN"), (3, "packaging failed (exit 3)")):
            def configure(vm, code=code):
                vm.qga.package_exit = code
            self.make(configure=configure).run()
            self.assertFailed(text, "recipe")
            self.tearDown_dirs()

    def tearDown_dirs(self):
        """Forget the output of the previous run so that the next make() starts from nothing."""
        shutil.rmtree(os.path.join(self.cfg.builder.out, TASK_ID))

    def test_a_packaging_timeout_is_timeout(self):
        def configure(vm):
            vm.qga.package_exit = 124
        self.make(configure=configure).run()
        self.assertFailed("packaging timed out", "timeout")

    def test_apt_install_is_recipe_and_apt_update_is_infra(self):
        def install(vm):
            vm.qga.apt_exit["install"] = 100
        self.make(configure=install).run()
        self.assertFailed("apt-get install failed", "recipe")
        self.tearDown_dirs()

        def update(vm):
            vm.qga.apt_exit["update"] = 100
        self.make(configure=update).run()
        self.assertFailed("apt-get update failed", "infra")

    def test_a_bad_dependency_name_is_recipe(self):
        job = self.make()
        job.task = dataclasses.replace(self.task, build_depends=("evil; id",))
        job.run()
        self.assertFailed("invalid build dependency", "recipe")

    def test_artifact_problems_are_recipe(self):
        def big(vm):
            vm.qga.deb = DEB + b"y" * (2 << 20)
        self.make(configure=big, max_artifact_mib=1).run()
        self.assertFailed("over the limit", "recipe")
        self.tearDown_dirs()

        def not_deb(vm):
            vm.qga.deb = b"#!/bin/sh\n"
        self.make(configure=not_deb).run()
        self.assertFailed("not a deb", "recipe")

    def test_vm_problems_are_infra(self):
        def boot(vm):
            vm.boot_error = InfraError("qemu exited early")
        self.make(configure=boot).run()
        self.assertFailed("qemu exited early", "infra")
        self.tearDown_dirs()

        def agent(vm):
            vm.qga.fail_on["status"] = InfraError("guest agent guest-exec-status timed out")
        self.make(configure=agent).run()
        self.assertFailed("guest agent", "infra")
        self.tearDown_dirs()

        def crash(vm):
            vm.qga.fail_on["write_stream"] = RuntimeError("boom")
        self.make(configure=crash).run()
        self.assertFailed("internal error", "infra")

    def test_vm_exit_and_disk_limit_are_infra(self):
        def hang(vm):
            vm.qga.hang_steps.add("make -j4")
        self.make(configure=hang)
        self.sleep_hook = lambda: setattr(self.factory.vms[0], "exit_now", True)
        self.job.run()
        self.assertFailed("exited unexpectedly", "infra")
        self.tearDown_dirs()
        self.sleep_hook = None

        doc = task_doc()
        doc["limits"] = {"disk_gib": 1}

        def disk(vm):
            vm.disk = 2 * GIB
            vm.qga.hang_steps.add("make -j4")
        self.make(doc, configure=disk).run()
        self.assertFailed("disk limit of 1 GiB", "infra")

    def test_source_and_host_problems_are_infra(self):
        self.make(source=b"tampered").run()
        self.assertFailed("does not match", "infra")
        self.tearDown_dirs()

        def damaged(vm):
            vm.qga.corrupt_source = True
        self.make(configure=damaged).run()
        self.assertFailed("damaged", "infra")
        self.tearDown_dirs()
        self.make(free_bytes=lambda path: 1 << 20).run()
        self.assertFailed("free disk space", "infra")

    def test_a_cancel_is_infra(self):
        def hang(vm):
            vm.qga.hang_steps.add("make -j4")
        self.make(configure=hang)
        self.sleep_hook = lambda: self.job.request_stop("cancelled", "cancelled")
        self.assertEqual(self.job.run(), "cancelled")
        self.assertFailed("cancelled", "infra")

    def test_the_default_class_of_an_unclassified_failure_result_is_infra(self):
        out = OutDir(self.make().cfg.builder.out, TASK_ID, 1 << 20)
        out.create()
        out.write_result(False, "x", [], [])
        out.write_result(False, "x", [], [], "bogus")
        self.assertEqual(self.result()["class"], "infra")
        out.write_result(True, "", [], [], "recipe")
        self.assertEqual(self.result()["class"], "ok")
        out.close()


if __name__ == "__main__":
    unittest.main()
