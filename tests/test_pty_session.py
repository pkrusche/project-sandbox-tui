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
        self.viewer_groups: set[int] = set()

    def resize(self, columns: int, rows: int) -> None:
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

    def pump(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.05)
            if ready:
                with contextlib.suppress(OSError):
                    foreground = os.tcgetpgrp(self.fd)
                    if foreground > 0 and foreground != self.proc.pid:
                        self.viewer_groups.add(foreground)
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
            with contextlib.suppress(OSError):
                foreground = os.tcgetpgrp(self.fd)
                if foreground > 0 and foreground != self.proc.pid:
                    self.viewer_groups.add(foreground)
            for group in self.viewer_groups:
                with contextlib.suppress(OSError):
                    os.killpg(group, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)  # also stop an interrupted test's agent
            self.proc.kill()  # also works if startup failed before login_tty created a session
        finally:
            # Hang up before waiting. On macOS an exiting session leader waits in
            # the kernel for its terminal's output to drain, and SIGKILL cannot
            # interrupt that. If nobody reads the master, it never exits.
            os.close(self.fd)
        self.proc.wait(timeout=5)
        # Linux's test subreaper adopts viewers orphaned by an interrupted jj.
        # Their process groups differ from the TUI's, so reap them explicitly.
        for group in self.viewer_groups:
            deadline = time.monotonic() + 5
            while True:
                try:
                    pid, _ = os.waitpid(-group, os.WNOHANG)
                except ChildProcessError:
                    break
                if pid == 0:
                    assert time.monotonic() < deadline, f"viewer group {group} survived"
                    time.sleep(0.01)


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
        "if len(sys.argv) == 1:\n"
        "    with open('/dev/tty') as tty:\n"
        "        tty.readline()\n"
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
            tui.buf.clear()
            tui.send(b"q" if viewer == "formatter" else b"\r")
            assert tui.wait_for(b"0/1 seen"), "the file table did not come back"
            assert b"press Enter to return" not in tui.buf
        tui.send(b" ")
        assert tui.wait_for(b"1/1 seen"), "the file table does not take keys"
        tui.send(b"q")  # back to the bookmark list
        assert_responsive(tui)
    finally:
        tui.close()


