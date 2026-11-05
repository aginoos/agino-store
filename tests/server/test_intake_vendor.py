# SPDX-License-Identifier: GPL-3.0-or-later
"""AI intake of a vendor application: the trial run in intake-step, the review record, adoption and the
candidate it makes (docs/server.md 15.8). The vendor is a local fake (fixtures.TestServer)."""

import os
import shutil
import unittest
from unittest import mock

import yaml
from agino_store_server import content, intake, intake_adopt, intake_delivery, intake_vendor, status_intake, trackers

from . import fixtures
from . import helpers as H
from . import intake_vendor_helpers as V
from .build_case import DPKG, GIT
from .intake_case import ADDRESS, APP, SHOTS, delivery, dump, git
from .test_intake_adopt import AdoptCase, slurp
from .test_intake_flow import FlowCase

DEB_NAME = "demo-app_2.1_amd64.deb"
DISTRIBUTION = V.distribution_supported()


@unittest.skipUnless(DPKG, "dpkg-deb is needed")
class VendorFlowCase(FlowCase):
    def setUp(self):
        super().setUp()
        self.server = fixtures.TestServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.base = self.server.base
        _, self.deb = V.serve(self.server, "2.1")

    def prepared_vendor(self):
        return self.prepared(ADDRESS, "--allow-host", "127.0.0.1")

    def files(self, deb=True, **kw):
        extra = {DEB_NAME: self.deb} if deb is True else ({DEB_NAME: deb} if deb else {})
        return V.files(self.base, **dict(kw, **extra))

    def deliver_vendor(self, intake_id, delivered=None, deps=None, expect=0):
        """The agent delivers (the runner is not involved: the AI task is simply marked done); intake-step runs."""
        delivered = self.files() if delivered is None else delivered
        ai_id = self.intake(intake_id)["ai_task"]
        for name, data in delivered.items():
            H.write(os.path.join(self.out, ai_id, "ai", name), data)
        self.set_status(ai_id, status="done", ai_files=sorted(delivered), error="", **{"class": "ok"})
        with (mock.patch("agino_store_server.checks.check_deps", return_value=deps) if deps else self.deps_ok()):
            return self.step(expect=expect)

    def in_vendor_review(self, delivered=None):
        intake_id = self.prepared_vendor()
        self.deliver_vendor(intake_id, delivered)
        self.assertEqual(self.istatus(intake_id), "review", self.intake(intake_id)["error"])
        return intake_id

    def feedback(self, intake_id):
        files = self.workspace(self.intake(intake_id)["ai_task"])
        return files["inputs/validation-error.txt"].decode("utf-8")


