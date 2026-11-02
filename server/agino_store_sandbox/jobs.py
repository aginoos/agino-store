# SPDX-License-Identifier: GPL-3.0-or-later
"""Queue jobs (written by the server) and per-job state (written by this runner)."""

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import stat

from . import profile, shots_scene
from .util import DESKTOP_RE, ID_RE, SHA256_RE, VERSION_RE, SandboxError, now

JOB_RE = re.compile(r"\A[a-z0-9][a-z0-9-]*_[0-9a-f]{12}\.json\Z")
MAX_JSON = 1 << 20
MAX_DEB = 4 << 30
MAX_DEMO = 256 << 20
MAX_ATTEMPTS = 3
MODES = ("check", "shots")
PACKAGE_RE = re.compile(r"\A[a-z0-9][a-z0-9+.-]+\Z")


class JobError(SandboxError):
    pass


@dataclasses.dataclass(frozen=True)
class Job:
    stem: str
    app: str
    version: str
    sha256: str
    size: int
    package: str
    launchable: object
    debconf: list
    default_files: list
    deb_path: str
    shots: object = None  # normalised scenes (shots_scene.validate) or None
    shots_error: object = None
    demo_path: object = None
    demo_size: int = 0
    demo_sha256: object = None
    mode: str = "check"  # "check" (sandbox checks) or "shots" (screenshots only)
    source: str = "deb"  # "deb" or "milkyway" (shots jobs only)


