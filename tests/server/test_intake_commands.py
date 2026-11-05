# SPDX-License-Identifier: GPL-3.0-or-later
"""intake-submit, intake-cancel, intake-repair, the [intake] settings and the proxy allow list."""

import os
import stat
import unittest

from agino_store_server import builds, config, content, intake, intake_proxy

from . import helpers as H
from .build_case import GIT
from .intake_case import ADDRESS, IntakeCase


class TestConfig(IntakeCase):
    def test_defaults(self):
        self.write_conf("")
        cfg = self.cfg
        self.assertEqual(cfg.intake_agents, ("claude",))
        self.assertFalse(cfg.intake_switch_agent)
        self.assertEqual((cfg.intake_ai_minutes, cfg.intake_max_rounds), (120, 3))
        self.assertEqual((cfg.intake_model_retry_minutes, cfg.intake_model_max_retries), (5, 12))
        self.assertTrue(cfg.intake_auto_repair)
        self.assertEqual((cfg.intake_max_auto_repairs, cfg.intake_max_unfinished), (2, 20))
        self.assertEqual(cfg.intake_proxy_allow_file, "/srv/store/builder-proxy/allow-extra.list")
        self.assertEqual((cfg.intake_max_page_kib, cfg.intake_max_inputs_mib, cfg.intake_keep_days), (1024, 8, 14))

    def test_values(self):
        self.write_conf("[intake]\nagents = codex, claude\nswitch_agent = yes\nai_minutes = 90\nmax_rounds = 2\n"
                        "model_retry_minutes = 10\nmodel_max_retries = 3\nauto_repair = no\nmax_auto_repairs = 1\n"
                        "max_unfinished = 5\nproxy_allow_file = /x/allow.list  # c\nmax_page_kib = 512\n"
                        "max_inputs_mib = 4\nkeep_days = 3\n")
        cfg = self.cfg
        self.assertEqual(cfg.intake_agents, ("codex", "claude"))
        self.assertTrue(cfg.intake_switch_agent)
        self.assertEqual((cfg.intake_ai_minutes, cfg.intake_max_rounds, cfg.intake_model_retry_minutes), (90, 2, 10))
        self.assertEqual((cfg.intake_model_max_retries, cfg.intake_auto_repair, cfg.intake_max_auto_repairs),
                         (3, False, 1))
        self.assertEqual((cfg.intake_max_unfinished, cfg.intake_proxy_allow_file), (5, "/x/allow.list"))
        self.assertEqual((cfg.intake_max_page_kib, cfg.intake_max_inputs_mib, cfg.intake_keep_days), (512, 4, 3))

    def test_bad_values(self):
        for text, needle in [("agents = gemini", "[intake] agents"), ("switch_agent = maybe", "switch_agent"),
                             ("ai_minutes = 5", "ai_minutes"), ("max_rounds = 11", "max_rounds"),
                             ("model_retry_minutes = 0", "model_retry_minutes"), ("max_unfinished = x", "max_unfinished")]:
            with self.subTest(text=text), self.assertRaises(config.ConfigError) as ctx:
                self.write_conf("[intake]\n%s\n" % text)
            self.assertIn(needle, str(ctx.exception))


