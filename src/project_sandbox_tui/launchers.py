"""Where interactive sessions run: inline, or in a terminal multiplexer.

``ui.terminal`` picks the mode:

- ``inline``  — suspend the TUI, run the agent in this terminal, return after.
- ``tmux``    — new tmux window when sbx-tui runs inside tmux, otherwise a
  detached tmux session named ``sbx-<repo>-<branch>`` (``tmux attach -t …``).
- ``zellij``  — new Zellij pane (closes when the agent exits); sbx-tui must run
  inside a Zellij session.
- ``screen``  — new screen window when inside screen, otherwise a detached
  screen session (``screen -r …``).
- ``custom``  — ``ui.terminal_command`` with ``{cmd}``, ``{branch}``, ``{repo}``
  (see :func:`fill_command` for quoting).

Everything but ``inline`` returns to the branch list immediately; the session's
liveness then comes from project-sandbox's session record (see jobs.py).
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path

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


def fill_command(
    template: str, values: Mapping[str, str], shell_text: Collection[str] = ()
) -> list[str]:
    """Split a configured command with shlex, then fill ``{placeholders}`` per token.

    A placeholder that is a whole argument is passed as is: it is one argv
    entry and no shell sees it. Inside a longer argument (``sh -c "cd {repo}
    && {cmd}"``) it is shell-quoted, since such an argument is usually a
    script; branch names may contain ``$``, ``;`` or backticks. Keys in
    ``shell_text`` already hold a command line and are inserted unquoted.
    Placeholders in a value are never expanded again.
    """
    pattern = re.compile("|".join(re.escape("{" + key + "}") for key in values))

    def quoted(match: re.Match[str]) -> str:
        key = match[0][1:-1]
        return values[key] if key in shell_text else shlex.quote(values[key])

    def fill(token: str) -> str:
        if pattern.fullmatch(token):
            return values[token[1:-1]]
        return pattern.sub(quoted, token)

    return [fill(t) for t in shlex.split(template)]


def session_name(repo: Path, branch: str) -> str:
    """A multiplexer-safe session name, unique per repo and branch."""
    raw = f"sbx-{repo.name}-{branch}"
    return re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")


def shell_script(cmd: list[str], cwd: str | None = None) -> str:
    """``sh -c`` script that runs ``cmd`` and, if it fails, keeps the window open.

    Multiplexer windows close when their command exits, which would hide e.g.
    an image-build error; like inline mode, wait for Enter after a failure.
    """
    prefix = f"cd {shlex.quote(cwd)} && " if cwd else ""
    return (
        f"{prefix}{shlex.join(cmd)}; status=$?; "
        'if [ "$status" -ne 0 ]; then '
        "printf '\\n── project-sandbox exited with %s; press Enter to close ──' \"$status\"; "
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
    }.get(mode, "")


def external_launch(
    mode: str,
    cmd: list[str],
    branch: str,
    repo: Path,
    template: str = "",
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> ExternalLaunch:
    """Build the command that starts ``cmd`` (the project-sandbox argv) elsewhere."""
    env = os.environ if env is None else env
    repo_s = str(repo)
    shell_cmd = shell_script(cmd)

    if mode == "custom":
        if not template:
            raise LaunchError('ui.terminal = "custom" needs ui.terminal_command')
        values = {"cmd": shlex.join(cmd), "branch": branch, "repo": repo_s}
        argv = fill_command(template, values, shell_text={"cmd"})
        return ExternalLaunch(argv, "opened in a new terminal", detached=True)

    if mode not in MULTIPLEXERS:
        raise LaunchError(f"unknown ui.terminal {mode!r}")
    if which(mode) is None:
        raise LaunchError(f"ui.terminal = {mode!r} but {mode} is not on PATH")
    inside = bool(env.get(INSIDE_ENV[mode]))
    name = session_name(repo, branch)

    if mode == "tmux":
        if inside:
            argv = ["tmux", "new-window", "-n", branch, "-c", repo_s, shell_cmd]
            return ExternalLaunch(argv, f"opened in tmux window {branch}", detached=False)
        argv = ["tmux", "new-session", "-d", "-s", name, "-c", repo_s, shell_cmd]
        message = f"started in tmux session {name} — attach with: tmux attach -t {name}"
        return ExternalLaunch(argv, message, detached=False)

    if mode == "zellij":
        if not inside:
            raise LaunchError(
                'ui.terminal = "zellij" opens a pane in the current Zellij session; '
                "start sbx-tui inside Zellij"
            )
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
    if inside:
        argv = ["screen", "-t", branch, "sh", "-c", script]
        return ExternalLaunch(argv, f"opened in screen window {branch}", detached=False)
    argv = ["screen", "-dmS", name, "sh", "-c", script]
    message = f"started in screen session {name} — attach with: screen -r {name}"
    return ExternalLaunch(argv, message, detached=False)


def start(launch: ExternalLaunch, cwd: Path, timeout: float = 15.0) -> None:
    """Run the launch command. Multiplexer commands return as soon as the
    window/pane/session exists, so their exit code and stderr are checked;
    a custom command may keep running (e.g. a terminal emulator) and is detached."""
    if launch.detached:
        subprocess.Popen(
            launch.argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
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
