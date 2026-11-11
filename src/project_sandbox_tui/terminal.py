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
import signal
import subprocess
import sys
import termios
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
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
def preserved(fd: int | None = None) -> Generator[TerminalState | None, None, None]:
    """Restore the terminal state on exit, however the block ends."""
    state = save(fd)
    try:
        yield state
    finally:
        restore(state)


def _foreground(fd: int, group: int) -> None:
    """Give a process group the tty, including when we are in the background."""
    previous = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        os.tcsetpgrp(fd, group)
    finally:
        signal.signal(signal.SIGTTOU, previous)


def run_foreground(argv: list[str], *, cwd: Path) -> int:
    """Run a viewer with sole ownership of terminal input and terminal signals.

    Keep jj and its pager in their own foreground process group so Ctrl+C
    cannot cancel the TUI's asyncio task. Reclaim the tty and stop any surviving
    descendants before restoring terminal modes and resuming Textual.
    """
    with preserved() as state:
        previous_group = os.tcgetpgrp(state.fd) if state else None
        # jj reports cancellation as exit code 1, also used for real errors.
        # A small supervisor in the viewer's group records SIGINT and reports
        # 130, keeping cancelled views quiet without hiding command failures.
        command = [sys.executable, str(Path(__file__).resolve()), *argv]
        with subprocess.Popen(command, cwd=cwd, process_group=0) as proc:
            try:
                if state is not None:
                    _foreground(state.fd, proc.pid)
                    # A fast child can try reading before tcsetpgrp and stop
                    # on SIGTTIN. It is safe to resume once it owns the tty.
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGCONT)
                while True:
                    try:
                        _, status = os.waitpid(proc.pid, os.WUNTRACED)
                    except KeyboardInterrupt:
                        # Also support SIGINT sent directly to the TUI process.
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(proc.pid, signal.SIGINT)
                        continue
                    if os.WIFSTOPPED(status):
                        # Ctrl+Z must not leave the app waiting on a stopped
                        # viewer with no shell available to foreground it.
                        return 130
                    proc.returncode = os.waitstatus_to_exitcode(status)
                    return proc.returncode
            finally:
                # jj may exit on SIGINT before its pager does. No process from
                # this viewing may keep reading or change modes after we resume.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # macOS reports EPERM for a group containing only zombies.
                    if sys.platform != "darwin":
                        raise
                if state is not None and previous_group is not None:
                    _foreground(state.fd, previous_group)


def _run_viewer(argv: list[str]) -> int:
    """Supervise the viewer in its foreground group (stdlib-only entry point)."""
    interrupted = False

    def on_interrupt(_signum: int, _frame: Any) -> None:
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, on_interrupt)
    try:
        with subprocess.Popen(argv) as proc:
            code = proc.wait()
    except OSError as exc:
        print(f"Could not start diff viewer: {exc}", file=sys.stderr, flush=True)
        return 127
    return 130 if interrupted else code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(_run_viewer(sys.argv[1:]))
