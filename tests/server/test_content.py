# SPDX-License-Identifier: GPL-3.0-or-later
import os
import tempfile
import unittest

from agino_store_server import content as C

from . import helpers as H


class ContentCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = H.make_content(os.path.join(self._tmp.name, "content"))

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def edit_app(self, app, change):
        H.edit_yaml(self.path("apps", app, "app.yaml"), change)

    def edit_vendor(self, change):
        H.edit_yaml(self.path("vendors", "chrome.yaml"), change)

    def errors(self):
        with self.assertRaises(C.ContentError) as ctx:
            C.load(self.root)
        return "\n".join(ctx.exception.errors)

    def assertRejected(self, needle):
        self.assertIn(needle, self.errors())


class TestValid(ContentCase):
    def test_loads(self):
        content = C.load(self.root)
        self.assertEqual([a.id for a in content.apps], ["chrome", "gimp"])
        gimp, chrome = content.app("gimp"), content.app("chrome")
        self.assertEqual(gimp.kind, "milkyway")
        self.assertEqual(chrome.vendor, "chrome")
        self.assertEqual(gimp.screenshots["zh_CN"], ("screenshots/zh_CN/1.png",))
        self.assertEqual(content.vendor("chrome").track["fingerprints"],
                         ("EB4C1BFD4F042F6DDDCCEC917721F63BD38B4796",))
        self.assertTrue(content.vendor("chrome").key_files[0].endswith("keys/google.asc"))
        self.assertEqual(content.featured[0].apps, ("chrome", "gimp"))

    def test_result_is_frozen(self):
        content = C.load(self.root)
        with self.assertRaises(Exception):
            content.apps[0].id = "x"

    def test_digest_tracks_content(self):
        before = C.content_digest(self.root)
        self.assertEqual(before, C.content_digest(self.root))
        H.write(self.path("apps", "gimp", "icon.svg"), b"<svg/>")
        self.assertNotEqual(before, C.content_digest(self.root))

    def test_missing_root(self):
        with self.assertRaises(C.ContentError):
            C.load(self.path("nope"))


