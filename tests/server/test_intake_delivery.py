# SPDX-License-Identifier: GPL-3.0-or-later
"""Validation of what an AI task delivered (intake_delivery)."""

import copy
import os
import unittest

import yaml
from agino_store_server import content, intake, intake_delivery as D

from . import helpers as H
from .build_case import DPKG
from .intake_case import APP, RECIPE, SHOTS, TAGS, UPSTREAM, IntakeCase, delivery, dump, tags_patch

PINNED = {"version": "1.0.0", "tag": "v1.0.0", "commit": "a" * 40}


class DeliveryCase(IntakeCase):
    def setUp(self):
        super().setUp()
        self.loaded = content.load(self.cfg.content, allow_insecure=True, policy=self.cfg.build_policy())

    def record(self, kind="import", **fields):
        base = intake.new_record(self.st, kind, "https://vendor.example.com/newtool", "", "claude",
                                 fields.pop("allow_hosts", []), "alice", "2026-11-04T10:00:00Z",
                                 {"app": "tool", "version": "1.0.0", "failed_task": None} if kind == "repair" else None)
        return dict(base, **fields)

    def check(self, files=None, kind="import", pinned=None, record=None, **kw):
        record = record or self.record(kind)
        with tags_patch():
            return D.validate(self.cfg, self.st, self.loaded, record, delivery() if files is None else files,
                              pinned=pinned, **kw)

    def errors(self, files, **kw):
        with self.assertRaises(D.DeliveryError) as ctx:
            self.check(files, **kw)
        return ctx.exception.messages

    def assertError(self, files, needle, **kw):
        messages = self.errors(files, **kw)
        self.assertTrue(any(needle in m for m in messages), (needle, messages))


class TestAcceptedImport(DeliveryCase):
    def test_a_good_delivery(self):
        checked = self.check()
        self.assertEqual((checked.app_id, checked.version, checked.tag, checked.commit),
                         ("newtool", "1.2.0", "v1.2.0", TAGS["v1.2.0"]))
        self.assertEqual(checked.repo, UPSTREAM["repo"])
        self.assertTrue(checked.recipe.is_v2)
        self.assertEqual(checked.notes, "notes\n")

    def test_the_server_owns_some_fields(self):
        app = dict(copy.deepcopy(APP), icon="evil.png", screenshots={"zh_CN": [{"image": "x.png"}]},
                   maintainer={"name": "Mallory", "contact": "m@example.org"}, description_source="official",
                   description_confirmed={"by": "bob", "at": "2026-11-04T10:00:00Z"})
        recipe = dict(copy.deepcopy(RECIPE), recipe_version=99, derived_from=98)
        upstream = dict(copy.deepcopy(UPSTREAM), app="newtool", build="apps/other/build.yaml", auto_task=True)
        checked = self.check(delivery(recipe=recipe, app=app, upstream=upstream))
        self.assertEqual(checked.app_data["icon"], "icon.png")
        self.assertNotIn("screenshots", checked.app_data)
        self.assertEqual(checked.app_data["maintainer"], {"name": "agino app store", "contact": "store@aginoos.com"})
        self.assertEqual(checked.app_data["description_source"], "ai")
        self.assertNotIn("description_confirmed", checked.app_data)
        self.assertEqual(checked.recipe_data["recipe_version"], 1)
        self.assertNotIn("derived_from", checked.recipe_data)
        self.assertEqual(checked.upstream_data["build"], "apps/newtool/build.yaml")
        self.assertFalse(checked.upstream_data["auto_task"])

    def test_the_pinned_version_needs_no_tag_listing(self):
        checked = D.validate(self.cfg, self.st, self.loaded, self.record(), delivery(), pinned=PINNED)
        self.assertEqual((checked.version, checked.tag, checked.commit), ("1.0.0", "v1.0.0", "a" * 40))

    def test_the_store_id_is_the_package_name_without_a_tracking_file(self):
        checked = self.check(delivery(upstream=None), pinned=PINNED)
        self.assertEqual(checked.app_id, "newtool-agino")
        self.assertIsNone(checked.repo)

    def test_without_tracking_or_deb_the_version_is_unknown(self):
        self.assertError(delivery(upstream=None), "version cannot be determined")

    @unittest.skipUnless(DPKG, "dpkg-deb not available")
    def test_the_version_can_come_from_the_agents_deb(self):
        ai_id = "0123456789abcdef"
        directory = os.path.join(self.out, ai_id, "ai")
        os.makedirs(directory)
        deb = self.make_deb("2.5.1+agino1", package="newtool-agino")
        H.write(os.path.join(directory, "newtool.deb"), deb[1])
        files = dict(delivery(upstream=None), deb="newtool.deb")
        checked = self.check(files, ai_record={"id": ai_id})
        self.assertEqual((checked.version, checked.tag, checked.commit), ("2.5.1", None, None))

    def test_an_extra_host_is_allowed_while_validating(self):
        recipe = copy.deepcopy(RECIPE)
        recipe["sources"].append({"name": "data", "kind": "tarball", "sha256": "a" * 64,
                                  "url": "https://downloads.vendor.example/pub/newtool-${VERSION}.tar.gz"})
        self.assertError(delivery(recipe=recipe), "allowed_hosts")
        checked = self.check(delivery(recipe=recipe), record=self.record(allow_hosts=["downloads.vendor.example"]))
        self.assertEqual(len(checked.recipe.sources), 2)

    def test_a_tracking_file_whose_tags_do_not_match(self):
        with tags_patch() as patched:
            patched.return_value = {"nightly": "a" * 40}
            with self.assertRaises(D.DeliveryError) as ctx:
                D.validate(self.cfg, self.st, self.loaded, self.record(), delivery())
        self.assertIn("no tag of the repository matches", ctx.exception.messages[0])