class TestTrialRun(VendorFlowCase):
    def test_a_good_delivery_goes_straight_to_review(self):
        intake_id = self.prepared_vendor()
        out = self.deliver_vendor(intake_id)
        rec = self.intake(intake_id)
        self.assertEqual((rec["status"], rec["app"], rec["version"], rec["verify_task"], rec["error"]),
                         ("review", "demo-app", "2.1", None, ""))
        check = rec["vendor_check"]
        self.assertEqual(check, {
            "version": "2.1", "sha256": self.sha(self.deb), "size": len(self.deb),
            "url": self.base + "/files/" + DEB_NAME, "filename": DEB_NAME, "flags": {"count": 0, "categories": []},
            "deps_ok": True, "matches_ai_deb": True, "hosts": [{"host": "127.0.0.1", "approved": True}]})
        self.assertTrue(os.path.isfile(os.path.join(self.cfg.downloads, check["sha256"], DEB_NAME)))
        self.assertEqual([b for b in self.st.builds() if b.get("verify")], [])
        self.assertEqual(self.st.candidates(), [])
        self.assertFalse(os.path.exists(os.path.join(self.cfg.state, "vendors")))
        self.assertIn("intake %s is ready for review (demo-app 2.1" % intake_id, out)

    def test_the_agents_files_are_kept_with_the_name_and_hash_of_its_deb(self):
        intake_id = self.in_vendor_review()
        saved = os.path.join(self.st.intake_dir(intake_id), "delivery")
        self.assertEqual(sorted(os.listdir(saved)), ["NOTES.md", "ai-deb.json", "app.yaml", "vendor.yaml"])
        self.assertEqual(intake_delivery.load_ai_deb(self.st, intake_id),
                         {"name": DEB_NAME, "sha256": self.sha(self.deb), "size": len(self.deb)})
        self.assertEqual(set(intake_delivery.load_snapshot(self.st, intake_id)), {"NOTES.md", "app.yaml", "vendor.yaml"})

    def test_a_different_deb_from_the_agent_is_a_warning_not_a_refusal(self):
        intake_id = self.in_vendor_review(self.files(deb=b"not the same file"))
        self.assertIs(self.intake(intake_id)["vendor_check"]["matches_ai_deb"], False)

    def test_a_delivery_without_a_deb_has_nothing_to_compare(self):
        intake_id = self.in_vendor_review(self.files(deb=False))
        self.assertIsNone(self.intake(intake_id)["vendor_check"]["matches_ai_deb"])
        self.assertIsNone(intake_delivery.load_ai_deb(self.st, intake_id))

    def test_flags_and_dependencies_are_recorded(self):
        V.serve(self.server, "2.1", scripts={"postinst": "#!/bin/sh\nsystemctl enable demo\n"},
                files={"lib/systemd/system/demo.service": b"[Unit]\n", "usr/bin/demo": b"x"},
                modes={"usr/bin/demo": 0o4755})
        intake_id = self.prepared_vendor()
        self.deliver_vendor(intake_id, self.files(deb=False), deps={"ok": False, "detail": "libx missing"})
        check = self.intake(intake_id)["vendor_check"]
        self.assertFalse(check["deps_ok"])
        self.assertEqual(check["flags"]["count"], 3)
        self.assertEqual(check["flags"]["categories"], ["postinst mentions systemctl enable", "setuid", "systemd unit"])

    def test_the_hosts_are_listed_with_their_approval(self):
        intake_id = self.in_vendor_review()
        rec = self.intake(intake_id)
        self.assertEqual(rec["vendor_check"]["hosts"], [{"host": "127.0.0.1", "approved": True}])

    def test_vendor_check_is_reported_through_status(self):
        intake_id = self.in_vendor_review()
        (task,) = status_intake.intake_section([self.intake(intake_id)])
        self.assertEqual(task["vendor_check"], self.intake(intake_id)["vendor_check"])
        self.assertNotIn("vendor_check", status_intake.intake_section([dict(self.intake(intake_id), vendor_check=None)])[0])


