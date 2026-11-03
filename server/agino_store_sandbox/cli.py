# SPDX-License-Identifier: GPL-3.0-or-later
"""Command line of agino-store-sandbox."""

import argparse
import dataclasses
import sys

from . import config, runner, session_serve, shots_job
from .util import SandboxError


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agino-store-sandbox")
    parser.add_argument("--config", default=config.DEFAULT_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run all queue jobs that have no result")
    run.add_argument("--only", metavar="NAME", help="run (or rerun) just this job")
    run.add_argument("--slots", type=int, help="parallel VMs (1..%d)" % config.MAX_SLOTS)
    sub.add_parser("check-base", help="boot the base image and check the session")
    reset = sub.add_parser("shots-reset", help="keep a shots job's output as NAME.prev and forget its state, "
                                               "so a new shots-request takes the screenshots again")
    reset.add_argument("name", metavar="NAME", help="job name, e.g. gimp_3c256705b88b")
    sess = sub.add_parser("session", help="interactive screenshot sessions (docs/sandbox.md section 9)")
    ssub = sess.add_subparsers(dest="session_command", required=True)
    serve = ssub.add_parser("serve", help="run one session as a daemon (started by agino-store-session@APP.service)")
    serve.add_argument("app", metavar="APP")
    serve.add_argument("--lang", default="zh_CN", choices=("zh_CN", "en_US"))
    ssub.add_parser("status", help="print the status JSON of a running session").add_argument("app", metavar="APP")
    ssub.add_parser("cleanup", help="remove captures directories older than retention_hours")
    args = parser.parse_args(argv)
    try:
        cfg = config.load(args.config)
        if args.command == "run":
            if args.slots is not None:
                if not 1 <= args.slots <= config.MAX_SLOTS:
                    raise SandboxError("--slots must be between 1 and %d" % config.MAX_SLOTS)
                cfg = dataclasses.replace(cfg, slots=args.slots)
            results = runner.run(cfg, args.only)
            if not results:
                print("nothing to do")
            return 1 if any(s.get("status") == "error" for s in results.values()) else 0
        if args.command == "session":
            if args.session_command == "serve":
                return session_serve.serve(cfg, args.app, args.lang)
            if args.session_command == "status":
                print(session_serve.status(cfg, args.app))
                return 0
            for name in session_serve.cleanup(cfg):
                print("removed %s" % name)
            return 0
        if args.command == "shots-reset":
            print(shots_job.reset(cfg, args.name))
            return 0
        ok, message = runner.check_base(cfg)
        print(("OK: " if ok else "FAIL: ") + message)
        return 0 if ok else 1
    except SandboxError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