class TestSubmit(IntakeCase):
    def test_a_new_task(self):
        intake_id = self.submit(ADDRESS, "--hint", "it is a tool", "--allow-host", "Dl.Vendor.Example",
                                "--allow-host", "dl.vendor.example")
        rec = self.intake(intake_id)
        for key, value in {"kind": "import", "status": "submitted", "address": ADDRESS, "hint": "it is a tool",
                           "agent": "claude", "by": "alice", "app": None, "attempt": 0, "ai_task": None,
                           "verify_task": None, "repair_of": None, "error": "", "started": None, "finished": None
                           }.items():
            self.assertEqual(rec[key], value, key)
        self.assertEqual(rec["allow_hosts"], ["dl.vendor.example", "vendor.example.com"])
        self.assertRegex(rec["id"], r"\A[0-9a-f]{16}\Z")
        self.assertTrue(rec["created"])

    def test_the_default_agent_is_the_first_one_and_codex_can_be_chosen(self):
        self.assertEqual(self.intake(self.submit())["agent"], "claude")
        self.assertEqual(self.intake(self.submit("https://vendor.example.com/other", "--agent", "codex"))["agent"], "codex")

    def test_an_allowed_upstream_host_is_not_added_again(self):
        rec = self.intake(self.submit("https://github.com/example/newtool"))
        self.assertEqual(rec["allow_hosts"], [])

    def test_the_same_address_is_refused_while_a_task_is_open(self):
        first = self.submit()
        for address in (ADDRESS, ADDRESS + "/", "https://VENDOR.example.com/newtool"):
            code, _, err = self.run_cli("intake-submit", address, "--by", "bob")
            self.assertEqual(code, 1, address)
            self.assertIn(first, err)
        self.assertEqual(self.run_cli("intake-cancel", first, "--by", "alice")[0], 0)
        self.assertEqual(self.run_cli("intake-submit", ADDRESS, "--by", "bob")[0], 0)

    def test_a_different_query_is_a_different_address(self):
        self.submit("https://vendor.example.com/dl?id=1")
        self.submit("https://vendor.example.com/dl?id=2")

    def test_bad_addresses(self):
        for address in ("vendor.example.com/newtool", "https://", "https://u:p@vendor.example.com/x",
                        "https://vendor.example.com/x#frag", "https://vendor.example.com/a b", "x" * 600,
                        "https://vendor.example.com/\x1b[31m", "https://vendor.example.com/<script>",
                        "javascript:alert(1)", "ftp://vendor.example.com/x", ""):
            with self.subTest(address=address):
                code, _, err = self.run_cli("intake-submit", address, "--by", "alice")
                self.assertEqual(code, 2, err)
        self.assertEqual(self.st.intakes(), [])

    def test_strict_address_rules_without_the_test_switch(self):
        for address in ("http://vendor.example.com/x", "https://127.0.0.1/x", "https://localhost/x",
                        "https://intranet/x", "https://vendor.example.com:8443/x", "https://[::1]/x",
                        "https://10.0.0.1/x"):
            with self.subTest(address=address), self.assertRaises(intake.IntakeUsage):
                intake.check_address(address, False)
        self.assertEqual(intake.check_address("https://Vendor.Example.com:443/x?a=1", False)[1], "vendor.example.com")

    def test_bad_hints_and_hosts(self):
        long_hint = "h" * 1001
        for extra in (["--hint", long_hint], ["--hint", "a\x00b"], ["--allow-host", "bad_host"],
                      ["--allow-host", "a/b.example.com"], ["--by", "x y"],
                      sum([["--allow-host", "h%d.example.com" % i] for i in range(6)], [])):
            with self.subTest(extra=extra[:2]):
                argv = ["intake-submit", ADDRESS] + ([] if "--by" in extra else ["--by", "alice"]) + extra
                self.assertEqual(self.run_cli(*argv)[0], 2)
        self.assertEqual(self.st.intakes(), [])

    def test_five_hosts_are_fine(self):
        extra = sum([["--allow-host", "h%d.example.com" % i] for i in range(5)], [])
        self.assertEqual(len(self.intake(self.submit(ADDRESS, *extra))["allow_hosts"]), 6)

    def test_an_agent_that_is_not_set_up(self):
        self.write_conf("")
        with self.assertRaises(intake.IntakeUsage):
            intake.check_agent(self.cfg, "codex")
        self.assertEqual(intake.check_agent(self.cfg, None), "claude")

    def test_too_many_open_tasks(self):
        self.write_conf("[intake]\nmax_unfinished = 2\nproxy_allow_file = {allow}\n")
        self.submit("https://vendor.example.com/a")
        self.submit("https://vendor.example.com/b")
        code, _, err = self.run_cli("intake-submit", "https://vendor.example.com/c", "--by", "alice")
        self.assertEqual(code, 1)
        self.assertIn("2 unfinished", err)

    def test_the_lock_is_respected(self):
        with self.st.lock():
            self.assertEqual(self.run_cli("intake-submit", ADDRESS, "--by", "alice")[0], 3)