class TestTrialFailures(VendorFlowCase):
    def corrected(self, delivered=None):
        intake_id = self.prepared_vendor()
        self.deliver_vendor(intake_id, delivered)
        rec = self.intake(intake_id)
        self.assertEqual((rec["status"], rec["rounds"]), ("ready", 1), rec["error"])
        self.assertEqual(self.st.candidates(), [])
        return intake_id

    def test_a_tracking_file_that_finds_nothing_goes_back_to_the_agent(self):
        del self.server.routes["/latest.json"]
        intake_id = self.corrected()
        text = self.feedback(intake_id)
        self.assertIn("could not get the vendor's deb", text)
        self.assertIn("HTTP 404", text)
        files = self.workspace(self.intake(intake_id)["ai_task"])
        self.assertIn("inputs/previous/vendor.yaml", files)
        self.assertNotIn("inputs/previous/recipe.yaml", files)

    def test_a_deb_of_another_package_or_architecture(self):
        for kwargs, needle in (({"package": "other"}, "Package"), ({"arch": "arm64"}, "Architecture")):
            with self.subTest(kwargs=kwargs):
                V.serve(self.server, "2.1", **kwargs)
                intake_id = self.prepared(ADDRESS + "/" + needle, "--allow-host", "127.0.0.1")
                self.deliver_vendor(intake_id)
                self.assertEqual(self.istatus(intake_id), "ready")
                self.assertIn(needle, self.feedback(intake_id))

    def test_a_deb_whose_version_is_not_the_tracked_one(self):
        self.server.routes["/latest.json"] = fixtures.Route(body=('{"version": "9.9", "url": "/files/%s"}' % DEB_NAME).encode())
        intake_id = self.corrected()
        self.assertIn("differs from tracked version", self.feedback(intake_id))

    def test_a_download_link_to_a_host_nobody_approved(self):
        self.server.routes["/latest.json"] = fixtures.Route(body=b'{"version": "2.1", "url": "http://evil.example/x.deb"}')
        intake_id = self.corrected()
        self.assertIn("host not allowed: evil.example", self.feedback(intake_id))

    def test_nothing_is_recorded_for_a_failed_trial(self):
        del self.server.routes["/latest.json"]
        self.corrected()
        self.assertEqual(self.st.candidates(), [])
        self.assertFalse(os.path.exists(os.path.join(self.cfg.state, "vendors")))

    def test_a_rejected_delivery_does_not_get_as_far_as_the_network(self):
        vendor = V.vendor_yaml(self.base, allowed_hosts=["127.0.0.1", "evil.example"])
        before = len(self.server.hits)
        intake_id = self.corrected(self.files(vendor=vendor))
        self.assertIn("not a host you may use", self.feedback(intake_id))
        self.assertEqual(len(self.server.hits), before)

    def test_a_fixed_delivery_is_accepted_in_the_next_round(self):
        del self.server.routes["/latest.json"]
        intake_id = self.corrected()
        V.serve(self.server, "2.1")
        self.deliver_vendor(intake_id)
        rec = self.intake(intake_id)
        self.assertEqual((rec["status"], rec["rounds"], rec["version"]), ("review", 1, "2.1"))

    def test_three_rounds_then_the_task_fails(self):
        del self.server.routes["/latest.json"]
        intake_id = self.prepared_vendor()
        for _ in range(3):
            self.deliver_vendor(intake_id)
            self.assertEqual(self.istatus(intake_id), "ready")
        self.deliver_vendor(intake_id, expect=1)
        self.assertEqual(self.istatus(intake_id), "failed")

    def test_recipe_and_vendor_together_go_back_to_the_agent(self):
        intake_id = self.corrected(dict(self.files(), **{"recipe.yaml": delivery()["recipe.yaml"]}))
        self.assertIn("not both", self.feedback(intake_id))


class TestUnits(unittest.TestCase):
    def test_flag_categories_drop_file_names(self):
        category = intake_vendor.flag_category
        self.assertEqual(category("setuid usr/bin/x"), "setuid")
        self.assertEqual(category("systemd unit lib/systemd/system/x.service"), "systemd unit")
        self.assertEqual(category("writes /etc/apt: etc/apt/sources.list.d/x.list"), "writes /etc/apt")
        self.assertEqual(category("writes /etc/sudoers.d/x"), "writes system configuration")
        self.assertEqual(category("postinst mentions apt-key"), "postinst mentions apt-key")

    def test_status_shapes_whatever_the_record_holds(self):
        self.assertIsNone(status_intake.vendor_check(None))
        self.assertIsNone(status_intake.vendor_check("x"))
        shaped = status_intake.vendor_check({
            "version": "1", "size": -5, "flags": {"count": True, "categories": ["a"] * 50 + [5]}, "deps_ok": "yes",
            "hosts": [{"host": "h", "approved": 1}] * 40 + ["junk"], "extra": "dropped", "url": "u" * 5000})
        self.assertEqual((shaped["size"], shaped["flags"]["count"], shaped["deps_ok"]), (0, 0, None))
        self.assertEqual(len(shaped["flags"]["categories"]), 12)
        self.assertEqual(len(shaped["hosts"]), 20)
        self.assertFalse(shaped["hosts"][0]["approved"])
        self.assertEqual(len(shaped["url"]), 2048)
        self.assertNotIn("extra", shaped)

    def test_the_options_belong_to_imports(self):
        with self.assertRaises(intake.IntakeUsage):
            intake_adopt._check_options({"kind": "repair"}, "store", None, False)
        self.assertIsNone(intake_adopt._check_options({"kind": "repair"}, None, None, False))

    def test_distribution_values(self):
        value = intake_adopt.distribution_value
        self.assertIsNone(value(None, None, False))
        self.assertEqual(value("store", None, False), {"mode": "store"})
        self.assertEqual(value("vendor", "https://v.example/dl", False),
                         {"mode": "vendor", "page": "https://v.example/dl", "direct": False})
        self.assertEqual(value("vendor", "https://v.example/a.deb", True)["direct"], True)
        for bad in (("store", "https://v.example/", False), ("store", None, True), (None, "https://v.example/", False),
                    (None, None, True), ("vendor", None, False), ("vendor", "http://v.example/", False),
                    ("vendor", "https://u:p@v.example/", False), ("vendor", "https://v.example/" + "a" * 2100, False),
                    ("vendor", "https://v.example/ x", False), ("vendor", "https:///x", False),
                    ("vendor", "ftp://v.example/", False), ("other", None, False)):
            with self.subTest(bad=bad), self.assertRaises(intake.IntakeUsage):
                value(*bad)


