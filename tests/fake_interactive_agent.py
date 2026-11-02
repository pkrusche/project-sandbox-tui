#!/usr/bin/env python3
"""Stand-in for an interactive ``project-sandbox --agent ...`` run (see test_pty_session).

Puts the terminal into raw mode like ``container run -it`` and reads keys until
``q``, then exits with ``FAKE_EXIT``. ``FAKE_MODE`` controls how it leaves the
terminal: ``clean`` restores it, ``raw`` leaves it raw, ``nonblock`` also leaves
O_NONBLOCK set — what a crashed container CLI can do.
"""

from __future__ import annotations

import fcntl
import os
import select
import sys
import termios
import tty


def main() -> int:
    if expected_cwd := os.environ.get("FAKE_EXPECT_CWD"):
        assert os.getcwd() == expected_cwd, (os.getcwd(), expected_cwd)
    mode = os.environ.get("FAKE_MODE", "clean")
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setraw(fd)
    if mode == "nonblock":
        fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_NONBLOCK)
    os.write(1, b"FAKE-AGENT-READY\r\n")
    while True:
        try:
            key = os.read(fd, 1)
        except BlockingIOError:
            select.select([fd], [], [])
            continue
        if key in (b"q", b""):
            break
    os.write(1, b"FAKE-AGENT-EXIT\r\n")
    if mode == "clean":
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    return int(os.environ.get("FAKE_EXIT", "0"))


if __name__ == "__main__":
    raise SystemExit(main())
