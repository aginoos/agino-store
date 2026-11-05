# SPDX-License-Identifier: GPL-3.0-or-later
"""Validation of a delivery with vendor.yaml instead of a recipe (intake_delivery.validate_vendor)."""

import copy
import os

from agino_store_server import intake_delivery as D

from . import helpers as H
from . import intake_vendor_helpers as V
from .intake_case import APP, SHOTS, delivery, dump
from .test_intake_delivery import DeliveryCase

BASE = "http://127.0.0.1:1"


class VendorDeliveryCase(DeliveryCase):
    def vcheck(self, files=None, hosts=("127.0.0.1",), **record_fields):
        record = self.record(allow_hosts=list(hosts), **record_fields)
        return self.check(V.files(BASE) if files is None else files, record=record)

    def verrors(self, files, **kw):
        with self.assertRaises(D.DeliveryError) as ctx:
            self.vcheck(files, **kw)
        return ctx.exception.messages

    def assertVError(self, files, needle, **kw):
        messages = self.verrors(files, **kw)
        self.assertTrue(any(needle in m for m in messages), (needle, messages))


class TestShape(VendorDeliveryCase):
    def test_a_good_vendor_delivery(self):
        checked = self.vcheck()
        self.assertTrue(checked.is_vendor)
        self.assertEqual((checked.app_id, checked.version, checked.tag, checked.repo), ("demo-app", None, None, None))
        self.assertEqual(checked.notes, "License: see the page.\n")
        self.assertEqual(checked.tracking["track"]["type"], "json")
        self.assertEqual(checked.tracking["allowed_hosts"], ["127.0.0.1"])

    def test_recipe_and_vendor_are_exclusive(self):
        both = dict(V.files(BASE), **{"recipe.yaml": delivery()["recipe.yaml"]})
        self.assertVError(both, "not both")

    def test_neither_is_refused(self):
        self.assertError({"app.yaml": dump(V.app_yaml()), "NOTES.md": b"x\n"}, "recipe.yaml is missing")

    def test_the_recipe_path_is_unchanged(self):
        checked = self.check(delivery())
        self.assertFalse(checked.is_vendor)

    def test_a_repair_cannot_deliver_a_vendor_file(self):
        record = self.record(kind="repair")
        with self.assertRaises(D.DeliveryError) as ctx:
            D.validate(self.cfg, self.st, self.loaded, record, V.files(BASE))
        self.assertIn("repair delivers recipe.yaml", ctx.exception.messages[0])

    def test_app_yaml_is_required(self):
        files = V.files(BASE)
        del files["app.yaml"]
        self.assertVError(files, "app.yaml is missing")

    def test_the_deb_name_is_not_part_of_the_checked_files(self):
        checked = self.vcheck(dict(V.files(BASE), deb="demo-app_2.1_amd64.deb"))
        self.assertEqual(checked.app_id, "demo-app")


class TestServerOwnedFields(VendorDeliveryCase):
    def test_package_is_forced(self):
        app = V.app_yaml(package={"kind": "milkyway", "name": "gimp", "vendor": "chrome"})
        checked = self.vcheck(V.files(BASE, app=app))
        self.assertEqual(checked.app_data["package"], {"kind": "deb", "name": "demo-app", "vendor": "demo-app"})

    def test_the_other_server_fields_and_distribution(self):
        app = V.app_yaml(icon="evil.png", maintainer={"name": "M", "contact": "m@example.org"},
                         description_source="official", screenshots={"zh_CN": []},
                         distribution={"mode": "vendor", "page": "https://phish.example/", "direct": True})
        checked = self.vcheck(V.files(BASE, app=app))
        self.assertEqual(checked.app_data["icon"], "icon.png")
        self.assertEqual(checked.app_data["description_source"], "ai")
        self.assertNotIn("distribution", checked.app_data)
        self.assertNotIn("screenshots", checked.app_data)
        self.assertEqual(checked.app_data["maintainer"]["name"], "agino app store")

    def test_distribution_is_dropped_on_the_recipe_path_too(self):
        app = dict(APP, distribution={"mode": "vendor", "page": "https://phish.example/"})
        checked = self.check(delivery(app=app))
        self.assertNotIn("distribution", checked.app_data)

    def test_fetch_is_forced_to_auto(self):
        checked = self.vcheck(V.files(BASE, vendor=V.vendor_yaml(BASE, fetch="manual")))
        self.assertEqual(checked.vendor_data["fetch"], "auto")
        self.assertEqual(checked.tracking["fetch"], "auto")

    def test_source_is_official_or_personal(self):
        self.assertVError(V.files(BASE, app=V.app_yaml(source="system")), "source must be official or personal")
        self.assertEqual(self.vcheck(V.files(BASE, app=V.app_yaml(source="official"))).app_id, "demo-app")

    def test_install_profile_is_allowed(self):
        app = V.app_yaml(install_profile={"debconf": ["x x/add-repo boolean false"]})
        self.assertEqual(self.vcheck(V.files(BASE, app=app)).app_data["install_profile"],
                         {"debconf": ["x x/add-repo boolean false"]})


