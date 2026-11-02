"""Where interactive sessions run: inline, or in a terminal multiplexer.

``ui.terminal`` picks the mode:

- ``inline``  — suspend the TUI, run the agent in this terminal, return after.
- ``tmux``    — new tmux window when sbx-tui runs inside tmux.
- ``zellij``  — new Zellij pane (closes when the agent exits) when inside Zellij.
- ``screen``  — new screen window when inside screen.
  All three fall back to inline when outside the selected multiplexer.
- ``custom``  — ``ui.terminal_command`` with ``{cmd}``, ``{branch}``, ``{repo}``
  (see :func:`fill_command` for quoting).

External launches return to the branch list immediately; the session's
liveness then comes from project-sandbox's session record (see jobs.py).
Host agents use a runner that records its PID and exit code for jobs.py.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from project_sandbox_tui.processes import reap_later

TERMINALS: tuple[str, ...] = ("inline", "tmux", "zellij", "screen", "custom")
MULTIPLEXERS: tuple[str, ...] = ("tmux", "zellij", "screen")

# Environment variables each multiplexer sets inside its sessions.
INSIDE_ENV = {"tmux": "TMUX", "zellij": "ZELLIJ", "screen": "STY"}


class LaunchError(RuntimeError):
    """The terminal/multiplexer could not start the session."""


@dataclass(frozen=True)
class ExternalLaunch:
    argv: list[str]
    message: str  # shown in the TUI once the session has been handed off
    detached: bool  # True: run detached (custom); False: wait briefly and check the exit code


def host_command(argv: list[str], summary_path: str) -> list[str]:
    """Wrap an external host command with durable lifetime reporting."""
    return [sys.executable, str(Path(__file__).with_name("host_session.py")), summary_path, *argv]


def fill_command(template: str, values: Mapping[str, str]) -> list[str]:
    """Split a configured command with shlex, then fill ``{placeholders}`` per token.

    Each placeholder must occupy a whole argument. Inserting quoted data
    into arbitrary shell source is unsafe: surrounding quotes, backticks and
    here-documents can turn it back into code. Scripts should take values as
    positional arguments instead, e.g. ``sh -c 'printf %s "$1"' sh {branch}``.
    Placeholders in a value are never expanded again. ``{cmd}`` is already a
    quoted command line and may be passed to ``sh -c {cmd}``.
    """
    pattern = re.compile("|".join(re.escape("{" + key + "}") for key in values))

    def fill(token: str) -> str:
        if pattern.fullmatch(token):
            return values[token[1:-1]]
        if match := pattern.search(token):
            raise ValueError(
                f"{match[0]} must occupy a whole argument; "
                "pass it as a separate argument and use positional parameters in shell scripts"
            )
        return token

    return [fill(t) for t in shlex.split(template)]


def effective_mode(mode: str, env: Mapping[str, str] | None = None) -> str:
    """Fall back to inline unless running inside the selected multiplexer."""
    env = os.environ if env is None else env
    if mode in MULTIPLEXERS and not env.get(INSIDE_ENV[mode]):
        return "inline"
    return mode


def shell_script(cmd: list[str], cwd: str | None = None) -> str:
    """``sh -c`` script that runs ``cmd`` and, if it fails, keeps the window open.

    Multiplexer windows close when their command exits, which would hide e.g.
    an image-build error; like inline mode, wait for Enter after a failure.
    """
    prefix = f"cd {shlex.quote(cwd)} && " if cwd else ""
    return (
        f"{prefix}{shlex.join(cmd)}; status=$?; "
        'if [ "$status" -ne 0 ]; then '
        "printf '\\n── command exited with %s; press Enter to close ──' \"$status\"; "
        "read -r _; fi; exit $status"
    )


def describe(mode: str) -> str:
    """Short phrase for the launch form: where the session will open."""
    return {
        "inline": "",
        "tmux": "opens in tmux",
        "zellij": "opens in a Zellij pane",
        "screen": "opens in screen",
        "custom": "opens via ui.terminal_command",
    }.get(effective_mode(mode), "")


def external_launch(
    mode: str,
    cmd: list[str],
    branch: str,
    repo: Path,
    template: str = "",
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> ExternalLaunch:
    """Build the command that starts ``cmd`` in another terminal or pane."""
    env = os.environ if env is None else env
    repo_s = str(repo)
    shell_cmd = shell_script(cmd)

    if mode == "custom":
        if not template:
            raise LaunchError('ui.terminal = "custom" needs ui.terminal_command')
        values = {"cmd": shlex.join(cmd), "branch": branch, "repo": repo_s}
        try:
            argv = fill_command(template, values)
        except ValueError as exc:
            raise LaunchError(f"ui.terminal_command: {exc}") from exc
        return ExternalLaunch(argv, "opened in a new terminal", detached=True)

    if mode not in MULTIPLEXERS:
        raise LaunchError(f"unknown ui.terminal {mode!r}")
    if effective_mode(mode, env) == "inline":
        raise LaunchError(f"sbx-tui is not running inside {mode}; launch inline instead")
    if which(mode) is None:
        raise LaunchError(f"ui.terminal = {mode!r} but {mode} is not on PATH")
    if mode == "tmux":
        argv = ["tmux", "new-window", "-n", branch, "-c", repo_s, shell_cmd]
        return ExternalLaunch(argv, f"opened in tmux window {branch}", detached=False)

    if mode == "zellij":
        argv = [
            "zellij",
            "run",
            "--name",
            branch,
            "--close-on-exit",
            "--cwd",
            repo_s,
            "--",
            "sh",
            "-c",
            shell_cmd,
        ]
        return ExternalLaunch(argv, f"opened in Zellij pane {branch}", detached=False)

    # screen: a window does not take a working directory, so cd first.
    script = shell_script(cmd, cwd=repo_s)
    argv = ["screen", "-t", branch, "sh", "-c", script]
    return ExternalLaunch(argv, f"opened in screen window {branch}", detached=False)


def start(launch: ExternalLaunch, cwd: Path, timeout: float = 15.0) -> None:
    """Run the launch command. Multiplexer commands return as soon as the
    window/pane/session exists, so their exit code and stderr are checked;
    a custom command may keep running (e.g. a terminal emulator) and is detached."""
    if launch.detached:
        proc = subprocess.Popen(
            launch.argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        reap_later(proc)
        return
    try:
        proc = subprocess.run(
            launch.argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise LaunchError(f"{launch.argv[0]} did not return within {timeout:.0f}s") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        reason = detail[-1] if detail else f"exit {proc.returncode}"
        raise LaunchError(f"{launch.argv[0]}: {reason}")
