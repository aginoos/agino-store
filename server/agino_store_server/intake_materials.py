# SPDX-License-Identifier: GPL-3.0-or-later
"""The reading material an AI task gets (contract ai-intake 10.5, `submitted` -> `fetching`).

The host fetches it with the controlled fetchers (https only, hosts on the allow list, size limits, no
redirect to another host) and never executes any of it:

  inputs/page.txt       the page at the address as plain text (HTML parsed with the standard library, scripts
                        and styles dropped)
  inputs/links.txt      the links of that page, `<url><TAB><text>`
  inputs/repo/          a shallow copy of the repository when the address is a git repository (symbolic links
                        are left out)
  inputs/categories.txt the store categories the agent may use in app.yaml
  inputs/MATERIALS.txt  what was and was not fetched

A repair task adds inputs/previous-recipe.yaml, inputs/current-app.yaml (the store entry, which the agent copies back
because the VM runner requires an app.yaml), inputs/failure.log (the tail of the failed build's log) and
inputs/upstream-diff.txt (`git diff --stat` between the last built tag and the failing one), and
inputs/repo/ holds the failing version. The result is one tar, state/intake/<id>/inputs.tar, from which every
workspace of the task is made (intake_workspace).
"""

import dataclasses
import html.parser
import io
import os
import re
import shutil
import tarfile
import tempfile
import urllib.parse

from . import builds, debinfo, fetch, source_assembly, upstream_fetch
from .upstream_fetch import UpstreamError

INPUTS = "inputs"
FORGE_HOSTS = ("github.com", "gitlab.com", "codeberg.org", "invent.kde.org", "gitlab.gnome.org")
SKIP_TAGS = frozenset(("script", "style", "noscript", "template", "svg", "iframe", "object", "canvas"))
BLOCK_TAGS = frozenset(("p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header",
                        "footer", "nav", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "dt", "dd"))
MAX_LINKS = 400
MAX_LINK_URL = 400
MAX_LINK_TEXT = 120
FAILURE_LOG_BYTES = 64 << 10
DIFF_BYTES = 64 << 10
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_SPACES = re.compile(r"[ \t\r\f\v ]+")
_BLANKS = re.compile(r"\n{3,}")
_LINK_BAD = re.compile(r"[\x00-\x20\x7f-\x9f<>\"'`\\]")


class MaterialError(Exception):
    """The reading material cannot be made (the task fails with this message)."""


def clean_block(text):
    """Text for a file the agent reads: control characters out, runs of blanks and blank lines squeezed."""
    text = _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip() + "\n"


# HTML to text and links