class TestStoreId(VendorDeliveryCase):
    def test_the_id_is_the_deb_package_name(self):
        checked = self.vcheck(V.files(BASE, vendor=V.vendor_yaml(BASE, package="demo-app-2")))
        self.assertEqual(checked.app_id, "demo-app-2")

    def test_a_bad_package_name(self):
        for package in ("Demo", "-x", "x y", "", 5, None, "a" * 65):
            with self.subTest(package=package):
                self.assertVError(V.files(BASE, vendor=V.vendor_yaml(BASE, package=package)), "`package` must be")

    def test_an_id_that_is_taken(self):
        for package in ("gimp", "chrome", "keys"):
            with self.subTest(package=package):
                self.assertVError(V.files(BASE, vendor=V.vendor_yaml(BASE, package=package)), "already used")

    def test_a_package_that_is_already_tracked_by_another_vendor(self):
        self.assertVError(V.files(BASE, vendor=V.vendor_yaml(BASE, package="google-chrome-stable")),
                          "already offered by the application chrome")

    def test_a_leftover_vendor_file_blocks_the_id(self):
        H.write(os.path.join(self.cfg.content, "vendors", "demo-app.yaml"), "junk: 1\n", "w")
        self.assertVError(V.files(BASE), "already used")


class TestTrackingRules(VendorDeliveryCase):
    def test_apt_is_refused(self):
        track = {"type": "apt", "url": "https://dl.example.com/deb/", "suite": "stable", "component": "main",
                 "keys": ["keys/x.asc"], "fingerprints": ["EB4C1BFD4F042F6DDDCCEC917721F63BD38B4796"]}
        self.assertVError(V.files(BASE, vendor=V.vendor_yaml(BASE, track=track)), "track.type apt is not allowed")

    def test_keys_are_refused(self):
        vendor = V.vendor_yaml(BASE)
        vendor["track"]["keys"] = ["keys/x.asc"]
        self.assertVError(V.files(BASE, vendor=vendor), "track.keys is not allowed")

    def test_unknown_types(self):
        for kind in ("tag", "release", "", None, 5):
            with self.subTest(kind=kind):
                vendor = V.vendor_yaml(BASE)
                vendor["track"]["type"] = kind
                self.assertTrue(self.verrors(V.files(BASE, vendor=vendor)))

    def test_the_four_types_are_accepted(self):
        tracks = {
            "json": {"type": "json", "url": BASE + "/a.json", "version_path": "v", "url_path": "u"},
            "page": {"type": "page", "url": BASE + "/d", "link_regex": r"href=\"(?P<url>[^\"]+_(?P<version>[0-9.]+)\.deb)\""},
            "redirect": {"type": "redirect", "url": BASE + "/latest", "version_regex": r"_(?P<version>[0-9.]+)\.deb"},
            "head": {"type": "head", "url": BASE + "/latest.deb"}}
        for kind, track in tracks.items():
            with self.subTest(kind=kind):
                self.assertEqual(self.vcheck(V.files(BASE, vendor=V.vendor_yaml(BASE, track=track))).tracking["track"]["type"], kind)

    def test_a_host_nobody_approved(self):
        vendor = V.vendor_yaml(BASE, allowed_hosts=["127.0.0.1", "evil.example"])
        self.assertVError(V.files(BASE, vendor=vendor), "allowed_hosts[1] 'evil.example' is not a host you may use")

    def test_the_hosts_that_are_approved(self):
        track = {"type": "json", "url": "https://vendor.example.com/latest.json", "version_path": "v", "url_path": "u"}
        # the submitted address (https://vendor.example.com/newtool), the task's extra hosts and server.conf's list
        vendor = V.vendor_yaml(BASE, allowed_hosts=["vendor.example.com", "cdn.example.net", "github.com"], track=track)
        checked = self.vcheck(V.files(BASE, vendor=vendor), hosts=("cdn.example.net",))
        self.assertEqual(checked.tracking["allowed_hosts"], ["vendor.example.com", "cdn.example.net", "github.com"])

    def test_the_task_hosts_are_what_counts_not_the_agents_wishes(self):
        self.assertVError(V.files(BASE), "is not a host you may use", hosts=())

    def test_the_tracked_url_must_be_on_an_allowed_host(self):
        track = {"type": "json", "url": "https://vendor.example.com/latest.json", "version_path": "v", "url_path": "u"}
        self.assertVError(V.files(BASE, vendor=V.vendor_yaml(BASE, track=track)), "host is not in allowed_hosts")

    def test_the_content_rules_still_apply(self):
        for track in ({"type": "page", "url": BASE + "/d", "link_regex": "(a+)+(?P<url>x)"},
                      {"type": "page", "url": BASE + "/d", "link_regex": "(?P<bad"},
                      {"type": "json", "url": BASE + "/a.json", "version_path": "v"}):
            with self.subTest(track=track):
                self.assertTrue(self.verrors(V.files(BASE, vendor=V.vendor_yaml(BASE, track=track))))

    def test_vendor_yaml_must_be_a_mapping_without_aliases(self):
        self.assertVError(dict(V.files(BASE), **{"vendor.yaml": b"- a\n"}), "vendor.yaml: must be a mapping")
        self.assertVError(dict(V.files(BASE), **{"vendor.yaml": b"a: &x 1\nb: *x\n"}), "anchors and aliases")


class TestScenes(VendorDeliveryCase):
    def test_scenes_are_kept(self):
        checked = self.vcheck(V.files(BASE, **{"shots.yaml": dump(SHOTS)}))
        self.assertEqual([s["id"] for s in checked.shots_data["scenes"]], ["main", "prefs"])

    def test_promo_is_refused(self):
        promo = dict(copy.deepcopy(SHOTS), promo=[{"image": "p.png", "lang": "zh_CN", "source": "x", "license": "y"}])
        self.assertVError(V.files(BASE, **{"shots.yaml": dump(promo)}), "`promo` is not allowed")

    def test_a_demo_tar_without_scenes(self):
        self.assertVError(V.files(BASE, **{"demo.tar": b"x"}), "only used by the scenes")