class TestApp(ContentCase):
    def test_unknown_key(self):
        self.edit_app("gimp", lambda d: dict(d, colour="red"))
        self.assertRejected("unknown key 'colour'")

    def test_missing_language(self):
        self.edit_app("gimp", lambda d: dict(d, summary={"zh_CN": "x"}))
        self.assertRejected("missing en_US")

    def test_bad_language_key(self):
        self.edit_app("gimp", lambda d: dict(d, name={"zh_CN": "a", "en_US": "b", "klingon!": "c"}))
        self.assertRejected("bad language key")

    def test_other_language_allowed(self):
        self.edit_app("gimp", lambda d: dict(d, name={"zh_CN": "a", "en_US": "b", "fr": "c"}))
        self.assertEqual(C.load(self.root).app("gimp").name["fr"], "c")

    def test_bad_app_id(self):
        os.rename(self.path("apps", "gimp"), self.path("apps", "Gimp"))
        self.assertRejected("apps/Gimp")

    def test_unknown_category(self):
        self.edit_app("gimp", lambda d: dict(d, categories=["nope"]))
        self.assertRejected("unknown category 'nope'")

    def test_source_kind_combinations(self):
        self.edit_app("gimp", lambda d: dict(d, source="official"))
        self.assertRejected("kind milkyway needs source system")

    def test_deb_cannot_be_system(self):
        self.edit_app("chrome", lambda d: dict(d, source="system"))
        self.assertRejected("kind deb needs official or personal")

    def test_personal_needs_maintainer(self):
        self.edit_app("chrome", lambda d: dict(d, source="personal"))
        self.assertRejected("required for source personal")

    def test_personal_with_maintainer(self):
        self.edit_app("chrome", lambda d: dict(d, source="personal",
                                               maintainer={"name": "A", "contact": "a@example.org"}))
        self.assertEqual(C.load(self.root).app("chrome").maintainer["name"], "A")

    def test_unknown_vendor(self):
        self.edit_app("chrome", lambda d: dict(d, package={"kind": "deb", "name": "x", "vendor": "nope"}))
        self.assertRejected("unknown vendor")

    def test_package_must_match_vendor(self):
        self.edit_app("chrome", lambda d: dict(d, package={"kind": "deb", "name": "other", "vendor": "chrome"}))
        self.assertRejected("must equal the vendor package")

    def test_milkyway_with_vendor(self):
        self.edit_app("gimp", lambda d: dict(d, package={"kind": "milkyway", "name": "gimp", "vendor": "chrome"}))
        self.assertRejected("only allowed for kind deb")

    def test_bad_launchable(self):
        self.edit_app("gimp", lambda d: dict(d, launchable="../x.desktop"))
        self.assertRejected("launchable")

    def test_l10n_loaded(self):
        self.edit_app("gimp", lambda d: dict(d, l10n={"zh_CN": ["gimp-help-zh-cn"], "fr": ["gimp-help-fr", "x-fr"]}))
        gimp = C.load(self.root).app("gimp")
        self.assertEqual(gimp.l10n, {"zh_CN": ("gimp-help-zh-cn",), "fr": ("gimp-help-fr", "x-fr")})

    def test_l10n_absent_is_none(self):
        self.assertIsNone(C.load(self.root).app("gimp").l10n)

    def test_l10n_only_for_milkyway(self):
        self.edit_app("chrome", lambda d: dict(d, l10n={"zh_CN": ["chrome-l10n"]}))
        self.assertRejected("only allowed for kind milkyway")

    def test_l10n_bad_values(self):
        for bad in ({"zh-cn": ["a-l10n"]}, {"zh_CN": []}, {"zh_CN": ["Bad Name"]}, {"zh_CN": "a-l10n"},
                    {"zh_CN": ["a1", "a2", "a3", "a4", "a5"]}, {}, ["a-l10n"],
                    {l: ["pkg-" + l] for l in ("aa", "bb", "cc", "dd", "ee", "ff", "gg", "hh", "ii")}):
            with self.subTest(bad=bad):
                self.edit_app("gimp", lambda d, bad=bad: dict(d, l10n=bad))
                self.assertRejected("l10n")

    def test_yaml_python_tags_are_not_executed(self):
        H.write(self.path("apps", "gimp", "app.yaml"), b"!!python/object/apply:os.system ['true']\n")
        self.assertRejected("invalid YAML")

    def test_collects_all_errors(self):
        self.edit_app("gimp", lambda d: dict(d, categories=["nope"], colour=1))
        self.edit_app("chrome", lambda d: dict(d, source="personal"))
        self.assertGreaterEqual(len(self.errors().splitlines()), 3)


class TestAssets(ContentCase):
    def test_icon_escaping_symlink(self):
        outside = os.path.join(self._tmp.name, "secret.png")
        H.write(outside, H.png(8, 8))
        os.symlink(outside, self.path("apps", "gimp", "evil.png"))
        self.edit_app("gimp", lambda d: dict(d, icon="evil.png"))
        self.assertRejected("must stay inside the app directory")

    def test_icon_dotdot(self):
        self.edit_app("gimp", lambda d: dict(d, icon="../chrome/icon.png"))
        self.assertRejected("must stay inside the app directory")

    def test_absolute_path(self):
        self.edit_app("gimp", lambda d: dict(d, icon="/etc/passwd"))
        self.assertRejected("must stay inside the app directory")

    def test_symlink_inside_app_dir_is_fine(self):
        os.symlink("icon.svg", self.path("apps", "gimp", "alias.svg"))
        self.edit_app("gimp", lambda d: dict(d, icon="alias.svg"))
        self.assertEqual(C.load(self.root).app("gimp").icon, "alias.svg")

    def test_bad_extension(self):
        H.write(self.path("apps", "gimp", "icon.gif"), b"GIF89a")
        self.edit_app("gimp", lambda d: dict(d, icon="icon.gif"))
        self.assertRejected("file type must be one of")

    def test_missing_icon(self):
        self.edit_app("gimp", lambda d: dict(d, icon="nothing.png"))
        self.assertRejected("existing file")

    def test_svg_screenshot_rejected(self):
        H.write(self.path("apps", "gimp", "s.svg"), b"<svg/>")
        self.edit_app("gimp", lambda d: dict(d, screenshots={"en_US": [{"image": "s.svg"}]}))
        self.assertRejected("file type must be one of")

    def test_screenshot_language(self):
        self.edit_app("gimp", lambda d: dict(d, screenshots={"fr": [{"image": "icon.svg"}]}))
        self.assertRejected("language must be zh_CN or en_US")


