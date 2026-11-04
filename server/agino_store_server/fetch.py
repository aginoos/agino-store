# SPDX-License-Identifier: GPL-3.0-or-later
"""Guarded HTTPS downloads (docs/server.md section 6, first bullet).

Everything fetched here is hostile until proven otherwise: hosts are checked on every
redirect hop, sizes are capped while streaming, file names are sanitized.
"""

import contextlib
import hashlib
import http.client
import ipaddress
import os
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

CONNECT_TIMEOUT = 60
TOTAL_TIMEOUT = 900
CHUNK = 1 << 16
READ_SIZE = 8192
HTTPS_PORT = 443
SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._+~-]")

_throttle_lock = threading.Lock()
_last_request = {}


class FetchError(Exception):
    pass


class FetchTooLarge(FetchError):
    pass


@dataclass(frozen=True)
class Response:
    url: str                 # final URL after redirects
    etag: str | None
    last_modified: str | None
    content_length: int | None
    not_modified: bool = False
    data: bytes = b""


@dataclass(frozen=True)
class Download:
    url: str
    path: str | None         # None when not_modified
    sha256: str | None
    size: int
    filename: str | None
    etag: str | None
    last_modified: str | None
    not_modified: bool = False
    reused: bool = False


def sanitize_filename(name):
    """Basename only, [A-Za-z0-9._+~-] only, no leading dot."""
    base = os.path.basename(urllib.parse.unquote(str(name)).replace("\\", "/"))
    clean = _UNSAFE_NAME.sub("_", base).lstrip(".")
    return clean[:200] or "download"


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def forbidden_address(value):
    """True for loopback, private, link-local, reserved, multicast, unspecified (or unparsable) addresses."""
    try:
        ip = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified or not ip.is_global)


def forbidden_host(host):
    """True for IP literals, localhost and single-label names."""
    host = (host or "").rstrip(".").lower()
    if not host or host == "localhost" or host.endswith(".localhost") or "." not in host:
        return True
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return ":" in host
    return True


def check_url(url, allowed_hosts, allow_insecure, previous_scheme=None):
    """Raise FetchError unless url may be fetched (scheme, host, no downgrade)."""
    parts = urllib.parse.urlsplit(url)
    schemes = ("https", "http", "file") if allow_insecure else ("https",)
    if parts.scheme not in schemes:
        raise FetchError("scheme not allowed: %s" % parts.scheme)
    if previous_scheme == "https" and parts.scheme != "https":
        raise FetchError("refusing scheme downgrade to %s" % parts.scheme)
    if parts.scheme == "file":
        return
    if parts.username is not None or parts.password is not None:
        raise FetchError("credentials in URL are not allowed")
    host = (parts.hostname or "").lower()
    if not host or host not in allowed_hosts:
        raise FetchError("host not allowed: %s" % (host or "?"))
    if not allow_insecure:
        _check_strict_target(host, parts)


def _check_strict_target(host, parts):
    if forbidden_host(host):
        raise FetchError("host not allowed (address literal or local name): %s" % host)
    try:
        port = parts.port
    except ValueError as e:
        raise FetchError("bad port in URL") from e
    if port not in (None, HTTPS_PORT):
        raise FetchError("port not allowed: %s" % port)


def throttle(host, interval):
    """Sleep until at least `interval` seconds have passed since the last request to `host` (any fetcher)."""
    if not host or interval <= 0:
        return
    with _throttle_lock:
        now = time.monotonic()
        delay = max(0.0, _last_request.get(host, 0.0) + interval - now)
        _last_request[host] = now + delay
    if delay:
        time.sleep(delay)


class _Deadline:
    """Wall-clock budget for one request; a watchdog shuts the sockets down when it runs out."""

    def __init__(self, seconds):
        self._end = time.monotonic() + seconds
        self._socks = []
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def remaining(self):
        return self._end - time.monotonic()

    def timeout(self):
        left = self.remaining()
        if left <= 0:
            raise FetchError("download took too long")
        return min(CONNECT_TIMEOUT, left)

    def track(self, sock):
        self._socks.append(sock)

    def _expire(self):
        for sock in list(self._socks):
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)

    def stop(self):
        self._timer.cancel()


