# SPDX-License-Identifier: GPL-3.0-or-later
"""Command line side of AI source intake: intake-submit, intake-cancel, intake-repair, intake-adopt, intake-step
(docs/server.md section 15). Exit codes as everywhere: 0 done, 1 refused or something needs a person, 2 bad
arguments (IntakeUsage), 3 lock busy."""

import contextlib
import re
import time

from . import content, intake, intake_adopt, intake_step
from .state import StateLocked

EXIT_OK, EXIT_PROBLEM = 0, 1
CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _clean(value):
    return CONTROL_RE.sub("?", str(value))


def cmd_submit(cfg, st, args):
    with st.lock():
        record = intake.submit(cfg, st, args.address, args.hint, args.agent, args.allow_host, args.by)
    print(record["id"])
    return EXIT_OK


def cmd_cancel(cfg, st, args):
    with st.lock():
        record = intake.cancel(cfg, st, args.id, args.by, reject=args.reject)
    print("%s %s" % (record["status"], record["id"]))
    return EXIT_OK


def cmd_repair(cfg, st, args):
    with st.lock():
        loaded = content.load(cfg.content, allow_insecure=cfg.allow_insecure, policy=cfg.build_policy())
        record = intake.create_repair(cfg, st, loaded, args.app, args.version, args.by)
    print(record["id"])
    return EXIT_OK


def cmd_adopt(cfg, st, args):
    with st.lock():
        lines, ok = intake_adopt.adopt(cfg, st, args.id, args.by, auto_task=args.auto_task)
    for line in lines:
        print(_clean(line))
    return EXIT_OK if ok else EXIT_PROBLEM


def cmd_step(cfg, st, args):
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(st.lock("intake-step"))
        except StateLocked:
            print("intake-step is already running")
            return EXIT_OK
        report = intake_step.step(cfg, st, time.time())
    for line in report.lines:
        print(_clean(line))
    return EXIT_PROBLEM if report.failed else EXIT_OK
