# SPDX-License-Identifier: GPL-3.0-or-later
"""Kicks: tell the next stage that there is work now, instead of letting it find out on its next timer run.

The AI intake and the build tasks pass through several stages (intake-step, build-step, the VM runner), each one
started by its own once-a-minute timer, so every hand-over used to cost up to a minute. A stage that has done
something writes a byte into a file of the kick directory; a systemd path unit watches that file and starts the
next stage at once. The timers stay as the fallback: a kick that is lost (the next stage was busy, the directory
is missing) only makes the hand-over as slow as it was before.

Names: "build" (build-step: fetch sources, collect results), "intake" (intake-step), "queue" (reports-ingest and
sandbox-queue: a candidate wants its sandbox check, or a report is waiting) and "sandbox" (the sandbox runner: the
queue has a new job).
"""

import os
import sys

NAMES = ("build", "intake", "queue", "sandbox")


def kick(cfg, name):
    """Best effort; never raises. Nothing happens when [paths] kick is not configured or the directory is missing."""
    directory = getattr(cfg, "kick_dir", None)
    if not directory or name not in NAMES or not os.path.isdir(directory):
        return
    try:
        fd = os.open(os.path.join(directory, name), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o660)
        try:
            os.write(fd, b"1")
        finally:
            os.close(fd)
    except OSError as e:
        print("warning: cannot kick %s: %s (the timer will do it)" % (name, e.strerror), file=sys.stderr)