@unittest.skipUnless(DPKG and GIT, "dpkg-deb and git are needed")
class VendorAdoptCase(VendorFlowCase):
    def adopt(self, intake_id, *extra, by="bob"):
        with self.deps_ok():
            return self.run_cli("intake-adopt", intake_id, "--by", by, *extra)

    def head(self):
        return git(self.cfg.content, "rev-parse", "HEAD")

    def path(self, *parts):
        return os.path.join(self.cfg.content, *parts)

    def changed(self):
        return sorted(git(self.cfg.content, "show", "--name-only", "--format=", "HEAD").split("\n"))

    def track(self):
        with self.deps_ok():
            return self.run_cli("track", "--vendor", "demo-app")


class TestAdoptVendor(VendorAdoptCase):
    def test_the_files_and_the_first_candidate(self):
        intake_id = self.in_vendor_review()
        before = self.head()
        code, out, err = self.adopt(intake_id, "--auto-task")
        self.assertEqual(code, 0, err)
        self.assertNotEqual(self.head(), before)
        self.assertEqual(git(self.cfg.content, "log", "-1", "--format=%s"), "intake(demo-app): add demo-app by bob")
        self.assertEqual(git(self.cfg.content, "status", "--porcelain"), "")
        self.assertEqual(self.changed(), ["apps/demo-app/app.yaml", "apps/demo-app/icon.png", "vendors/demo-app.yaml"])
        self.assertIn("--auto-task ignored", out)
        app = yaml.safe_load(slurp(self.path("apps", "demo-app", "app.yaml")))
        self.assertEqual(app["package"], {"kind": "deb", "name": "demo-app", "vendor": "demo-app"})
        self.assertEqual((app["description_source"], app["icon"]), ("ai", "icon.png"))
        self.assertNotIn("distribution", app)
        vendor = yaml.safe_load(slurp(self.path("vendors", "demo-app.yaml")))
        self.assertEqual((vendor["package"], vendor["fetch"], vendor["track"]["type"]), ("demo-app", "auto", "json"))
        loaded = content.load(self.cfg.content, allow_insecure=True, policy=self.cfg.build_policy())
        self.assertEqual(trackers.target_for(content.tracking_view(loaded), "demo-app").vendor_id, "demo-app")
        self.assertFalse(os.path.exists(self.path("apps", "demo-app", "build.yaml")))
        self.assertFalse(os.path.exists(self.path("upstreams", "demo-app.yaml")))
        # the candidate
        rec = self.intake(intake_id)
        (cand,) = self.st.candidates(app="demo-app")
        self.assertEqual(cand["origin"], {"type": "https", "url": self.base + "/files/" + DEB_NAME, "via": "intake",
                                          "intake": intake_id})
        self.assertEqual((cand["status"], cand["version"], cand["package"], cand["vendor"], cand["sha256"]),
                         ("pending", "2.1", "demo-app", "demo-app", self.sha(self.deb)))
        self.assertEqual(cand["vendor_url"], self.base + "/files/" + DEB_NAME)
        self.assertEqual(cand["checks"]["sandbox"], {"status": "missing"})
        self.assertTrue(cand["checks"]["deps"]["ok"])
        self.assertTrue(os.path.isdir(self.st.inspect_dir(cand)))
        self.assertEqual((rec["status"], rec["adopted_by"], rec["candidate"]),
                         ("adopted", "bob", os.path.basename(cand["_path"])[:-5]))
        self.assertIn("candidate %s of demo-app is pending" % rec["candidate"], out)

    def test_track_takes_over_for_later_versions(self):
        intake_id = self.in_vendor_review()
        self.assertEqual(self.adopt(intake_id)[0], 0)
        code, out, _ = self.track()
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(len(self.st.candidates(app="demo-app")), 1)
        V.serve(self.server, "2.2")
        code, out, _ = self.track()
        self.assertEqual(code, 0, out)
        newest = [c for c in self.st.candidates(app="demo-app") if c["version"] == "2.2"]
        self.assertEqual([c["origin"] for c in newest], [{"type": "https", "url": self.base + "/files/demo-app_2.2_amd64.deb"}])

    def test_the_scenes_are_committed_too(self):
        intake_id = self.in_vendor_review(self.files(**{"shots.yaml": dump(SHOTS)}))
        self.assertEqual(self.adopt(intake_id)[0], 0)
        self.assertIn("apps/demo-app/shots.yaml", self.changed())

    def test_a_candidate_that_fails_can_be_retried_and_keeps_the_strict_mark(self):
        intake_id = self.in_vendor_review()
        with mock.patch.object(trackers, "ingest", side_effect=trackers.TrackError("boom")):
            code, out, _ = self.adopt(intake_id)
        self.assertEqual(code, 1)
        self.assertIn("run intake-adopt again", out)
        rec = self.intake(intake_id)
        self.assertEqual((rec["status"], rec["candidate"]), ("adopted", None))
        self.assertIn("boom", rec["error"])
        committed = self.head()
        # track finds the same file meanwhile and makes the candidate without the mark
        self.assertEqual(self.track()[0], 0)
        (cand,) = self.st.candidates(app="demo-app")
        self.assertNotIn("via", cand["origin"])
        code, out, err = self.adopt(intake_id)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.head(), committed)
        self.assertIn("finishing the adoption", out)
        (cand,) = self.st.candidates(app="demo-app")
        self.assertEqual((cand["origin"]["via"], cand["origin"]["intake"]), ("intake", intake_id))
        self.assertEqual(self.intake(intake_id)["error"], "")

    def test_without_the_trial_deb_nothing_is_committed(self):
        intake_id = self.in_vendor_review()
        shutil.rmtree(os.path.join(self.cfg.downloads, self.intake(intake_id)["vendor_check"]["sha256"]))
        before = self.head()
        code, _, err = self.adopt(intake_id)
        self.assertEqual((code, self.head(), self.istatus(intake_id)), (1, before, "review"))
        self.assertIn("missing from the downloads", err)

    def test_an_id_taken_meanwhile_is_refused(self):
        intake_id = self.in_vendor_review()
        shutil.copytree(self.path("apps", "gimp"), self.path("apps", "demo-app"))
        before = self.head()
        code, _, err = self.adopt(intake_id)
        self.assertEqual(code, 1)
        self.assertIn("no longer validates", err)
        self.assertIn("already used", err)
        self.assertEqual(self.head(), before)

    def test_proprietary_software_needs_a_choice(self):
        app = V.app_yaml(proprietary=True, license="LicenseRef-proprietary")
        intake_id = self.in_vendor_review(self.files(app=app))
        before = self.head()
        code, _, err = self.adopt(intake_id)
        self.assertEqual((code, self.head(), self.istatus(intake_id)), (1, before, "review"))
        self.assertIn("proprietary", err)
        self.assertIn("--distribution", err)

    def test_bad_options_change_nothing(self):
        intake_id = self.in_vendor_review()
        before = self.head()
        for extra in (("--download-page", "https://v.example/"), ("--direct",),
                      ("--distribution", "store", "--download-page", "https://v.example/"),
                      ("--distribution", "store", "--direct"), ("--distribution", "vendor"),
                      ("--distribution", "vendor", "--download-page", "http://v.example/"),
                      ("--distribution", "vendor", "--download-page", "https://u:p@v.example/"),
                      ("--distribution", "vendor", "--download-page", "https://v.example/" + "a" * 2100)):
            with self.subTest(extra=extra):
                code, _, err = self.adopt(intake_id, *extra)
                self.assertEqual(code, 2, err)
        self.assertEqual((self.head(), self.istatus(intake_id)), (before, "review"))


