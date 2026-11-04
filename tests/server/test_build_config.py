# SPDX-License-Identifier: GPL-3.0-or-later
import os
import tempfile
import unittest

from agino_store_server import buildcfg, config

from . import helpers as H


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = self._tmp.name
        self.key, _ = H.make_key(self.base)
        self.conf = os.path.join(self.base, "server.conf")

    def load(self, extra=""):
        H.write_conf_file(self.conf, H.make_config(self.base, self.key))
        with open(self.conf, "a", encoding="utf-8") as f:
            f.write(extra)
        return config.load(self.conf)

    def assertBad(self, extra, needle):
        with self.assertRaises(config.ConfigError) as ctx:
            self.load(extra)
        self.assertIn(needle, str(ctx.exception))


class TestDefaults(ConfigCase):
    def test_sections_are_optional(self):
        cfg = self.load()
        self.assertEqual(cfg.upstream_allowed_hosts, buildcfg.DEFAULT_UPSTREAM_HOSTS)
        self.assertEqual(cfg.upstream_check_interval_hours, 6)
        self.assertEqual((cfg.builds_queue, cfg.builds_out), ("/srv/store/build-queue", "/srv/store/build-out"))
        self.assertEqual(cfg.builds_max_concurrent, 1)
        self.assertEqual(cfg.builds_limits, buildcfg.Limits(90, 8, 20))
        self.assertEqual((cfg.builds_max_source_mib, cfg.builds_max_artifact_mib, cfg.builds_max_log_mib),
                         (512, 1024, 8))

    def test_build_policy(self):
        policy = self.load().build_policy()
        self.assertEqual(policy.allowed_hosts, buildcfg.DEFAULT_UPSTREAM_HOSTS)
        self.assertEqual(policy.limits, buildcfg.DEFAULT_LIMITS)


class TestParsing(ConfigCase):
    def test_all_values(self):
        cfg = self.load("[upstream]\nallowed_hosts = Git.Example.org  codeberg.org\ncheck_interval_hours = 12\n"
                        "[builds]\nqueue = /q\nout = /o\nmax_concurrent = 2\n"
                        "limits = cpu_minutes=30 memory_gib=4  # lower\n"
                        "max_source_mib = 100\nmax_artifact_mib = 200\nmax_log_mib = 2\n")
        self.assertEqual(cfg.upstream_allowed_hosts, ("git.example.org", "codeberg.org"))
        self.assertEqual(cfg.upstream_check_interval_hours, 12)
        self.assertEqual((cfg.builds_queue, cfg.builds_out, cfg.builds_max_concurrent), ("/q", "/o", 2))
        self.assertEqual(cfg.builds_limits, buildcfg.Limits(30, 4, 20))
        self.assertEqual((cfg.builds_max_source_mib, cfg.builds_max_artifact_mib, cfg.builds_max_log_mib),
                         (100, 200, 2))

    def test_bad_hosts(self):
        for hosts in ("", "localhost", "intranet", "127.0.0.1", "bad_host.org", "a/b.org", "-x.org"):
            with self.subTest(hosts=hosts):
                self.assertBad("[upstream]\nallowed_hosts = %s\n" % hosts, "[upstream] allowed_hosts")

    def test_bad_limits(self):
        for limits in ("cpu_minutes=0", "cpu_minutes=abc", "gpu=1", "cpu_minutes", "memory_gib=-3", "disk_gib=1.5"):
            with self.subTest(limits=limits):
                self.assertBad("[builds]\nlimits = %s\n" % limits, "[builds] limits")

    def test_bad_numbers(self):
        for section, key, value in [("builds", "max_concurrent", "0"), ("builds", "max_concurrent", "99"),
                                    ("builds", "max_source_mib", "x"), ("builds", "max_log_mib", "0"),
                                    ("upstream", "check_interval_hours", "0")]:
            with self.subTest(key=key, value=value):
                self.assertBad("[%s]\n%s = %s\n" % (section, key, value), "[%s] %s" % (section, key))


class TestHelpers(unittest.TestCase):
    def test_parse_limits_defaults(self):
        self.assertEqual(buildcfg.parse_limits(None), buildcfg.DEFAULT_LIMITS)
        self.assertEqual(buildcfg.parse_limits("disk_gib=5,cpu_minutes=10"), buildcfg.Limits(10, 8, 5))

    def test_insecure_hosts_may_be_local(self):
        self.assertEqual(buildcfg.parse_hosts("127.0.0.1 localhost", allow_insecure=True), ("127.0.0.1", "localhost"))


if __name__ == "__main__":
    unittest.main()