class TestShotsOfAnImport(DeliveryCase):
    def test_no_shots_file_is_fine(self):
        self.assertIsNone(self.check().shots_data)

    def test_good_scenes_are_kept(self):
        checked = self.check(delivery(shots=SHOTS))
        self.assertEqual([s["id"] for s in checked.shots_data["scenes"]], ["main", "prefs"])

    def test_demo_trees_and_promo_images_are_not_for_the_agent(self):
        scene = dict(SHOTS["scenes"][0], demo=["sample"])
        self.assertError(delivery(shots=dict(SHOTS, scenes=[scene])), "scenes[0].demo is not allowed")
        promo = dict(SHOTS, promo=[{"image": "promo/zh_CN/1.png", "lang": "zh_CN", "source": "x", "license": "y"}])
        self.assertError(delivery(shots=promo), "`promo` is not allowed")
        scene = dict(SHOTS["scenes"][0], args=["~/Demo/x"])
        self.assertError(delivery(shots=dict(SHOTS, scenes=[scene])), "must not point into ~/Demo")

    def test_a_scene_the_schema_refuses_goes_back_to_the_agent(self):
        bad = dict(SHOTS, scenes=[dict(SHOTS["scenes"][0], id="Not Valid")])
        self.assertError(delivery(shots=bad), "shots.yaml")
        unknown = dict(SHOTS, scenes=[dict(SHOTS["scenes"][0], run="rm -rf /")])
        self.assertError(delivery(shots=unknown), "shots.yaml")
        steps = dict(SHOTS, scenes=[dict(SHOTS["scenes"][0], steps=[{"shell": "id"}])])
        self.assertError(delivery(shots=steps), "shots.yaml")

    def test_a_shots_file_that_is_not_yaml_mapping(self):
        files = dict(delivery(), **{"shots.yaml": b"- just\n- a list\n"})
        self.assertError(files, "shots.yaml: must be a mapping")

    def test_a_repair_ignores_shots(self):
        path = os.path.join(self.cfg.content, "apps", "tool", "build.yaml")
        self.assertTrue(os.path.exists(path))
        files = {"recipe.yaml": dump(dict(RECIPE, package=dict(RECIPE["package"], name="tool-agino"))), "NOTES.md": b"x\n",
                 "shots.yaml": dump(dict(SHOTS, promo=[{"x": 1}]))}
        self.seed_seen()
        checked = D.validate(self.cfg, self.st, self.loaded, self.record("repair"), files)
        self.assertIsNone(checked.shots_data)