class TestCatalogParts(ContentCase):
    def test_featured_unknown_app(self):
        H.edit_yaml(self.path("featured.yaml"), lambda d: [dict(d[0], apps=["ghost"])])
        self.assertRejected("unknown app 'ghost'")

    def test_featured_duplicate_app(self):
        H.edit_yaml(self.path("featured.yaml"), lambda d: [dict(d[0], apps=["gimp", "gimp"])])
        self.assertRejected("duplicate app")

    def test_category_duplicate(self):
        H.edit_yaml(self.path("categories.yaml"), lambda d: d + [d[0]])
        self.assertRejected("duplicate id 'graphics'")

    def test_install_profile_type(self):
        self.edit_app("chrome", lambda d: dict(d, install_profile={"debconf": ["a b seen true"]}))
        self.assertRejected("type boolean/string/select")

    def test_install_profile_newline(self):
        self.edit_app("chrome", lambda d: dict(d, install_profile={"debconf": ["a b string x\ny"]}))
        self.assertRejected("debconf[0]")

    def test_install_profile_default_path(self):
        profile = {"default_files": [{"path": "/etc/passwd", "content": "x"}]}
        self.edit_app("chrome", lambda d: dict(d, install_profile=profile))
        self.assertRejected("bad format")

    def test_install_profile_size(self):
        profile = {"default_files": [{"path": "/etc/default/x", "content": "a" * 4097}]}
        self.edit_app("chrome", lambda d: dict(d, install_profile=profile))
        self.assertRejected("at most 4 KiB")

    def test_install_profile_unknown_key(self):
        self.edit_app("chrome", lambda d: dict(d, install_profile={"scripts": []}))
        self.assertRejected("unknown key 'scripts'")

    def test_release_notes_version_must_be_string(self):
        self.edit_app("chrome", lambda d: dict(d, release_notes={1.5: {"zh_CN": ["a"], "en_US": ["b"]}}))
        self.assertRejected("version must be a quoted string")