@unittest.skipUnless(DISTRIBUTION, "content.load does not know `distribution` yet")
class TestAdoptDistribution(VendorAdoptCase):
    def app(self, intake_id):
        return yaml.safe_load(slurp(self.path("apps", "demo-app", "app.yaml")))

    def test_store_is_written_when_asked_for(self):
        intake_id = self.in_vendor_review(self.files(app=V.app_yaml(proprietary=True, license="LicenseRef-proprietary")))
        code, _, err = self.adopt(intake_id, "--distribution", "store")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.app(intake_id)["distribution"], {"mode": "store"})

    def test_vendor_with_a_page_and_a_note_for_an_unknown_host(self):
        intake_id = self.in_vendor_review(self.files(app=V.app_yaml(proprietary=True, license="LicenseRef-proprietary")))
        code, out, err = self.adopt(intake_id, "--distribution", "vendor", "--download-page", "https://obsidian.example/download",
                                    "--direct")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.app(intake_id)["distribution"],
                         {"mode": "vendor", "page": "https://obsidian.example/download", "direct": True})
        self.assertIn("note: the download page is on obsidian.example", out)

    def test_a_page_on_the_submitted_host_needs_no_note(self):
        intake_id = self.in_vendor_review()
        code, out, err = self.adopt(intake_id, "--distribution", "vendor", "--download-page", "https://vendor.example.com/dl")
        self.assertEqual(code, 0, err)
        self.assertNotIn("note:", out)
        self.assertFalse(self.app(intake_id)["distribution"]["direct"])


