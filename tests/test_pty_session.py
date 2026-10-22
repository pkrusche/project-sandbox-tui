"""Interactive sessions in a real pseudo-terminal (App.suspend cannot run headless).

Regression for "the terminal hangs after closing an interactive session": the
TUI must take keys again afterwards, however the agent left the terminal.
"""

from __future__ import annotations

import fcntl
import os
import select
import struct
import sys
import termios
import time
from pathlib import Path

import pytest

pty = pytest.importorskip("pty")

AGENT = Path(__file__).with_name("fake_interactive_agent.py")
SRC = Path(__file__).resolve().parents[1] / "src"


class Tui:
    def __init__(self, repo: Path, env: dict[str, str]) -> None:
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # child: run the real TUI on the pty
            os.chdir(repo)
            os.execve(sys.executable, [sys.executable, "-m", "project_sandbox_tui"], env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
        self.buf = bytearray()

    def pump(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.05)
            if ready:
                try:
                    self.buf.extend(os.read(self.fd, 65536))
                except OSError:
                    return

    def wait_for(self, needle: bytes, seconds: float = 10) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if needle in self.buf:
                return True
            self.pump(0.1)
        return False

    def send(self, data: bytes) -> None:
        os.write(self.fd, data)

    def close(self) -> None:
        try:
            os.kill(self.pid, 9)
            os.waitpid(self.pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
        os.close(self.fd)


def start_tui(repo: Path, command: str, **env_extra: str) -> Tui:
    (repo / ".project-sandbox-tui.toml").write_text(
        f'[project]\nbase_image = "img"\n[sandbox]\ncommand = "{command}"\n'
        '[ui]\nnotify_command = "true"\nbell = false\n'
    )
    pythonpath = os.pathsep.join(p for p in (str(SRC), os.environ.get("PYTHONPATH", "")) if p)
    env = {**os.environ, "TERM": "xterm-256color", "PYTHONPATH": pythonpath, **env_extra}
    tui = Tui(repo, env)
    try:
        assert tui.wait_for(b"unsupervised", 20), "TUI did not start"
        tui.send(b"?")
        assert tui.wait_for(b"this help"), "TUI does not take keys before the session"
        tui.send(b"x")
        tui.pump(0.5)
        return tui
    except BaseException:
        tui.close()
        raise


def assert_responsive(tui: Tui) -> None:
    tui.pump(1.0)
    tui.buf.clear()
    tui.send(b"?")
    assert tui.wait_for(b"this help", 5), "TUI does not take keys after the session"


@pytest.mark.parametrize(
    "mode",
    ["clean", "raw", "nonblock"],
)
@pytest.mark.parametrize("exit_code", ["0", "3"])
def test_tui_takes_keys_after_interactive_session(repo: Path, mode: str, exit_code: str) -> None:
    tui = start_tui(repo, f"{sys.executable} {AGENT}", FAKE_MODE=mode, FAKE_EXIT=exit_code)
    try:
        tui.send(b"i")
        tui.pump(1.0)
        tui.send(b"\r")  # launch from the form
        assert tui.wait_for(b"FAKE-AGENT-READY"), "agent did not start"
        tui.send(b"q")
        assert tui.wait_for(b"FAKE-AGENT-EXIT", 5)
        if exit_code != "0":
            # the prompt must work even if the agent left the terminal raw
            assert tui.wait_for(b"press Enter", 5)
            tui.send(b"\r")
        assert_responsive(tui)
    finally:
        tui.close()


def test_tui_recovers_when_sandbox_command_is_missing(repo: Path, tmp_path: Path) -> None:
    tui = start_tui(repo, str(tmp_path / "no-such-project-sandbox"))
    try:
        tui.send(b"i")
        tui.pump(1.0)
        tui.send(b"\r")
        assert tui.wait_for(b"could not start project-sandbox", 10)
        assert_responsive(tui)
    finally:
        tui.close()
