#!/usr/bin/python3 -I
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate the sample catalog source and its placeholder SVG assets (icons, screenshots).

Icons are coloured rounded squares with a letter; screenshots are schematic windows with a
Chinese or English menu bar. No vendor artwork is used. Output is deterministic.
"""

import argparse
import hashlib
import json
import os
import sys

DEB_URL = "file:///usr/share/agino-store/test-debs/%s.deb"
PLACEHOLDER_SHA = "0" * 64

CATEGORIES = [
    ("office", "办公", "Office"), ("chat", "聊天", "Chat"), ("web", "网络", "Web"),
    ("dev", "开发", "Development"), ("media", "影音", "Media"), ("graphics", "图形", "Graphics"),
    ("games", "游戏", "Games"), ("tools", "工具", "Tools"),
]

# id, category, source, zh name, en name, zh summary, en summary, license, proprietary, downloads, updated
APPS = [
    ("gimp", "graphics", "system", "GIMP", "GIMP", "图像编辑器", "Image editor", "GPL-3.0-or-later", False, 9200, "2026-10-12"),
    ("vlc", "media", "system", "VLC 播放器", "VLC media player", "全能媒体播放器", "Plays almost any media", "GPL-2.0-or-later", False, 15800, "2026-10-20"),
    ("libreoffice", "office", "system", "LibreOffice", "LibreOffice", "完整的办公套件", "Complete office suite", "MPL-2.0", False, 12400, "2026-10-25"),
    ("firefox-esr", "web", "system", "Firefox ESR", "Firefox ESR", "网页浏览器（长期支持版）", "Web browser (extended support)", "MPL-2.0", False, 14300, "2026-10-28"),
    ("krita", "graphics", "system", "Krita", "Krita", "数字绘画", "Digital painting", "GPL-3.0-or-later", False, 5100, "2026-09-30"),
    ("inkscape", "graphics", "system", "Inkscape", "Inkscape", "矢量图形编辑器", "Vector graphics editor", "GPL-3.0-or-later", False, 4700, "2026-09-18"),
    ("obs-studio", "media", "system", "OBS Studio", "OBS Studio", "录屏和直播", "Screen recording and streaming", "GPL-2.0-or-later", False, 6300, "2026-10-05"),
    ("keepassxc", "tools", "system", "KeePassXC", "KeePassXC", "本地密码管理器", "Offline password manager", "GPL-3.0-or-later", False, 3900, "2026-10-14"),
    ("supertuxkart", "games", "system", "SuperTuxKart", "SuperTuxKart", "卡通赛车游戏", "Cartoon kart racing", "GPL-3.0-or-later", False, 2500, "2026-08-22"),
    ("wps-office", "office", "official", "WPS Office", "WPS Office", "办公套件", "Office suite", "LicenseRef-proprietary", True, 20100, "2026-10-28"),
    ("google-chrome", "web", "official", "Google Chrome", "Google Chrome", "网页浏览器", "Web browser", "LicenseRef-proprietary", True, 18700, "2026-10-30"),
    ("code", "dev", "official", "Visual Studio Code", "Visual Studio Code", "代码编辑器", "Code editor", "LicenseRef-proprietary", True, 11200, "2026-10-27"),
    ("wechat", "chat", "official", "微信", "WeChat", "即时通讯", "Instant messaging", "LicenseRef-proprietary", True, 25300, "2026-10-22"),
    ("quick-notes", "tools", "personal", "快速笔记", "Quick Notes", "轻量 Markdown 笔记", "Lightweight Markdown notes", "MIT", False, 800, "2026-10-02"),
    ("pixel-forge", "graphics", "personal", "像素工坊", "Pixel Forge", "像素画编辑器", "Pixel art editor", "GPL-3.0-or-later", False, 650, "2026-09-12"),
    ("tg-lite", "chat", "personal", "轻聊", "TG Lite", "轻量聊天客户端", "Lightweight chat client", "Apache-2.0", False, 420, "2026-10-09"),
    ("hex-peek", "dev", "personal", "十六进制查看器", "Hex Peek", "二进制文件查看器", "Binary file viewer", "MIT", False, 310, "2026-08-30"),
    # Vendor distribution (licence forbids the store to distribute the deb): see VENDOR_APPS.
    ("acme-draw", "graphics", "official", "示例绘图", "Acme Draw", "矢量绘图", "Vector drawing", "LicenseRef-proprietary", True, 700, "2026-10-31"),
    ("acme-video", "media", "official", "示例视频", "Acme Video", "视频剪辑", "Video editing", "LicenseRef-proprietary", True, 650, "2026-11-01"),
]

# app id -> download.direct. acme-draw sends the user to the vendor's download page,
# acme-video straight to the file (deb.vendor_url).
VENDOR_APPS = {"acme-draw": False, "acme-video": True}

PALETTE = ["#e5484d", "#f76b15", "#e5a000", "#30a46c", "#12a594", "#0091ff", "#3e63dd", "#8e4ec6", "#d6409f"]


def color(app_id, shift=0):
    h = int(hashlib.sha256(app_id.encode()).hexdigest()[:8], 16)
    return PALETTE[(h + shift) % len(PALETTE)]


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def icon_svg(app_id, name):
    letter = esc(name[0].upper())
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">\n'
            '  <rect width="128" height="128" rx="28" fill="%s"/>\n'
            '  <text x="64" y="64" font-family="sans-serif" font-size="68" font-weight="bold" fill="#ffffff" '
            'text-anchor="middle" dominant-baseline="central">%s</text>\n</svg>\n') % (color(app_id), letter)


MENUS = {"zh_CN": ["文件", "编辑", "查看", "帮助"], "en_US": ["File", "Edit", "View", "Help"]}
PANES = {"zh_CN": ["示意界面 %d", "示例内容"], "en_US": ["Sample view %d", "Sample content"]}


def shot_svg(app_id, name, lang, index):
    c = color(app_id, index)
    menus = "".join('<text x="%d" y="62" font-family="sans-serif" font-size="20" fill="#333">%s</text>' % (24 + i * 90, esc(m))
                    for i, m in enumerate(MENUS[lang]))
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">\n'
            '  <rect width="1280" height="720" fill="#e9ecef"/>\n'
            '  <rect x="40" y="30" width="1200" height="660" rx="14" fill="#ffffff" stroke="#c4c9cf"/>\n'
            '  <rect x="40" y="30" width="1200" height="48" rx="14" fill="#f3f4f6"/>\n'
            '  <circle cx="68" cy="54" r="7" fill="#ff5f57"/><circle cx="92" cy="54" r="7" fill="#febc2e"/>'
            '<circle cx="116" cy="54" r="7" fill="#28c840"/>\n'
            '  <g transform="translate(120,16)">%s</g>\n'
            '  <rect x="60" y="110" width="220" height="560" rx="8" fill="%s" opacity="0.18"/>\n'
            '  <rect x="300" y="110" width="920" height="560" rx="8" fill="%s" opacity="0.08"/>\n'
            '  <text x="760" y="330" font-family="sans-serif" font-size="44" fill="%s" text-anchor="middle">%s</text>\n'
            '  <text x="760" y="390" font-family="sans-serif" font-size="26" fill="#555" text-anchor="middle">%s</text>\n'
            '</svg>\n') % (menus, c, c, c, esc(name), esc(PANES[lang][0] % index + " - " + PANES[lang][1]))


def paragraphs(name_zh, name_en, summary_zh, summary_en):
    return {
        "zh_CN": ["%s：%s。" % (name_zh, summary_zh), "这是示例目录中的说明文字，用于界面和测试。"],
        "en_US": ["%s: %s." % (name_en, summary_en), "This is sample text from the test catalog, used for the UI and tests."],
    }


def build(assets_out):
    apps = []
    for (aid, cat, source, nzh, nen, szh, sen, lic, prop, dl, updated) in APPS:
        os.makedirs(os.path.join(assets_out, "icons"), exist_ok=True)
        os.makedirs(os.path.join(assets_out, "shots"), exist_ok=True)
        with open(os.path.join(assets_out, "icons", aid + ".svg"), "w", encoding="utf-8") as f:
            f.write(icon_svg(aid, nen))
        shots = {}
        for lang, label in (("zh_CN", nzh), ("en_US", nen)):
            shots[lang] = []
            for i in (1, 2):
                rel = "shots/%s-%s-%d.svg" % (aid, lang, i)
                with open(os.path.join(assets_out, rel), "w", encoding="utf-8") as f:
                    f.write(shot_svg(aid, label, lang, i))
                shots[lang].append({"image": rel, "thumb": rel, "width": 1280, "height": 720})

        if source == "system":
            package = {"kind": "milkyway", "name": aid}
        else:
            package = {"kind": "deb", "name": aid, "deb": {
                "version": "1.0.0", "url": DEB_URL % aid, "vendor_url": "https://example.invalid/" + aid,
                "sha256": PLACEHOLDER_SHA, "size": 1000000, "installed_size": 5000000}}
            if aid in VENDOR_APPS:
                del package["deb"]["url"]
                if VENDOR_APPS[aid]:
                    package["deb"]["vendor_url"] = "https://example.invalid/%s/download/%s-1.0.0.deb" % (aid, aid)
                package["distribution"] = "vendor"
                package["download"] = {"page": "https://example.invalid/%s/download" % aid, "direct": VENDOR_APPS[aid]}

        detail = {
            "description": paragraphs(nzh, nen, szh, sen),
            "developer": {"zh_CN": "示例开发者", "en_US": "Sample Developer"},
            "homepage": "https://example.invalid/" + aid,
            "maintainer": "Sample Maintainer <maintainer@example.invalid>" if source == "personal" else None,
            "screenshots": shots,
            "releases": [
                {"version": "1.0.0", "date": updated, "notes": {"zh_CN": ["修复若干问题", "性能改进"], "en_US": ["Fixed several issues", "Performance improvements"]}},
                {"version": "0.9.0", "date": "2026-07-01", "notes": {"zh_CN": ["首个示例版本"], "en_US": ["First sample release"]}},
            ],
        }
        if aid == "code":
            detail["install_profile"] = {"debconf": ["code code/add-microsoft-repo boolean false"]}
        if aid == "google-chrome":
            detail["install_profile"] = {"default_files": [{
                "path": "/etc/default/google-chrome",
                "content": "repo_add_once=\"false\"\nrepo_reenable_on_distupgrade=\"false\"\n"}]}

        apps.append({
            "id": aid,
            "name": {"zh_CN": nzh, "en_US": nen},
            "summary": {"zh_CN": szh, "en_US": sen},
            "icon": "icons/%s.svg" % aid,
            "categories": [cat],
            "source": source,
            "license": lic,
            "proprietary": prop,
            "updated": updated,
            "package": package,
            "detail": detail,
            "stats": {"downloads": dl},
        })

    return {
        "categories": [{"id": i, "name": {"zh_CN": z, "en_US": e}, "order": n + 1}
                       for n, (i, z, e) in enumerate(CATEGORIES)],
        "featured": [
            {"id": "spotlight-office", "title": {"zh_CN": "高效办公", "en_US": "Get things done"},
             "subtitle": {"zh_CN": "办公软件精选", "en_US": "Office picks"},
             "apps": ["wps-office", "libreoffice"], "banner": True},
            {"id": "spotlight-create", "title": {"zh_CN": "创作工具", "en_US": "Create"},
             "subtitle": {"zh_CN": "绘画与设计", "en_US": "Paint and design"},
             "apps": ["krita", "inkscape", "gimp"], "banner": True},
            {"id": "essentials", "title": {"zh_CN": "装机必备", "en_US": "Essentials"},
             "subtitle": {"zh_CN": "新系统常用软件", "en_US": "Common apps for a new system"},
             "apps": ["firefox-esr", "wps-office", "wechat", "vlc", "keepassxc"], "banner": False},
        ],
        "apps": apps,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--assets-out", required=True)
    ap.add_argument("--source-out", required=True)
    args = ap.parse_args()
    src = build(args.assets_out)
    with open(args.source_out, "w", encoding="utf-8") as f:
        json.dump(src, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("wrote %s and assets in %s" % (args.source_out, args.assets_out))


if __name__ == "__main__":
    sys.exit(main())