def _regular(path, limit):
    try:
        st = os.lstat(path)
    except OSError as e:
        raise JobError("cannot stat %s: %s" % (os.path.basename(path), e.strerror)) from None
    if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
        raise JobError("%s is not a regular file of acceptable size" % os.path.basename(path))
    return st.st_size


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_job(queue, name):
    """Validated Job from queue/<name>; JobError for anything malformed or a deb that does not match."""
    if not JOB_RE.match(name):
        raise JobError("not a job file name")
    stem = name[:-5]
    path = os.path.join(queue, name)
    _regular(path, MAX_JSON)
    try:
        with open(path, "rb") as f:
            doc = json.loads(f.read(MAX_JSON).decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        raise JobError("job file is not valid JSON") from None
    if not isinstance(doc, dict) or doc.get("schema") != 1:
        raise JobError("unsupported job schema")
    mode = doc.get("mode", "check")
    if mode not in MODES:
        raise JobError("unknown job mode")
    if mode == "shots" and doc.get("source") == "milkyway":
        return _read_milkyway(doc, stem, queue)
    if mode == "check" and "source" in doc:
        raise JobError("source is only valid for shots jobs")
    if mode == "shots" and doc.get("source") != "deb":
        raise JobError("bad shots source")
    app, version, sha, size = doc.get("app"), doc.get("version"), doc.get("sha256"), doc.get("size")
    package, launchable = doc.get("package"), doc.get("launchable")
    if not (isinstance(app, str) and ID_RE.match(app) and isinstance(version, str) and VERSION_RE.match(version)
            and isinstance(sha, str) and SHA256_RE.match(sha) and isinstance(package, str) and ID_RE.match(package)):
        raise JobError("bad job fields")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_DEB:
        raise JobError("bad job size")
    launchable = _launchable(launchable)
    if doc.get("deb") != stem + ".deb":
        raise JobError("deb name does not match the job")
    try:
        debconf, files = profile.validate(doc.get("install_profile"))
    except profile.ProfileError as e:
        raise JobError(str(e)) from None
    deb = os.path.join(queue, stem + ".deb")
    if _regular(deb, MAX_DEB) != size:
        raise JobError("deb size differs from the job")
    if file_sha256(deb) != sha:
        raise JobError("deb sha256 differs from the job")
    job = Job(stem, app, version, sha, size, package, launchable, debconf, files, deb, mode=mode,
              source="deb")
    job = _with_shots(job, doc, queue)
    if mode == "shots" and job.shots is None:
        raise JobError("shots job without usable shots: %s" % (job.shots_error or "missing"))
    return job


def _launchable(value):
    """Desktop file id of the job (None: the runner takes the package's first desktop file)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise JobError("bad launchable")
    value = value if value.endswith(".desktop") else value + ".desktop"
    if not DESKTOP_RE.match(value):
        raise JobError("bad launchable")
    return value


def milkyway_stem(app, package, version):
    h = hashlib.sha256(("milkyway:%s=%s" % (package, version)).encode("utf-8")).hexdigest()[:12]
    return "%s_%s" % (app, h)


def _read_milkyway(doc, stem, queue):
    """A shots job for a package from the milkyway repository: no deb, no hash, install by name."""
    app, version, package = doc.get("app"), doc.get("version"), doc.get("package")
    if not (isinstance(app, str) and ID_RE.match(app) and isinstance(version, str) and VERSION_RE.match(version)
            and isinstance(package, str) and PACKAGE_RE.match(package)):
        raise JobError("bad job fields")
    if doc.get("sha256") is not None or doc.get("deb") is not None or doc.get("size") is not None:
        raise JobError("a milkyway job has no deb")
    if stem != milkyway_stem(app, package, version):
        raise JobError("job name does not match package and version")
    job = Job(stem, app, version, None, 0, package, _launchable(doc.get("launchable")), [], [], None, mode="shots",
              source="milkyway")
    job = _with_shots(job, doc, queue)
    if job.shots is None:
        raise JobError("shots job without usable shots: %s" % (job.shots_error or "missing"))
    return job


def _with_shots(job, doc, queue):
    """Attach the screenshot scenes; problems here only switch screenshots off, never the sandbox run."""
    if doc.get("shots") is None:
        return job
    try:
        shots = shots_scene.validate(doc["shots"])
        demo_path, demo_size, demo_sha = None, 0, None
        if any(sc["demo"] for sc in shots["scenes"]):
            if doc.get("demo") != job.stem + ".demo.tar":
                raise shots_scene.ShotsError("demo tar name does not match the job")
            demo_path = os.path.join(queue, job.stem + ".demo.tar")
            demo_size = _regular(demo_path, MAX_DEMO)
            demo_sha = file_sha256(demo_path)
    except (shots_scene.ShotsError, JobError) as e:
        return dataclasses.replace(job, shots_error=str(e)[:200])
    return dataclasses.replace(job, shots=shots, demo_path=demo_path, demo_size=demo_size, demo_sha256=demo_sha)


def stem_of(name):
    return name[:-5] if name.endswith(".json") else name


def state_key(stem, mode):
    """A shots job may share its queue name with an earlier check job; keep their states apart."""
    return stem + ".shots" if mode == "shots" else stem


def job_mode(queue, name):
    """The job's mode from its queue JSON ("check" when unreadable: the run records it as invalid)."""
    try:
        with open(os.path.join(queue, name), "rb") as f:
            doc = json.loads(f.read(MAX_JSON).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return "check"
    mode = doc.get("mode", "check") if isinstance(doc, dict) else "check"
    return mode if mode in MODES else "check"


def read_state(state_dir, stem):
    path = os.path.join(state_dir, stem + ".json")
    try:
        with open(path, "rb") as f:
            doc = json.loads(f.read(MAX_JSON).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def write_state(state_dir, stem, doc):
    os.makedirs(state_dir, mode=0o755, exist_ok=True)
    final = os.path.join(state_dir, stem + ".json")
    tmp = os.path.join(state_dir, ".%s.tmp" % stem)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dict(doc, updated=now()), f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, final)


def pending(queue, state_dir, only=None):
    """Job file names that need a run: no state, or an earlier infrastructure error with attempts left."""
    try:
        names = sorted(n for n in os.listdir(queue) if JOB_RE.match(n))
    except OSError as e:
        raise SandboxError("cannot list queue: %s" % e.strerror) from None
    if only is not None:
        only = only if only.endswith(".json") else only + ".json"
        return [only] if only in names else []
    out = []
    for name in names:
        state = read_state(state_dir, state_key(stem_of(name), job_mode(queue, name)))
        if state is None or (state.get("status") == "error" and int(state.get("attempts", 0)) < MAX_ATTEMPTS):
            out.append(name)
    return out


def previous_attempts(state_dir, stem):
    state = read_state(state_dir, stem)
    if not state or state.get("status") != "error":
        return 0
    with contextlib.suppress(TypeError, ValueError):
        return max(0, int(state.get("attempts", 0)))
    return 0
