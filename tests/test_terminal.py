from __future__ import annotations

import fcntl
import os
import termios
import tty

import pytest

from project_sandbox_tui import terminal

pty = pytest.importorskip("pty")


def test_restores_raw_mode_and_nonblocking_flag() -> None:
    master, slave = pty.openpty()
    try:
        before = termios.tcgetattr(slave)
        flags = fcntl.fcntl(slave, fcntl.F_GETFL)
        with terminal.preserved(slave) as state:
            assert state is not None
            tty.setraw(slave)  # what a crashed `container run -it` leaves behind
            fcntl.fcntl(slave, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        assert termios.tcgetattr(slave) == before
        assert fcntl.fcntl(slave, fcntl.F_GETFL) == flags
    finally:
        os.close(master)
        os.close(slave)


def test_restores_even_when_the_block_raises() -> None:
    master, slave = pty.openpty()
    try:
        before = termios.tcgetattr(slave)
        with pytest.raises(RuntimeError), terminal.preserved(slave):
            tty.setraw(slave)
            raise RuntimeError("agent crashed")
        assert termios.tcgetattr(slave) == before
    finally:
        os.close(master)
        os.close(slave)


def test_not_a_tty_is_a_no_op(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with (tmp_path / "f").open("w") as fh:
        assert terminal.save(fh.fileno()) is None
        terminal.restore(None)