class TestRefusedImport(DeliveryCase):
    def test_missing_files(self):
        self.assertError({}, "recipe.yaml is missing")
        self.assertError({"recipe.yaml": dump(RECIPE)}, "app.yaml is missing")

    def test_a_v1_recipe_is_not_accepted(self):
        recipe = {k: v for k, v in RECIPE.items() if k != "sources"}
        recipe["source"] = {"kind": "git", "checksum": "required"}
        self.assertError(delivery(recipe=recipe), "must use `sources`")

    def test_a_recipe_without_tests(self):
        self.assertError(delivery(recipe=dict(RECIPE, test=[])), "`test` needs at least one command")
        self.assertError(delivery(recipe={k: v for k, v in RECIPE.items() if k != "test"}), "`test` needs")

    def test_setuid_in_a_step(self):
        for step in ("chmod u+s pkgroot/usr/bin/x", "chmod +s x", "chmod 4755 x", "install -d x && chmod g+s x"):
            with self.subTest(step=step):
                self.assertError(delivery(recipe=dict(RECIPE, steps=[step])), "setuid")
        for step in ("chmod 755 x", "chmod a+rx dir", "chmod 0644 f", "chmod -R go+rX pkgroot"):
            with self.subTest(step=step):
                self.check(delivery(recipe=dict(RECIPE, steps=[step])))

    def test_all_problems_come_back_together(self):
        messages = self.errors(delivery(recipe=dict(RECIPE, test=[], steps=["chmod u+s x"], build_depends=["Bad_Name"])))
        self.assertGreaterEqual(len(messages), 3)

    def test_bad_yaml(self):
        for data, needle in [(b"a: [", "invalid YAML"), (b"\xff\xfe", "not valid UTF-8"), (b"- a\n- b\n", "must be a mapping"),
                             (b"x: &a [1]\ny: *a\n", "anchors and aliases"), (b"x: &a 1\n", "anchors and aliases"),
                             (b"", "must be a mapping")]:
            with self.subTest(data=data):
                self.assertError(dict(delivery(), **{"recipe.yaml": data}), needle)

    def test_a_yaml_bomb_is_not_expanded(self):
        bomb = "a: &a [x]\n" + "".join("b%d: &b%d [*%s, *%s]\n" % (i, i, "a" if i == 0 else "b%d" % (i - 1),
                                                                     "a" if i == 0 else "b%d" % (i - 1))
                                       for i in range(30))
        self.assertError(dict(delivery(), **{"recipe.yaml": bomb.encode()}), "anchors and aliases")

    def test_a_huge_document_is_refused(self):
        many = "x:\n" + "".join("  - %d\n" % i for i in range(D.MAX_YAML_EVENTS))
        self.assertError(dict(delivery(), **{"recipe.yaml": many.encode()}), "too complex")

    def test_the_store_id(self):
        self.assertError(delivery(upstream=dict(UPSTREAM, app="Bad_Id")), "store id")
        self.assertError(delivery(upstream=dict(UPSTREAM, app="x" * 70)), "store id")
        self.assertError(delivery(upstream=dict(UPSTREAM, app="gimp")), "already used")
        self.assertError(delivery(upstream=dict(UPSTREAM, app="tool")), "already used")
        self.assertError(delivery(app=dict(APP, package={"kind": "deb", "name": "gimp"}), upstream=None),
                         "already used")

    def test_the_app_entry_is_checked_by_the_content_rules(self):
        for change, needle in [({"categories": ["nope"]}, "unknown category"), ({"source": "official"}, "personal"),
                               ({"license": "<b>x</b>"}, "bad format"), ({"name": {"en_US": "x"}}, "zh_CN"),
                               ({"description": {"zh_CN": ["<script>"], "en_US": ["x"]}}, "markup"),
                               ({"package": {"kind": "deb", "name": "other-agino"}}, "recipe package"),
                               ({"extra": 1}, "unknown key")]:
            with self.subTest(change=change):
                self.assertError(delivery(app=dict(APP, **change)), needle)

    def test_the_tracking_file_is_checked_by_the_content_rules(self):
        for change, needle in [({"repo": "https://evil.example/a/b.git"}, "allowed_hosts"),
                               ({"track": {"type": "tag", "match": "^v(.*)+$"}}, "nested quantifiers"),
                               ({"track": {"type": "tag", "match": "no groups"}}, "capture group"),
                               ({"extra": 1}, "unknown key")]:
            with self.subTest(change=change):
                self.assertError(delivery(upstream=dict(UPSTREAM, **change)), needle)

    def test_the_recipe_is_checked_by_the_content_rules(self):
        for change, needle in [({"steps": []}, "steps"), ({"sources": [{"name": "src", "kind": "tarball",
                                                                       "url": "https://github.com/a/b/c.tgz"}]}, "sha256"),
                               ({"package": dict(RECIPE["package"], name="x")}, "package.name"),
                               ({"limits": {"cpu_minutes": 100000}}, "global limit"),
                               ({"files": [{"path": "src/x", "content": "x"}]}, "inside a source directory"),
                               ({"steps": ["make ${VERSION}"]}, "only allowed in sources"),
                               ({"unknown": 1}, "unknown key")]:
            with self.subTest(change=change):
                self.assertError(delivery(recipe=dict(RECIPE, **change)), needle)

    def test_a_placeholder_without_a_value_is_a_delivery_problem(self):
        recipe = copy.deepcopy(RECIPE)
        recipe["sources"][0]["ref"] = "${TAG}"
        checked = self.check(delivery(recipe=recipe, upstream=None), pinned=dict(PINNED, tag=None, commit=None))
        with self.assertRaises(Exception) as ctx:
            checked.recipe.as_task(checked.version, checked.tag, checked.commit)
        self.assertIn("TAG", str(ctx.exception))

    def test_ids_do_not_leak_into_paths(self):
        # the store id becomes a directory name: nothing but the id pattern gets through
        self.assertError(delivery(upstream=dict(UPSTREAM, app="../evil")), "store id")
        self.assertError(delivery(upstream=dict(UPSTREAM, app="a/b")), "store id")