class TestCancel(IntakeCase):
    def test_cancel_and_reject(self):
        first, second = self.submit(), self.submit("https://vendor.example.com/other")
        code, out, _ = self.run_cli("intake-cancel", first, "--by", "bob")
        self.assertEqual((code, out.strip()), (0, "cancelled " + first))
        rec = self.intake(first)
        self.assertEqual((rec["status"], rec["error"]), ("cancelled", "cancelled by bob"))
        self.assertTrue(rec["finished"])
        self.assertEqual(self.run_cli("intake-cancel", second, "--by", "bob", "--reject")[1].strip(), "rejected " + second)
        self.assertEqual(self.istatus(second), "rejected")

    def test_finished_tasks_cannot_be_cancelled(self):
        intake_id = self.submit()
        self.assertEqual(self.run_cli("intake-cancel", intake_id, "--by", "bob")[0], 0)
        code, _, err = self.run_cli("intake-cancel", intake_id, "--by", "bob")
        self.assertEqual(code, 1)
        self.assertIn("already cancelled", err)

    def test_unknown_and_bad_ids(self):
        self.assertEqual(self.run_cli("intake-cancel", "0123456789abcdef", "--by", "bob")[0], 2)
        self.assertEqual(self.run_cli("intake-cancel", "../etc", "--by", "bob")[0], 2)
        self.assertEqual(self.run_cli("intake-cancel", "0123456789abcdef", "--by", "b b")[0], 2)

    def test_the_build_tasks_are_cancelled_too(self):
        intake_id = self.submit()
        self.step()
        ai_id = self.intake(intake_id)["ai_task"]
        self.assertEqual(self.st.build(ai_id)["status"], "ready")
        self.assertEqual(self.run_cli("intake-cancel", intake_id, "--by", "bob")[0], 0)
        self.assertEqual(self.st.build(ai_id)["status"], "cancelled")
        self.assertTrue(os.path.exists(os.path.join(self.queue, ai_id, "cancel")))


@unittest.skipUnless(GIT, "git not available")
class TestRepairCommand(IntakeCase):
    def repair(self, *argv):
        return self.run_cli("intake-repair", "tool", *argv)

    def test_a_repair_task(self):
        self.seed_seen()
        code, out, err = self.repair("--version", "1.0.0", "--by", "alice")
        self.assertEqual(code, 0, err)
        rec = self.intake(out.strip())
        for key, value in {"kind": "repair", "app": "tool", "status": "submitted", "by": "alice",
                           "address": "file://" + self.repo, "allow_hosts": [], "agent": "claude"}.items():
            self.assertEqual(rec[key], value, key)
        self.assertEqual(rec["repair_of"], {"app": "tool", "version": "1.0.0", "failed_task": None})

    def test_a_repair_task_carries_the_requirements_of_the_recipe(self):
        self.seed_seen()
        path = os.path.join(self.cfg.content, "apps", "tool", "build.yaml")
        H.edit_yaml(path, lambda d: dict(d, requirements="Leave out the plugin."))
        rec = self.intake(self.repair("--version", "1.0.0", "--by", "alice")[1].strip())
        self.assertEqual(rec["hint"], "Leave out the plugin.")

    def test_the_failed_build_is_remembered(self):
        self.seed_seen()
        build_id = self.request()
        self.set_status(build_id, status="failed", **{"class": "recipe"})
        rec = self.intake(self.repair("--version", "1.0.0", "--by", "alice")[1].strip())
        self.assertEqual(rec["repair_of"]["failed_task"], build_id)

    def test_only_one_open_repair_per_version(self):
        self.seed_seen()
        self.assertEqual(self.repair("--version", "1.0.0", "--by", "alice")[0], 0)
        code, _, err = self.repair("--version", "1.0.0", "--by", "alice")
        self.assertEqual(code, 1)
        self.assertIn("already an unfinished repair", err)

    def test_refusals(self):
        self.seed_seen()
        self.assertEqual(self.repair("--version", "9.9.9", "--by", "alice")[0], 1)          # never seen
        self.assertEqual(self.run_cli("intake-repair", "gimp", "--version", "1.0", "--by", "alice")[0], 1)   # no recipe
        self.assertEqual(self.repair("--version", "../x", "--by", "alice")[0], 2)
        self.assertEqual(self.repair("--version", "1.0.0", "--by", "a b")[0], 2)

    def test_auto_repairs_are_counted_by_version(self):
        self.seed_seen()
        loaded = content.load(self.cfg.content, allow_insecure=True)
        with self.st.lock():
            first = intake.create_repair(self.cfg, self.st, loaded, "tool", "1.0.0", "auto")
        self.assertEqual(intake.auto_repairs(self.st, "tool", "1.0.0"), 1)
        self.assertEqual(intake.auto_repairs(self.st, "tool", "2.0.0"), 0)
        self.assertEqual(first["by"], "auto")


