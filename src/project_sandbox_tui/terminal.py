"""Save and restore the controlling terminal around an interactive child.

An interactive agent runs ``container run -it`` / ``docker run -it``, which puts
the terminal into raw mode. If that process crashes or is killed it can leave
the terminal raw (Enter sends ``\\r``, so a line-based prompt never returns) or
leave O_NONBLOCK set on the shared file description. Textual would then record
that broken state as "the terminal before the app" and hand it back to the
user's shell on exit. Restoring what we saw before the child started avoids
both.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import sys
import termios
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any


@dataclass
class TerminalState:
    fd: int
    attrs: list[Any] | None
    flags: int | None


def save(fd: int | None = None) -> TerminalState | None:
    """Snapshot termios attributes and file status flags, or None if not a tty."""
    if fd is None:
        try:
            fd = sys.stdin.fileno()
        except (AttributeError, OSError, ValueError):
            return None
    if not os.isatty(fd):
        return None
    try:
        attrs: list[Any] | None = termios.tcgetattr(fd)
    except termios.error:
        attrs = None
    try:
        flags: int | None = fcntl.fcntl(fd, fcntl.F_GETFL)
    except OSError:
        flags = None
    return TerminalState(fd, attrs, flags)


def restore(state: TerminalState | None) -> None:
    """Put the terminal back the way :func:`save` found it (best effort)."""
    if state is None:
        return
    if state.flags is not None:
        with contextlib.suppress(OSError):
            fcntl.fcntl(state.fd, fcntl.F_SETFL, state.flags)
    if state.attrs is not None:
        with contextlib.suppress(termios.error):
            # TCSAFLUSH also drops keystrokes typed into the dying child.
            termios.tcsetattr(state.fd, termios.TCSAFLUSH, state.attrs)


@contextlib.contextmanager
def preserved(fd: int | None = None) -> Iterator[TerminalState | None]:
    """Restore the terminal state on exit, however the block ends."""
    state = save(fd)
    try:
        yield state
    finally:
        restore(state)