class TestVendor(ContentCase):
    def set_track(self, **changes):
        def change(data):
            track = dict(data["track"], **changes)
            return dict(data, track={k: v for k, v in track.items() if v is not None})
        self.edit_vendor(change)

    def test_http_url(self):
        self.set_track(url="http://dl.google.com/x/")
        self.assertRejected("must be an https URL")

    def test_http_allowed_when_insecure(self):
        self.set_track(url="http://dl.google.com/x/")
        self.assertEqual(C.load(self.root, allow_insecure=True).vendor("chrome").track["suite"], "stable")

    def test_host_not_allowed(self):
        self.set_track(url="https://evil.example.org/x/")
        self.assertRejected("host is not in allowed_hosts")

    def set_hosts(self, host, url=None):
        self.edit_vendor(lambda d: dict(d, allowed_hosts=[host], track=dict(
            d["track"], url=url or "https://%s/x/" % host)))

    def test_ip_literal_host_rejected(self):
        for host in ("127.0.0.1", "10.0.0.5", "2130706433", "0x7f.1"):
            self.set_hosts(host)
            self.assertRejected("allowed_hosts[0]")

    def test_local_and_single_label_hosts_rejected(self):
        for host in ("localhost", "foo.localhost", "intranet"):
            self.set_hosts(host)
            self.assertRejected("fully qualified")

    def test_local_hosts_allowed_when_insecure(self):
        self.set_hosts("127.0.0.1", "http://127.0.0.1:8000/x/")
        self.assertEqual(C.load(self.root, allow_insecure=True).vendor("chrome").allowed_hosts, ("127.0.0.1",))

    def test_track_url_port(self):
        self.set_track(url="https://dl.google.com:8443/x/")
        self.assertRejected("port must be 443")
        self.set_track(url="https://dl.google.com:443/x/")
        C.load(self.root)
        self.set_track(url="https://dl.google.com:8443/x/")
        C.load(self.root, allow_insecure=True)

    def test_nested_quantifier_regex_rejected(self):
        for regex in (r"(?P<url>(a+)+)", r"(?P<url>(a*)*b)", r"(?P<url>(x+)*)", r"(?P<url>([a-z]+?)+)"):
            self.page_track(regex)
            self.assertRejected("nested quantifiers")

    def test_bad_fingerprint(self):
        self.set_track(fingerprints=["1234"])
        self.assertRejected("bad format")

    def test_key_file_missing(self):
        self.set_track(keys=["keys/missing.asc"])
        self.assertRejected("existing file below vendors/keys/")

    def test_key_path_escape(self):
        H.write(self.path("vendors", "other.asc"), b"x")
        self.set_track(keys=["keys/../other.asc"])
        self.assertRejected("existing file below vendors/keys/")

    def test_unknown_track_type(self):
        self.set_track(type="ftp")
        self.assertRejected("type must be one of")

    def test_unknown_vendor_key(self):
        self.edit_vendor(lambda d: dict(d, extra=1))
        self.assertRejected("unknown key 'extra'")

    def test_fetch_value(self):
        self.edit_vendor(lambda d: dict(d, fetch="sometimes"))
        self.assertRejected("must be auto or manual")

    def head_track(self, fetch):
        self.edit_vendor(lambda d: dict(d, fetch=fetch, track={"type": "head", "url": d["track"]["url"]}))

    def test_head_auto(self):
        self.head_track("auto")
        self.assertEqual(C.load(self.root).vendor("chrome").track, {"type": "head", "url": VENDOR_URL})

    def test_head_manual_rejected(self):
        self.head_track("manual")
        self.assertRejected("cannot be used with type head")

    def test_json_track(self):
        self.edit_vendor(lambda d: dict(d, track={"type": "json", "url": d["track"]["url"], "strip_js": True,
                                                  "version_path": "version", "url_path": "x64.deb"}))
        self.assertTrue(C.load(self.root).vendor("chrome").track["strip_js"])

    def test_json_bad_path(self):
        self.edit_vendor(lambda d: dict(d, track={"type": "json", "url": d["track"]["url"],
                                                  "version_path": "a..b", "url_path": "x"}))
        self.assertRejected("version_path")

    def page_track(self, regex, fetch="auto"):
        self.edit_vendor(lambda d: dict(d, fetch=fetch, track={"type": "page", "url": d["track"]["url"],
                                                               "link_regex": regex}))

    def test_page_regex_needs_url_group(self):
        self.page_track(r"href=\"(?P<link>[^\"]+)\"")
        self.assertRejected("needs a named group (?P<url>...)")

    def test_page_regex_invalid(self):
        self.page_track("(unclosed")
        self.assertRejected("invalid regular expression")

    def test_page_regex_ok_and_manual_needs_version(self):
        self.page_track(r"(?P<url>https://dl\.google\.com/[^\"]+)")
        C.load(self.root)
        self.page_track(r"(?P<url>https://dl\.google\.com/[^\"]+)", fetch="manual")
        self.assertRejected("fetch manual needs a (?P<version>...) group")

    def test_redirect_regex_needs_version(self):
        self.edit_vendor(lambda d: dict(d, track={"type": "redirect", "url": d["track"]["url"],
                                                  "version_regex": r"/(\d+)/"}))
        self.assertRejected("needs a named group (?P<version>...)")


VENDOR_URL = H.VENDOR["track"]["url"]


if __name__ == "__main__":
    unittest.main()