class TestProxyAllowFile(IntakeCase):
    def make(self, status, hosts):
        rec = intake.new_record(self.st, "import", ADDRESS, "", "claude", hosts, "alice", "2026-11-04T10:00:00Z")
        self.st.save_intake(dict(rec, status=status))
        return rec["id"]

    def update(self):
        rep = builds.Report()
        intake_proxy.update(self.cfg, self.st, rep)
        return rep

    def read(self):
        with open(self.allow_file, encoding="utf-8") as f:
            return f.read()

    def test_only_running_tasks_count_and_hosts_are_merged(self):
        self.make("ready", ["b.example.com", "a.example.com"])
        self.make("running", ["a.example.com", "c.example.com"])
        self.make("verifying", ["d.example.com"])
        for status in ("submitted", "fetching", "review", "adopted", "failed", "cancelled", "rejected", "paused"):
            self.make(status, ["never.example.com"])
        self.assertEqual(self.update().failed, 0)
        self.assertEqual(self.read(), "a.example.com\nb.example.com\nc.example.com\nd.example.com\n")

    def test_an_empty_file_when_nothing_runs(self):
        self.update()
        self.assertEqual(self.read(), "")

    def test_the_step_writes_the_file_and_the_hosts_leave_when_the_task_ends(self):
        intake_id = self.submit(ADDRESS, "--allow-host", "dl.vendor.example")
        self.step()
        self.assertEqual(self.istatus(intake_id), "ready")
        self.assertEqual(self.read(), "dl.vendor.example\nvendor.example.com\n")
        self.assertEqual(self.run_cli("intake-cancel", intake_id, "--by", "bob")[0], 0)
        self.step()
        self.assertEqual(self.read(), "")

    def test_invalid_names_are_dropped_with_a_note(self):
        self.make("ready", ["ok.example.com", "Bad.Example.com", "a b", "-x.com", "x" * 300, 5])
        hosts, dropped = intake_proxy.wanted_hosts(self.st)
        self.assertEqual(hosts, ["ok.example.com"])
        self.assertEqual(len(dropped), 5)
        rep = self.update()
        self.assertTrue(any("dropped" in line for line in rep.lines))
        self.assertEqual(self.read(), "ok.example.com\n")

    def test_the_file_is_replaced_atomically_and_only_when_it_changes(self):
        intake_proxy.write_atomic(self.allow_file, "a.example.com\n")
        before = os.stat(self.allow_file)
        self.assertFalse(intake_proxy.write_atomic(self.allow_file, "a.example.com\n"))
        self.assertEqual(os.stat(self.allow_file).st_ino, before.st_ino)
        self.assertTrue(intake_proxy.write_atomic(self.allow_file, "b.example.com\n"))
        self.assertEqual(self.read(), "b.example.com\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.allow_file).st_mode), 0o644)
        self.assertEqual(os.listdir(os.path.dirname(self.allow_file)), ["allow-extra.list"])

    def test_a_missing_directory_is_reported_as_a_failure(self):
        os.rmdir(os.path.dirname(self.allow_file))
        code, out, _ = self.run_cli("intake-step")
        self.assertEqual(code, 1)
        self.assertIn("cannot write", out)

    def test_a_write_error_leaves_no_temporary_file(self):
        directory = os.path.dirname(self.allow_file)
        self.make("ready", ["a.example.com"])
        os.chmod(directory, 0o500)
        try:
            if os.access(directory, os.W_OK):
                self.skipTest("running as a user that ignores directory permissions")
            self.assertEqual(self.update().failed, 1)
        finally:
            os.chmod(directory, 0o700)
        self.assertEqual(os.listdir(directory), [])


class TestUtilities(unittest.TestCase):
    def test_address_key(self):
        self.assertEqual(intake.address_key("https://A.example.com/x/"), intake.address_key("https://a.example.com/x"))
        self.assertNotEqual(intake.address_key("https://a.example.com/x?1"), intake.address_key("https://a.example.com/x?2"))

    def test_check_hosts(self):
        self.assertEqual(intake.check_hosts(["A.example.com", "a.example.com", "b.example.org"]),
                         ("a.example.com", "b.example.org"))
        self.assertEqual(intake.check_hosts(None), ())
        for bad in (["localhost"], ["1.2.3.4"], ["a_b.example.com"], ["single"]):
            with self.subTest(bad=bad), self.assertRaises(intake.IntakeUsage):
                intake.check_hosts(bad)

    def test_check_by_is_shared_with_builds(self):
        with self.assertRaises(builds.BuildUsage):
            builds.check_by("a b")

    def test_check_hint(self):
        self.assertEqual(intake.check_hint(None), "")
        self.assertEqual(intake.check_hint("  a\nb  "), "a\nb")
        with self.assertRaises(intake.IntakeUsage):
            intake.check_hint("a\x07b")