class _GuardedHTTPSConnection(http.client.HTTPSConnection):
    """Refuses to talk to a peer whose connected address is not a public one."""

    def __init__(self, host, deadline=None, **kw):
        super().__init__(host, **kw)
        self._deadline = deadline

    def connect(self):
        super().connect()
        try:
            peer = self.sock.getpeername()[0]
        except OSError as e:
            self.close()
            raise FetchError("cannot determine the peer address") from e
        if forbidden_address(peer):
            self.close()
            raise FetchError("refusing connection to non-public address %s" % peer)
        if self._deadline is not None:
            self._deadline.track(self.sock)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, deadline):
        super().__init__()
        self._deadline = deadline

    def https_open(self, req):
        deadline = self._deadline
        return self.do_open(lambda host, **kw: _GuardedHTTPSConnection(host, deadline=deadline, **kw), req)


class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 8

    def __init__(self, allowed_hosts, allow_insecure):
        super().__init__()
        self._allowed = allowed_hosts
        self._insecure = allow_insecure

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        absolute = urllib.parse.urljoin(req.full_url, newurl)
        if urllib.parse.urlsplit(absolute).scheme == "file":
            raise FetchError("redirect to file: is not allowed")
        previous = urllib.parse.urlsplit(req.full_url).scheme
        check_url(absolute, self._allowed, self._insecure, previous)
        return super().redirect_request(req, fp, code, msg, headers, absolute)


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class Fetcher:
    def __init__(self, cfg, allowed_hosts):
        self._cfg = cfg
        self._allowed = frozenset(h.lower() for h in allowed_hosts)

    def _opener(self, deadline):
        handlers = [urllib.request.ProxyHandler({}),
                    _GuardedRedirects(self._allowed, self._cfg.allow_insecure)]
        if not self._cfg.allow_insecure:
            handlers.append(_GuardedHTTPSHandler(deadline))
        return urllib.request.build_opener(*handlers)

    # Low level

    def _wait(self, url):
        throttle(urllib.parse.urlsplit(url).hostname, self._cfg.min_interval_seconds)

    def _request(self, url, deadline, method="GET", etag=None, last_modified=None):
        """Return an open response, or None for 304. The caller closes it."""
        check_url(url, self._allowed, self._cfg.allow_insecure)
        self._wait(url)
        headers = {"User-Agent": self._cfg.user_agent, "Accept-Encoding": "identity"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        req = urllib.request.Request(url, headers=headers, method=method)
        try:
            return self._opener(deadline).open(req, timeout=deadline.timeout())
        except urllib.error.HTTPError as e:
            e.close()
            if e.code == 304:
                return None
            raise FetchError("HTTP %d for %s" % (e.code, url)) from e
        except FetchError:
            raise
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
            if isinstance(getattr(e, "reason", None), FetchError):
                raise e.reason from e
            raise FetchError("cannot fetch %s: %s" % (url, e)) from e

    @staticmethod
    def _meta(resp):
        h = resp.headers
        return h.get("ETag"), h.get("Last-Modified"), _int_or_none(h.get("Content-Length"))

    @staticmethod
    def _check_length(length, limit):
        if length is not None and length > limit:
            raise FetchTooLarge("Content-Length %d exceeds limit %d" % (length, limit))

    @staticmethod
    def _chunks(resp, limit, deadline):
        total = 0
        read = getattr(resp, "read1", resp.read)
        try:
            while True:
                if deadline.remaining() <= 0:
                    raise FetchError("download took too long")
                chunk = read(READ_SIZE)
                if not chunk:
                    return
                total += len(chunk)
                if total > limit:
                    raise FetchTooLarge("response exceeds %d bytes" % limit)
                yield chunk
        except (OSError, http.client.HTTPException) as e:
            raise FetchError("read failed: %s" % e) from e

    # Public API

    def head(self, url):
        deadline = _Deadline(TOTAL_TIMEOUT)
        try:
            resp = self._request(url, deadline, method="HEAD")
            try:
                etag, lm, length = self._meta(resp)
                return Response(resp.geturl(), etag, lm, length)
            finally:
                resp.close()
        finally:
            deadline.stop()

    def get(self, url, max_bytes=None, etag=None, last_modified=None):
        limit = max_bytes if max_bytes is not None else self._cfg.max_index_bytes
        deadline = _Deadline(TOTAL_TIMEOUT)
        try:
            resp = self._request(url, deadline, etag=etag, last_modified=last_modified)
            if resp is None:
                return Response(url, etag, last_modified, None, not_modified=True)
            try:
                new_etag, lm, length = self._meta(resp)
                self._check_length(length, limit)
                data = b"".join(self._chunks(resp, limit, deadline))
                return Response(resp.geturl(), new_etag, lm, length, data=data)
            finally:
                resp.close()
        finally:
            deadline.stop()

    def download(self, url, dest_root, filename=None, max_bytes=None, expected_sha256=None,
                 expected_size=None, etag=None, last_modified=None):
        """Download into dest_root/<sha256>/<filename>; reuse an identical existing file.

        A known expected_size is the size cap (instead of max_deb_bytes / max_bytes).
        """
        limit = expected_size if expected_size is not None else (
            max_bytes if max_bytes is not None else self._cfg.max_deb_bytes)
        if expected_sha256 and filename:
            found = existing_download(dest_root, expected_sha256, filename)
            if found:
                return Download(url, found, expected_sha256, os.path.getsize(found),
                                sanitize_filename(filename), etag, last_modified, reused=True)
        deadline = _Deadline(TOTAL_TIMEOUT)
        try:
            resp = self._request(url, deadline, etag=etag, last_modified=last_modified)
            if resp is None:
                return Download(url, None, None, 0, None, etag, last_modified, not_modified=True)
            try:
                new_etag, lm, length = self._meta(resp)
                self._check_length(length, limit)
                final = resp.geturl()
                name = sanitize_filename(filename or urllib.parse.urlsplit(final).path)
                tmp, sha, size = self._stream(resp, dest_root, limit, deadline)
            finally:
                resp.close()
        finally:
            deadline.stop()
        return self._finish(final, dest_root, tmp, sha, size, name, new_etag, lm,
                            expected_sha256, expected_size)

    def _stream(self, resp, dest_root, limit, deadline):
        os.makedirs(dest_root, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=dest_root)
        h = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in self._chunks(resp, limit, deadline):
                    h.update(chunk)
                    size += len(chunk)
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        return tmp, h.hexdigest(), size

    @staticmethod
    def _finish(final, dest_root, tmp, sha, size, name, etag, lm, want_sha, want_size):
        try:
            if want_sha and sha != want_sha:
                raise FetchError("sha256 mismatch for %s" % final)
            if want_size is not None and size != want_size:
                raise FetchError("size mismatch for %s" % final)
            path, reused = _place(tmp, dest_root, sha, name)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        return Download(final, path, sha, size, name, etag, lm, reused=reused)


def existing_download(dest_root, sha256, filename):
    """Path of dest_root/<sha256>/<filename> if it exists and really has that hash."""
    if not SHA256_RE.match(sha256 or ""):
        return None
    path = os.path.join(dest_root, sha256, sanitize_filename(filename))
    if os.path.islink(path) or not os.path.isfile(path):
        return None
    return path if file_sha256(path) == sha256 else None


def _place(tmp, dest_root, sha, name):
    found = existing_download(dest_root, sha, name)
    if found:
        os.unlink(tmp)
        return found, True
    directory = os.path.join(dest_root, sha)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return path, False


def store_file(src, dest_root, filename):
    """Copy a local file into dest_root/<sha256>/<filename> (used by intake). Returns (path, sha, size)."""
    os.makedirs(dest_root, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=dest_root)
    h = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                h.update(chunk)
                size += len(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        path, _ = _place(tmp, dest_root, h.hexdigest(), sanitize_filename(filename))
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return path, h.hexdigest(), size
