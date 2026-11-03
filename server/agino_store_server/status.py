# SPDX-License-Identifier: GPL-3.0-or-later
"""`agino-store-server status --json`: the read-only interface for the admin console (docs/server.md section 11).

Never takes state/.lock, writes nothing, runs no sudo and uses no network.
"""

import datetime
import json

from . import status_apps, status_sandbox, status_state
from .status_io import Errors, sanitize

SCHEMA = 1
SECTIONS = ("apps", "candidates", "approved", "vendors", "catalog", "sandbox", "timers")
FUTURE_SECTIONS = ("upstreams", "submissions", "sessions", "builds")
MAX_BYTES = 8 << 20
# (section, path inside the section) of the lists the size guard may shorten; newest or most important first.
TRUNCATABLE = (("apps", ()), ("candidates", ()), ("approved", ("log",)), ("vendors", ()),
               ("sandbox", ("queue",)), ("sandbox", ("jobs",)), ("sandbox", ("reports",)))


class UnknownSection(ValueError):
    pass


def resolve_sections(names):
    """The requested section names (default all), validated; UnknownSection for anything unknown."""
    if not names:
        return list(SECTIONS)
    bad = [n for n in names if n not in SECTIONS + FUTURE_SECTIONS]
    if bad:
        raise UnknownSection("unknown section %s (known: %s)" % (", ".join(sorted(set(bad))),
                                                                  ", ".join(SECTIONS + FUTURE_SECTIONS)))
    return [n for n in dict.fromkeys(names) if n in SECTIONS]


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _collect(cfg, wanted, errors):
    content = status_apps.load_content(cfg, errors) if {"apps", "vendors"} & set(wanted) else None
    candidates = (status_state.load_candidates(cfg, errors)
                  if {"apps", "candidates"} & set(wanted) else [])
    approved = status_state.read_approved(cfg, errors) if {"apps", "approved"} & set(wanted) else {}
    makers = {
        "apps": lambda: status_apps.section(cfg, content, approved, status_state.newest_status(candidates), errors),
        "candidates": lambda: candidates,
        "approved": lambda: status_state.approved_section(cfg, approved, errors),
        "vendors": lambda: status_state.vendors_section(cfg, content, errors),
        "catalog": lambda: status_state.catalog_section(cfg, errors),
        "sandbox": lambda: status_sandbox.sandbox_section(cfg, errors),
        "timers": lambda: status_sandbox.timers_section(errors),
    }
    out = {}
    for name in wanted:
        try:
            out[name] = makers[name]()
        except Exception as e:  # noqa: BLE001 - one broken section must not hide the others
            errors.add(name, "%s: %s" % (type(e).__name__, e))
    return out


def _list_at(sections, name, path):
    node = sections.get(name)
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, list) else None


def _dump(doc):
    return json.dumps(doc, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _guard(doc, limit):
    """Halve the largest list until the document fits; the section names that were cut go to doc['truncated']."""
    text = _dump(doc)
    cut = []
    while len(text.encode("ascii")) > limit:
        lists = [(len(lst), name, path) for name, path in TRUNCATABLE
                 if (lst := _list_at(doc["sections"], name, path))]
        if not lists:
            break
        _, name, path = max(lists)
        lst = _list_at(doc["sections"], name, path)
        del lst[len(lst) // 2:]
        if name not in cut:
            cut.append(name)
            doc["truncated"] = cut
        text = _dump(doc)
    return text


def build(cfg, names, clean, limit=None):
    """The status document as JSON text. `clean` is the control-character sanitizer for strings."""
    wanted = resolve_sections(names)
    limit = MAX_BYTES if limit is None else limit
    errors = Errors()
    sections = _collect(cfg, wanted, errors)
    doc = sanitize({"schema": SCHEMA, "generated": _now(), "sections": sections, "errors": errors.items}, clean)
    return _guard(doc, limit)
