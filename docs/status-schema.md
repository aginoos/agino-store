# `agino-store-server status --json` example

Schema version 2; field reference in `docs/server.md` section 11. All strings are sanitised, output is
ASCII JSON. Sections not requested (or not implemented yet: `submissions`, `sessions`) are absent. Schema 2 added `upstreams`, `builds` and `builder`. This example is abridged
(lists have one entry) and pretty-printed; the real output is one line.

```json
{
  "schema": 2,
  "generated": "2026-11-03T08:00:00Z",
  "sections": {
    "apps": [{
      "id": "chrome", "name": {"zh_CN": "Chrome", "en_US": "Chrome"},
      "summary": {"zh_CN": "...", "en_US": "Browser"},
      "description": {"zh_CN": ["...", "..."], "en_US": ["Paragraph one", "Paragraph two"]},
      "license": "LicenseRef-proprietary", "categories": ["web"],
      "source": "official", "kind": "deb", "package": "google-chrome-stable", "vendor": "chrome",
      "proprietary": true, "developer": {"zh_CN": "...", "en_US": "Google"}, "homepage": null,
      "maintainer": null, "launchable": "google-chrome.desktop", "has_icon": true,
      "description_source": "confirmed", "description_confirmed": {"by": "alice", "at": "2026-11-03T08:00:00Z"},
      "l10n": false,
      "shots": {"state": "draft", "scenes": 2, "langs": ["zh_CN", "en_US"],
                "counts": {"draft": 4, "approved": 0, "rejected": 1}},
      "approved": {"version": "156.0-1", "approved_at": "2026-11-01T08:00:00Z", "by": "alice"},
      "current_candidate_status": "pending"
    }],
    "candidates": [{
      "id": "chrome/157.0-1_0123456789ab", "app": "chrome", "vendor": "chrome",
      "package": "google-chrome-stable", "version": "157.0-1", "size": 145633696,
      "installed_size": 467080192, "found": "2026-11-02T08:00:00Z", "status": "pending",
      "origin": {"type": "apt", "signed_by": "EB4C1BFD4F042F6DDDCCEC917721F63BD38B4796"},
      "source_class": "official",
      "checks": {"deps": {"ok": true, "detail": ""},
                 "inspect": {"flags": []},
                 "diff": {"against": "156.0-1", "scripts_changed": [], "flags_added": [], "summary": "no changes"},
                 "sandbox": {"status": "pass", "summary": "ok", "report": "chrome_0123456789ab.json"}},
      "decided": null
    }],
    "approved": {"apps": {"chrome": {"version": "156.0-1", "sha256": "...", "candidate": "...",
                                     "approved_at": "2026-11-01T08:00:00Z", "by": "alice", "note": ""}},
                 "log": [{"at": "2026-11-01T08:00:00Z", "action": "approve", "app": "chrome", "by": "alice"}]},
    "vendors": [{"id": "chrome", "name": "google-chrome-stable", "in_content": true,
                 "last_check": "2026-11-03T06:00:00Z", "last_ok": "2026-11-03T06:00:00Z",
                 "last_error": null, "seen_version": "157.0-1"}],
    "catalog": {"state": {"serial": 20261103, "revision": "r20261103-abc", "built_at": "2026-11-03T02:10:00Z",
                          "inputs_sha256": "...", "apps": 12},
                "published": {"serial": 20261103, "revision": "r20261103-abc", "created": "2026-11-03T02:10:00Z",
                              "expires": "2026-11-17T02:10:00Z", "valid_until": "2026-11-17T02:10:00Z",
                              "apps": 12, "signature_present": true}},
    "sandbox": {"queue": [{"job": "chrome_0123456789ab", "mode": "check", "app": "chrome", "version": "157.0-1",
                           "size": 145633696, "created": "2026-11-02T08:00:00Z"}],
                "jobs": [{"job": "chrome_0123456789ab", "status": "error", "attempts": 2, "reason": "qga timeout",
                          "updated": "2026-11-02T08:20:00Z"}],
                "reports": [{"name": "chrome_0123456789ab.json", "location": "done", "app": "chrome",
                             "version": "157.0-1", "result": "pass", "finished": "2026-11-02T08:30:00Z",
                             "at": "2026-11-02T08:31:00Z", "summary": "ok"}]},
    "shots": [{"id": "chrome", "counts": {"draft": 1, "approved": 1, "rejected": 0},
               "images": [{"lang": "zh_CN", "file": "01-main.png", "scene": "main", "width": 1280, "height": 800,
                           "size": 123456, "sha256": "...", "source": "sandbox", "status": "approved",
                           "reviewer": "alice", "reviewed": "2026-11-03T08:00:00Z", "note": "",
                           "origin": "chrome_0123456789ab"}]}],
    "categories": [{"id": "web", "name": {"zh_CN": "网络", "en_US": "Web"}}],
    "upstreams": [{"app": "gimp", "repo": "https://github.com/GNOME/gimp.git", "in_content": true, "auto_task": false,
                   "latest_version": "3.0.4", "last_check": "2026-11-04T06:00:00Z", "last_ok": "2026-11-04T06:00:00Z",
                   "last_error": null, "tag_moved": 1, "seen_count": 12, "versions": ["3.0.4", "3.0.2", "3.0.0"],
                   "tag_moves": [{"version": "3.0.2", "tag": "GIMP_3_0_2", "old_commit": "0123456789abcdef0123456789abcdef01234567",
                                  "new_commit": "89abcdef0123456789abcdef0123456789abcdef", "at": "2026-11-04T06:00:00Z"}]}],
    "builds": [{"id": "0123456789abcdef", "app": "gimp", "version": "3.0.4+agino1", "upstream_version": "3.0.4",
                "tag": "GIMP_3_0_4", "commit": "0123456789abcdef0123456789abcdef01234567", "attempt": 1,
                "status": "done", "by": "alice", "created": "2026-11-04T06:10:00Z", "started": "2026-11-04T06:12:00Z",
                "finished": "2026-11-04T07:05:00Z", "error": "",
                "artifact": {"file": "gimp-agino_3.0.4+agino1_amd64.deb", "sha256": "...", "size": 52428800},
                "candidate": "3.0.4+agino1_0123456789ab"}],
    "builder": {"running": null, "queued": 0},
    "timers": [{"unit": "agino-store-track.timer", "LoadState": "loaded", "ActiveState": "active",
                "SubState": "waiting", "LastTriggerUSec": "Tue 2026-11-03 06:00:00 UTC",
                "NextElapseUSecRealtime": "Tue 2026-11-03 12:00:00 UTC"}]
  },
  "errors": [{"section": "candidates", "detail": "chrome/9.0-1_bbbbbbbbbbbb.json: invalid JSON"}],
  "truncated": ["candidates"]
}
```

A candidate built from source has `"origin": {"type": "built", "source_url": "https://github.com/...git",
"source_ref": "GIMP_3_0_4", "source_commit": "<40 hex>", "build_job": "<16 hex>"}` and `"source_class": "personal"`.
`builds` lists the 50 newest tasks (newest first; no recipe, no log); `builder.queued` counts tasks in
`queued`, `fetching` and `ready`; `versions` in `upstreams` are the seen versions, newest first, at most 30.

`description_source` is one of `ai`, `confirmed`, `submitter` (default), `official`; `description_confirmed` is
`{by, at}` or `null`. Section `categories` lists `categories.yaml` in file order and is also a valid `--section`
name. `truncated` is present only when the size guard cut a list. Exit code is 0 even when `errors` is
non-empty; 2 for a missing `--json` or an unknown `--section`.