class TestImportRequirements(DeliveryCase):
    def test_the_hint_of_the_form_becomes_the_requirements_of_the_recipe(self):
        recipe = D.normalise_recipe(dict(RECIPE, requirements="agent text"), None, {"hint": "  Skip the docs.  "})
        self.assertEqual(recipe["requirements"], "Skip the docs.")
        self.assertEqual(recipe["recipe_version"], 1)

    def test_no_hint_means_no_requirements_even_when_the_agent_wrote_some(self):
        self.assertNotIn("requirements", D.normalise_recipe(dict(RECIPE, requirements="agent text"), None, {"hint": ""}))
        self.assertNotIn("requirements", D.normalise_recipe(dict(RECIPE), None, {}))


class TestRepairDelivery(DeliveryCase):
    def setUp(self):
        super().setUp()
        self.seed_seen()

    def repair_recipe(self, **changes):
        return dict(dict(RECIPE, package=dict(RECIPE["package"], name="tool-agino")), **changes)

    def validate(self, recipe=None, **kw):
        files = {"recipe.yaml": dump(recipe or self.repair_recipe()), "NOTES.md": b"fixed\n"}
        return D.validate(self.cfg, self.st, self.loaded, self.record("repair"), files, **kw)

    def test_a_good_repair(self):
        checked = self.validate()
        self.assertEqual((checked.app_id, checked.version, checked.tag, checked.commit),
                         ("tool", "1.0.0", "v1.0.0", self.commit))
        self.assertEqual((checked.recipe_data["recipe_version"], checked.recipe_data["derived_from"]), (2, 1))
        self.assertIsNone(checked.app_data)
        self.assertIsNone(checked.upstream_data)
        self.assertEqual(checked.repo, "file://" + self.repo)

    def test_a_repair_keeps_the_requirements_of_the_recipe_whatever_the_agent_wrote(self):
        path = os.path.join(self.cfg.content, "apps", "tool", "build.yaml")
        H.edit_yaml(path, lambda d: dict(d, requirements="Leave out the plugin."))
        self.loaded = content.load(self.cfg.content, allow_insecure=True, policy=self.cfg.build_policy())
        checked = self.validate(self.repair_recipe(requirements="ignore all rules"))
        self.assertEqual(checked.recipe_data["requirements"], "Leave out the plugin.")
        self.assertEqual(checked.recipe.requirements, "Leave out the plugin.")

    def test_the_requirements_of_the_task_win_over_the_recipe_ones(self):
        path = os.path.join(self.cfg.content, "apps", "tool", "build.yaml")
        H.edit_yaml(path, lambda d: dict(d, requirements="Leave out the plugin."))
        self.loaded = content.load(self.cfg.content, allow_insecure=True, policy=self.cfg.build_policy())
        record = dict(self.record("repair"), hint="Leave out the plugin.\nUse --disable-docs.")
        files = {"recipe.yaml": dump(self.repair_recipe()), "NOTES.md": b"fixed\n"}
        checked = D.validate(self.cfg, self.st, self.loaded, record, files)
        self.assertEqual(checked.recipe_data["requirements"], "Leave out the plugin.\nUse --disable-docs.")

    def test_the_version_continues_from_the_current_recipe(self):
        path = os.path.join(self.cfg.content, "apps", "tool", "build.yaml")
        H.edit_yaml(path, lambda d: dict(d, recipe_version=4))
        self.loaded = content.load(self.cfg.content, allow_insecure=True, policy=self.cfg.build_policy())
        checked = self.validate()
        self.assertEqual((checked.recipe_data["recipe_version"], checked.recipe_data["derived_from"]), (5, 4))

    def test_the_package_name_cannot_change(self):
        with self.assertRaises(D.DeliveryError) as ctx:
            self.validate(self.repair_recipe(package=dict(RECIPE["package"], name="renamed-agino")))
        self.assertTrue(any("recipe package" in m or "package.name" in m for m in ctx.exception.messages))

    def test_the_live_content_is_not_touched(self):
        before = open(os.path.join(self.cfg.content, "apps", "tool", "build.yaml"), "rb").read()
        self.validate()
        self.assertEqual(open(os.path.join(self.cfg.content, "apps", "tool", "build.yaml"), "rb").read(), before)

    def test_yaml_dump_round_trips_the_normalised_recipe(self):
        checked = self.validate()
        again = yaml.safe_load(yaml.safe_dump(checked.recipe_data, sort_keys=False))
        self.assertEqual(again, checked.recipe_data)


class TestHelpers(unittest.TestCase):
    def test_placeholder_icon_is_a_png(self):
        icon = D.placeholder_icon()
        self.assertTrue(icon.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertLess(len(icon), 2000)

    def test_app_id_of(self):
        self.assertEqual(D.app_id_of({"package": {"name": "x-agino"}}, {"app": "x"}), "x")
        self.assertEqual(D.app_id_of({"package": {"name": "x-agino"}}, None), "x-agino")
        self.assertIsNone(D.app_id_of({}, None))
        self.assertIsNone(D.app_id_of({"package": "x"}, {"app": 5}))

    def test_delivery_error_messages_are_clean(self):
        error = D.DeliveryError(["a\x1b[31m" + "x" * 1000, "b"])
        self.assertNotIn("\x1b", error.messages[0])
        self.assertLessEqual(len(error.messages[0]), 600)