class TestRecipePathAndDistribution(AdoptCase):
    def test_a_proprietary_recipe_needs_a_choice_too(self):
        intake_id = self.in_review(files=delivery(app=dict(APP, proprietary=True, license="LicenseRef-proprietary")))
        before = self.head()
        code, _, err = self.adopt(intake_id)
        self.assertEqual((code, self.head(), self.istatus(intake_id)), (1, before, "review"))
        self.assertIn("proprietary", err)

    def test_what_the_agent_wrote_for_distribution_never_reaches_the_repository(self):
        app = dict(APP, distribution={"mode": "vendor", "page": "https://phish.example/", "direct": True})
        intake_id = self.in_review(files=delivery(app=app))
        self.assertEqual(self.adopt(intake_id)[0], 0)
        self.assertNotIn("distribution", slurp(self.path("apps", "newtool", "app.yaml")).decode("utf-8"))

    def test_a_recipe_app_cannot_be_sent_to_the_vendors_page(self):
        # The store builds the deb of a recipe itself; the vendor's page has another file. Refused before any commit.
        intake_id = self.in_review(files=delivery(app=dict(APP, proprietary=True, license="LicenseRef-proprietary")))
        before = self.head()
        code, _, err = self.adopt(intake_id, "--distribution", "vendor", "--download-page", "https://vendor.example.com/dl")
        self.assertEqual((code, self.head(), self.istatus(intake_id)), (1, before, "review"))
        self.assertIn("built from a recipe", err)

    def test_a_proprietary_recipe_app_is_adopted_with_an_explicit_store_choice(self):
        intake_id = self.in_review(files=delivery(app=dict(APP, proprietary=True, license="LicenseRef-proprietary")))
        code, _, err = self.adopt(intake_id, "--distribution", "store")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.yaml("apps", "newtool", "app.yaml")["distribution"], {"mode": "store"})   # the choice is on record


if __name__ == "__main__":
    unittest.main()