class _Page(html.parser.HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.parts, self.links = [], []
        self._skip = 0
        self._href = None
        self._anchor = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip += 1
            return
        if tag in BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "a" and not self._skip:
            self._href = dict(attrs).get("href")
            self._anchor = []

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if tag in BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "a" and self._href is not None:
            self._add_link(self._href, "".join(self._anchor))
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        self.parts.append(data)
        if self._href is not None:
            self._anchor.append(data)

    def _add_link(self, href, text):
        url = urllib.parse.urljoin(self.base, href.strip())
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or _LINK_BAD.search(url):
            return
        url = urllib.parse.urlunsplit(parts._replace(fragment=""))
        if len(url) <= MAX_LINK_URL:
            self.links.append((url, _SPACES.sub(" ", _CONTROL.sub("", text)).strip()[:MAX_LINK_TEXT]))


def html_to_text(markup, base_url):
    """(text, links) of an HTML document; links are absolute, de-duplicated, at most MAX_LINKS."""
    parser = _Page(base_url)
    try:
        parser.feed(markup)
        parser.close()
    except (ValueError, AssertionError, RecursionError):
        pass   # broken markup: what was read up to here is used
    seen, links = set(), []
    for url, text in parser.links:
        if url not in seen and len(links) < MAX_LINKS:
            seen.add(url)
            links.append((url, text))
    return clean_block("".join(parser.parts)), links


def links_text(links):
    return "".join("%s\t%s\n" % (url, text) for url, text in links)


# fetching

def _hosts(cfg, record):
    return source_assembly.hosts_for(cfg, record)


def _limited(cfg, record):
    return dataclasses.replace(cfg, upstream_allowed_hosts=_hosts(cfg, record))


def is_repository(address):
    parts = urllib.parse.urlsplit(address)
    segments = [s for s in parts.path.split("/") if s]
    return parts.path.endswith(".git") or ((parts.hostname or "") in FORGE_HOSTS and len(segments) == 2)


def fetch_page(cfg, record):
    """(text, links) of the page at the record's address; MaterialError when it cannot be fetched."""
    fetcher = fetch.Fetcher(cfg, _hosts(cfg, record))
    try:
        response = fetcher.get(record["address"], max_bytes=cfg.intake_max_page_kib << 10)
    except fetch.FetchError as e:
        raise MaterialError("cannot fetch the address: %s" % debinfo.clean_text(e)) from e
    data = response.data or b""
    if b"\x00" in data[:4096]:
        raise MaterialError("the address does not return a text page")
    return html_to_text(data.decode("utf-8", "replace"), response.url or record["address"])


def _copy_tar(out, src, prefix):
    """Add every file and directory of the tar `src` below `prefix`; symbolic links are left out."""
    with tarfile.open(src, "r:*") as tar:
        for member in tar:
            if member.isreg() or member.isdir():
                info = member.replace(name="%s/%s" % (prefix, member.name), deep=False)
                out.addfile(info, tar.extractfile(member) if member.isreg() else None)


def _text_member(out, name, text):
    data = text.encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.type = len(data), 0o644, tarfile.REGTYPE
    out.addfile(info, io.BytesIO(data))


def _dir_member(out, name):
    info = tarfile.TarInfo(name)
    info.type, info.mode = tarfile.DIRTYPE, 0o755
    out.addfile(info)


def _repo_part(cfg, repo, tag, commit, work, budget):
    """(tar path, note) of a shallow copy of the repository (a tag when given, else the default branch)."""
    raw, tar = os.path.join(work, "raw.tar"), os.path.join(work, "repo.tar")
    if tag:
        found = upstream_fetch.git_checkout_tar(cfg, repo, tag, commit, work, raw, budget)
    else:
        found = upstream_fetch.git_head_tar(cfg, repo, work, raw, budget)
    upstream_fetch.normalise_tar(raw, tar, budget)
    os.unlink(raw)
    return tar, "repo/: repository %s at %s (commit %s); symbolic links are not included" % (
        repo, tag or "the default branch", found[:12])


def _tail(path, limit):
    """The last `limit` bytes of a log file written by the runner (untrusted), cleaned; '' when unreadable."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return ""
    with os.fdopen(fd, "rb") as f:
        try:
            size = os.fstat(f.fileno()).st_size
            f.seek(max(0, size - limit))
            data = f.read(limit)
        except OSError:
            return ""
    return clean_block(data.decode("utf-8", "replace"))


def _read_regular(path, limit):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise MaterialError("cannot read %s: %s" % (os.path.basename(path), e.strerror)) from e
    with os.fdopen(fd, "rb") as f:
        if os.fstat(f.fileno()).st_size > limit:
            raise MaterialError("%s is larger than %d KiB" % (os.path.basename(path), limit >> 10))
        return f.read(limit)


def _categories_text(loaded):
    lines = ["%s\t%s" % (c.id, c.name.get("en_US", "")) for c in loaded.categories]
    return "# category id <TAB> English name; app.yaml `categories` takes ids from this list\n" + "\n".join(lines) + "\n"


def _repair_parts(cfg, st, loaded, record, work, notes):
    """(extra text files {name: text}, repo tar path or None) for a repair task."""
    of = record["repair_of"]
    app, version = of["app"], of["version"]
    up = loaded.upstream(app)
    texts = {}
    recipe_path = os.path.join(loaded.root, "apps", app, "build.yaml")
    texts["previous-recipe.yaml"] = _read_regular(recipe_path, 1 << 20).decode("utf-8", "replace")
    texts["current-app.yaml"] = _read_regular(os.path.join(loaded.root, "apps", app, "app.yaml"),
                                              1 << 20).decode("utf-8", "replace")
    failed = st.build(of["failed_task"]) if of.get("failed_task") else None
    log = _tail(os.path.join(builds.out_dir(cfg, failed["id"]), "build.log"), FAILURE_LOG_BYTES) if failed else ""
    texts["failure.log"] = log or "(no build log was kept)\n"
    if failed and failed.get("error"):
        texts["failure.log"] = "error: %s\n\n%s" % (debinfo.clean_text(failed["error"]), texts["failure.log"])
    seen = (st.upstream_memo(app).get("seen") or {}).get(version) or {}
    previous = _last_built(st, app, version)
    texts["upstream-diff.txt"] = _diffstat(cfg, up, previous, seen, work, notes)
    tar = None
    if up is not None and seen.get("tag"):
        try:
            tar, note = _repo_part(cfg, up.repo, seen["tag"], seen.get("commit"), work, cfg.intake_max_inputs_mib << 20)
            notes.append(note)
        except UpstreamError as e:
            notes.append("repository not included: %s" % debinfo.clean_text(e))
    return texts, tar


def _last_built(st, app, version):
    """The tag of the newest successfully built version before `version`, or None."""
    best = None
    for b in st.builds(app=app):
        if b.get("status") == "done" and b.get("mode") != "ai" and b.get("tag") and b.get("upstream_version") != version:
            if best is None or debinfo.version_gt(b["upstream_version"], best["upstream_version"]):
                best = b
    return best


def _diffstat(cfg, up, previous, seen, work, notes):
    if up is None or previous is None or not seen.get("tag"):
        return "(no earlier successful build to compare with)\n"
    try:
        text = upstream_fetch.git_tag_diffstat(cfg, up.repo, previous["tag"], seen["tag"], work)
    except UpstreamError as e:
        notes.append("upstream diff not available: %s" % debinfo.clean_text(e))
        return "(the upstream diff could not be made: %s)\n" % debinfo.clean_text(e)
    return "git diff --stat %s %s\n\n%s" % (previous["tag"], seen["tag"], clean_block(text)[:DIFF_BYTES])


def build_inputs(cfg, st, record, loaded):
    """Fetch the reading material of an intake record and write state/intake/<id>/inputs.tar. Returns the path.
    MaterialError when the address cannot be read at all."""
    limited = _limited(cfg, record)
    directory = st.intake_dir(record["id"], create=True)
    budget = cfg.intake_max_inputs_mib << 20
    notes, texts, repo_tar = [], {}, None
    work = tempfile.mkdtemp(prefix=".work-", dir=directory)
    try:
        if record["kind"] == "repair":
            texts, repo_tar = _repair_parts(limited, st, loaded, record, work, notes)
        else:
            if not is_repository(record["address"]):
                text, links = fetch_page(limited, record)
                texts["page.txt"], texts["links.txt"] = text, links_text(links)
                notes.append("page.txt and links.txt: the page at the address")
            else:
                try:
                    repo_tar, note = _repo_part(limited, record["address"], None, None, work, budget)
                    notes.append(note)
                except UpstreamError as e:
                    raise MaterialError("cannot read the repository: %s" % debinfo.clean_text(e)) from e
        texts["categories.txt"] = _categories_text(loaded)
        texts["MATERIALS.txt"] = "\n".join(notes + ["The fetch was done by the host; nothing was executed."]) + "\n"
        return _write_inputs(directory, texts, repo_tar, budget)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _write_inputs(directory, texts, repo_tar, budget):
    final = os.path.join(directory, "inputs.tar")
    fd, tmp = tempfile.mkstemp(prefix=".inputs-", dir=directory)
    os.close(fd)
    try:
        with tarfile.open(tmp, "w", format=tarfile.PAX_FORMAT) as out:
            _dir_member(out, INPUTS)
            for name, text in sorted(texts.items()):
                _text_member(out, "%s/%s" % (INPUTS, name), clean_block(text) if name not in ("links.txt", "categories.txt") else text)
            if repo_tar:
                _dir_member(out, INPUTS + "/repo")
                _copy_tar(out, repo_tar, INPUTS + "/repo")
        if os.path.getsize(tmp) > budget + (1 << 20):
            raise MaterialError("the reading material exceeds %d MiB" % (budget >> 20))
        upstream_fetch.verify_tar(tmp, budget + (2 << 20))
        os.replace(tmp, final)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise
    return final