@pytest.mark.parametrize(
    "pager", ["default", "less", "builtin", "delta", "delta-table", "JJ_PAGER"]
)
@pytest.mark.parametrize("exit_key", [b"q", b"\x03", b"\x1a"], ids=["quit", "ctrl-c", "ctrl-z"])
def test_short_diff_stays_in_pager_and_tui_recovers(
    repo: Path, pager: str, exit_key: bytes
) -> None:
    jj(repo, "config", "set", "--user", "ui.diff-formatter", ":git")
    jj(repo, "config", "set", "--user", "ui.paginate", "never")
    if pager.startswith("delta"):
        # Exercise delta's paging contract without depending on a delta install.
        delta = repo.parent / "delta"
        delta.write_text(
            f"#!{sys.executable}\n"
            "import os, shlex, sys\n"
            "assert sys.argv[1:] == ['--side-by-side', '--paging=always']\n"
            "assert os.environ['KEEP'] == 'yes'\n"
            "args = shlex.split(os.environ['DELTA_PAGER'])\n"
            "os.execvp(args[0], args)\n"
        )
        delta.chmod(0o755)
        command = [str(delta), "--side-by-side", "--paging", "auto"]
        if pager == "delta-table":
            value = (
                f"{{command={json.dumps(command)}, env={{DELTA_PAGER='less -FRX', KEEP='yes'}}}}"
            )
        else:
            value = json.dumps(shlex.join(command[:-2] + ["--paging=never"]))
        jj(repo, "config", "set", "--user", "ui.pager", value)
    elif pager in ("less", "builtin"):
        jj(
            repo,
            "config",
            "set",
            "--user",
            "ui.pager",
            ":builtin" if pager == "builtin" else "less -FRXK",
        )
    env = {"JJ_PAGER": "less -FRX"} if pager == "JJ_PAGER" else {}
    tui = start_tui(repo, "true", LESS="-F -E", DELTA_PAGER="less -FRX", KEEP="yes", **env)
    try:
        tui.send(b"j=")
        assert tui.wait_for(b"0/1 seen"), tui.buf.decode(errors="replace")
        for _ in range(2):
            tui.buf.clear()
            tui.send(b"\r")
            assert tui.wait_for(b"diff --git"), tui.buf.decode(errors="replace")
            tui.pump(0.5)
            assert b"0/1 seen" not in tui.buf, "short diff exited without waiting for q"
            tui.send(b"\x04")  # half-page in the pager, never EOF for a return prompt
            tui.pump(0.2)
            assert b"0/1 seen" not in tui.buf
            tui.buf.clear()
            if pager == "builtin" and exit_key == b"\x1a":
                # The builtin pager consumes Ctrl+Z as an unbound raw key;
                # exercise a stopped process independently of its bindings.
                os.killpg(os.tcgetpgrp(tui.fd), signal.SIGSTOP)
            else:
                tui.send(exit_key)
            assert tui.wait_for(b"0/1 seen"), tui.buf.decode(errors="replace")
            assert b"press Enter to return" not in tui.buf
            error_at = tui.buf.find(b"jj diff exited")
            assert error_at < 0, bytes(tui.buf[error_at : error_at + 100])
            assert tui.proc.poll() is None
        tui.send(b"q")
        assert_responsive(tui)
        tui.send(b"q")  # close help
        tui.pump(0.2)
        tui.send(b"\x11")  # Textual's quit key; Ctrl+C shows a quit hint
        tui.pump(1)
        assert tui.proc.poll() == 0, "the TUI should exit on the first quit key"
    finally:
        tui.close()


def test_interrupted_diff_does_not_leave_a_viewer_running(repo: Path) -> None:
    script = repo.parent / "slow_viewer.py"
    pid_file = repo.parent / "viewer.pid"
    script.write_text(
        "import fcntl, os, pathlib, signal, sys, time, tty\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "sys.stdin.read()\n"
        "with open('/dev/tty') as terminal:\n"
        "    tty.setcbreak(terminal.fileno())\n"
        "flags = fcntl.fcntl(1, fcntl.F_GETFL)\n"
        "fcntl.fcntl(1, fcntl.F_SETFL, flags | os.O_NONBLOCK)\n"
        "print('SLOW-VIEWER-READY', flush=True)\n"
        "time.sleep(60)\n"
    )
    jj(repo, "config", "set", "--user", "ui.diff-formatter", ":git")
    jj(
        repo,
        "config",
        "set",
        "--user",
        "ui.pager",
        json.dumps([sys.executable, str(script), str(pid_file)]),
    )
    tui = start_tui(repo, "true")
    try:
        tui.send(b"j=")
        assert tui.wait_for(b"0/1 seen")
        tui.buf.clear()
        tui.send(b"\r")
        assert tui.wait_for(b"SLOW-VIEWER-READY"), tui.buf.decode(errors="replace")
        viewer_pid = int(pid_file.read_text())
        tui.buf.clear()
        tui.send(b"\x03")
        assert tui.wait_for(b"0/1 seen"), tui.buf.decode(errors="replace")
        # A dead orphan may briefly be a zombie until the test's subreaper
        # collects it. A running viewer must never outlive this viewing.
        try:
            pid, _ = os.waitpid(viewer_pid, os.WNOHANG)
        except ChildProcessError:
            with pytest.raises(ProcessLookupError):
                os.kill(viewer_pid, 0)
        else:
            assert pid == viewer_pid, "interrupted viewer is still running"
        tui.send(b"q")  # back to the bookmark list, where ? opens help
        assert_responsive(tui)
    finally:
        tui.close()
