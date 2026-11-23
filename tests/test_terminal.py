from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import sys
import termios
import tty
from pathlib import Path
from typing import Any

import pytest

from project_sandbox_tui import terminal

pty = pytest.importorskip("pty")


@pytest.mark.parametrize("raises", [False, True], ids=["normal-exit", "exception"])
def test_restores_raw_mode_and_nonblocking_flag(raises: bool) -> None:
    master, slave = pty.openpty()
    try:
        before = termios.tcgetattr(slave)
        flags = fcntl.fcntl(slave, fcntl.F_GETFL)
        expected = pytest.raises(RuntimeError) if raises else contextlib.nullcontext()
        with expected, terminal.preserved(slave) as state:
            assert state is not None
            tty.setraw(slave)  # what a crashed `container run -it` leaves behind
            fcntl.fcntl(slave, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            if raises:
                raise RuntimeError("agent crashed")
        assert termios.tcgetattr(slave) == before
        assert fcntl.fcntl(slave, fcntl.F_GETFL) == flags
    finally:
        os.close(master)
        os.close(slave)


def test_not_a_tty_is_a_no_op(tmp_path: Path) -> None:
    with (tmp_path / "f").open("w") as fh:
        assert terminal.save(fh.fileno()) is None
        terminal.restore(None)


def test_restore_closed_terminal_is_best_effort() -> None:
    master, slave = pty.openpty()
    try:
        state = terminal.save(slave)
        assert state is not None
    finally:
        os.close(master)
        os.close(slave)
    terminal.restore(state)


class InterruptedTui(Exception):
    pass


@pytest.mark.parametrize("child_dies", [False, True], ids=["child-exits", "child-interrupted"])
def test_run_shared_waits_for_child_after_ctrl_c(tmp_path: Path, child_dies: bool) -> None:
    """Ctrl+C right after the launch, possibly before the wait, still waits for the child."""
    script = (
        "import os, signal, sys, time\n"
        # SIG_IGN would survive exec and keep Ctrl+C from the child.
        "assert signal.getsignal(signal.SIGINT) is signal.default_int_handler\n"
        "os.kill(os.getppid(), signal.SIGINT)\n"
        + ("os.kill(os.getpid(), signal.SIGINT)\n" if child_dies else "")
        + "time.sleep(0.2)\n"
        "sys.exit(3)\n"
    )

    def interrupted(_signum: int, _frame: Any) -> None:
        raise InterruptedTui  # not KeyboardInterrupt, which would stop pytest

    previous = signal.signal(signal.SIGINT, interrupted)
    try:
        code = terminal.run_shared([sys.executable, "-c", script], cwd=tmp_path)
        assert signal.getsignal(signal.SIGINT) is interrupted
    finally:
        signal.signal(signal.SIGINT, previous)
    assert code == (-signal.SIGINT if child_dies else 3)


def test_run_shared_restores_handler_when_command_is_missing(tmp_path: Path) -> None:
    previous = signal.getsignal(signal.SIGINT)
    with pytest.raises(OSError):
        terminal.run_shared([str(tmp_path / "missing")], cwd=tmp_path)
    assert signal.getsignal(signal.SIGINT) is previous
