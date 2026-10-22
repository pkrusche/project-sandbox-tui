from __future__ import annotations

import contextlib
import fcntl
import os
import termios
import tty
from pathlib import Path

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
