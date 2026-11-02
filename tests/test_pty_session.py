"""Interactive sessions in a real pseudo-terminal (App.suspend cannot run headless).

Regression for "the terminal hangs after closing an interactive session": the
TUI must take keys again afterwards, however the agent left the terminal.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import select
import shlex
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from project_sandbox_tui import config
from tests.conftest import jj

pty = pytest.importorskip("pty")

AGENT = Path(__file__).with_name("fake_interactive_agent.py")
SRC = Path(__file__).resolve().parents[1] / "src"


class Tui:
    def __init__(self, repo: Path, env: dict[str, str]) -> None:
        # xdist exports the runner's COLUMNS/LINES. Let the TUI use this PTY's
        # dimensions instead, or its small-window layout hides the startup marker.
        env = {key: value for key, value in env.items() if key not in ("COLUMNS", "LINES")}
        self.fd, slave = pty.openpty()
        try:
            self.resize(110, 30)
            # pytest/xdist already has threads: never run Python between fork
            # and exec in that process. A fresh interpreter safely acquires the
            # controlling terminal before replacing itself with the real TUI.
            self.proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import os, sys; os.login_tty(int(sys.argv[1])); "
                    "os.execv(sys.executable, [sys.executable, '-m', 'project_sandbox_tui'])",
                    str(slave),
                ],
                cwd=repo,
                env=env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                pass_fds=(slave,),
            )
        except BaseException:
            os.close(self.fd)
            raise
        finally:
            os.close(slave)
        self.buf = bytearray()

    def resize(self, columns: int, rows: int) -> None:
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

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
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)  # also stop an interrupted test's agent
            self.proc.kill()  # also works if startup failed before login_tty created a session
        finally:
            # Hang up before waiting. On macOS an exiting session leader waits in
            # the kernel for its terminal's output to drain, and SIGKILL cannot
            # interrupt that. If nobody reads the master, it never exits.
            os.close(self.fd)
        self.proc.wait(timeout=5)


def start_tui(repo: Path, command: str, *, host_agent_command: str = "", **env_extra: str) -> Tui:
    (repo / ".project-sandbox-tui.toml").write_text(
        f'[project]\nbase_image = "img"\n[sandbox]\ncommand = "{command}"\n'
        '[ui]\nnotify_command = "true"\nbell = false\n'
        f"host_agent_command = {json.dumps(host_agent_command)}\n"
    )
    config.ProjectTrust.check(repo).trust(repo)
    pythonpath = os.pathsep.join(p for p in (str(SRC), os.environ.get("PYTHONPATH", "")) if p)
    env = {**os.environ, "TERM": "xterm-256color", "PYTHONPATH": pythonpath, **env_extra}
    tui = Tui(repo, env)
    try:
        assert tui.wait_for(b"unsupervised", 20), (
            f"TUI did not show its startup marker:\n{tui.buf.decode(errors='replace')}"
        )
        tui.send(b"?")
        assert tui.wait_for(b"this help"), "TUI does not take keys before the session"
        tui.send(b"q")  # closes help
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
@pytest.mark.parametrize("host", [False, True], ids=["sandbox", "host"])
def test_tui_takes_keys_after_interactive_session(
    repo: Path, mode: str, exit_code: str, host: bool
) -> None:
    from project_sandbox_tui.vcs import workspace_path

    command = shlex.join([sys.executable, str(AGENT)])
    tui = start_tui(
        repo,
        command,
        host_agent_command=command if host else "",
        FAKE_MODE=mode,
        FAKE_EXIT=exit_code,
        FAKE_EXPECT_CWD=str(workspace_path(repo, "feature/x", None)) if host else "",
    )
    try:
        tui.send(b"jI" if host else b"ji")
        if not host:
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


def test_tui_responds_to_pty_resize(repo: Path) -> None:
    tui = start_tui(repo, "true")
    try:
        tui.buf.clear()
        tui.resize(80, 24)
        assert tui.wait_for(b"full help"), "small-terminal footer did not appear after resize"
        tui.buf.clear()
        tui.resize(110, 30)
        assert tui.wait_for(b"unsupervised"), "cheat-sheet did not return after resize"
        assert_responsive(tui)
    finally:
        tui.close()


def test_tui_recovers_when_sandbox_command_is_missing(repo: Path, tmp_path: Path) -> None:
    tui = start_tui(repo, str(tmp_path / "no-such-project-sandbox"))
    try:
        tui.send(b"ji")
        tui.pump(1.0)
        tui.send(b"\r")
        assert tui.wait_for(b"could not start project-sandbox", 10)
        assert_responsive(tui)
    finally:
        tui.close()


@pytest.mark.parametrize("launch", ["shell", "interactive", "host"])
@pytest.mark.parametrize("interrupt_at", ["child", "prompt"])
def test_tui_survives_ctrl_c(repo: Path, launch: str, interrupt_at: str) -> None:
    script = repo.parent / "interruptible.py"
    script.write_text(
        "import sys, time\n"
        "print('INTERRUPTIBLE-READY', flush=True)\n"
        + ("time.sleep(60)\n" if interrupt_at == "child" else "")
        + "sys.exit(3)\n"
    )
    command = shlex.join([sys.executable, str(script)])
    tui = start_tui(repo, command, host_agent_command=command)
    try:
        for _ in range(2):  # a previous interrupt must not poison the next session
            tui.buf.clear()
            if launch == "shell":
                tui.send(b":")
                tui.pump(0.5)  # let the command input mount before typing
                tui.send(f"!exec {command}".encode())
            elif launch == "interactive":
                tui.send(b"ggji")
            else:
                tui.send(b"ggjI")
            if launch != "host":
                tui.pump(0.5)
                tui.send(b"\r")
            assert tui.wait_for(b"INTERRUPTIBLE-READY"), tui.buf.decode(errors="replace")
            if interrupt_at == "child":
                tui.send(b"\x03")
            assert tui.wait_for(b"press Enter to return to sbx-tui")
            tui.send(b"\r" if interrupt_at == "child" else b"\x03")
            assert_responsive(tui)
            assert tui.proc.poll() is None
            tui.send(b"q")  # close help before the next session
            tui.pump(0.5)
    finally:
        tui.close()


@pytest.mark.parametrize("viewer", ["pager", "formatter"])
def test_diff_uses_host_viewer_and_returns_to_tui(repo: Path, viewer: str) -> None:
    script = repo.parent / "diff_viewer.py"
    script.write_text(
        "import pathlib, sys\n"
        "if len(sys.argv) == 1:\n"
        "    assert sys.stdout.isatty()\n"
        "    diff = sys.stdin.read()\n"
        "    assert 'diff --git' in diff and '+b' in diff\n"
        "else:\n"
        "    assert not (pathlib.Path(sys.argv[1]) / 'b.txt').exists()\n"
        "    assert (pathlib.Path(sys.argv[2]) / 'b.txt').read_text() == 'b\\n'\n"
        "print('HOST-DIFF-VIEWER-READY', flush=True)\n"
        # A pager reads jj's output on stdin; use the controlling tty for keys.
        "with open('/dev/tty') as tty:\n"
        "    tty.readline()\n"
    )
    command = [sys.executable, str(script)]
    jj(repo, "config", "set", "--user", "ui.color", "never")
    if viewer == "pager":
        jj(repo, "config", "set", "--user", "ui.diff-formatter", ":git")
        jj(repo, "config", "set", "--user", "ui.pager", json.dumps(command))
    else:
        jj(repo, "config", "set", "--user", "ui.paginate", "never")
        jj(
            repo,
            "config",
            "set",
            "--user",
            "ui.diff-formatter",
            json.dumps([*command, "$left", "$right"]),
        )
    tui = start_tui(repo, "true")
    try:
        tui.send(b"j=")
        assert tui.wait_for(b"0/1 seen"), tui.buf.decode(errors="replace")
        for _ in range(2):  # each viewing starts on a clear screen
            tui.buf.clear()
            tui.send(b"\r")
            assert tui.wait_for(b"HOST-DIFF-VIEWER-READY"), tui.buf.decode(errors="replace")
            assert b"\x1b[2J\x1b[H" + "── sbx-tui: diff".encode() in tui.buf
            tui.send(b"\r")
            assert tui.wait_for(b"press Enter to return to sbx-tui")
            tui.buf.clear()
            tui.send(b"\r")
            assert tui.wait_for(b"0/1 seen"), "the file list did not come back"
        tui.send(b" ")
        assert tui.wait_for(b"1/1 seen"), "the file list does not take keys"
        tui.send(b"q")  # back to the bookmark list
        assert_responsive(tui)
    finally:
        tui.close()
