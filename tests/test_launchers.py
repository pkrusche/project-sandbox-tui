from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from project_sandbox_tui import launchers
from project_sandbox_tui.launchers import LaunchError, external_launch

CMD = ["project-sandbox", "/work/my repo", "img", "--branch", "feat/x", "--agent", "claude"]
REPO = Path("/work/my repo")
SHELL_CMD = launchers.shell_script(CMD)


def found(_name: str) -> str:
    return "/usr/bin/x"


def launch(
    mode: str, env: dict[str, str] | None = None, template: str = ""
) -> launchers.ExternalLaunch:
    return external_launch(mode, CMD, "feat/x", REPO, template, env=env or {}, which=found)


@pytest.mark.parametrize("mode, variable", launchers.INSIDE_ENV.items())
@pytest.mark.parametrize("value", [None, "", "session"])
def test_effective_mode_requires_selected_manager(
    mode: str, variable: str, value: str | None
) -> None:
    env = dict.fromkeys(launchers.INSIDE_ENV.values(), "other-manager")
    if value is None:
        del env[variable]
    else:
        env[variable] = value
    assert launchers.effective_mode(mode, env) == (mode if value else "inline")


@pytest.mark.parametrize("mode", ["inline", "custom"])
def test_effective_mode_preserves_other_modes(mode: str) -> None:
    assert launchers.effective_mode(mode, {}) == mode


@pytest.mark.parametrize("mode, variable", launchers.INSIDE_ENV.items())
def test_outside_manager_cannot_launch_externally(
    mode: str, variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(variable, raising=False)
    assert launchers.describe(mode) == ""
    with pytest.raises(LaunchError, match="launch inline instead"):
        launch(mode)


def test_tmux_inside_opens_window() -> None:
    inside = launch("tmux", {"TMUX": "/tmp/tmux-1/default,1,0"})
    assert inside.argv == ["tmux", "new-window", "-n", "feat/x", "-c", str(REPO), SHELL_CMD]
    assert not inside.detached  # exit code is checked


def test_zellij_inside_opens_pane() -> None:
    inside = launch("zellij", {"ZELLIJ": "0"})
    assert inside.argv == [
        "zellij", "run", "--name", "feat/x", "--close-on-exit", "--cwd", str(REPO),
        "--", "sh", "-c", SHELL_CMD,
    ]  # fmt: skip


def test_screen_inside_opens_window() -> None:
    script = launchers.shell_script(CMD, cwd=str(REPO))
    assert launch("screen", {"STY": "123.pts-0.host"}).argv == [
        "screen", "-t", "feat/x", "sh", "-c", script,
    ]  # fmt: skip


def test_custom_template_substitutes_per_token() -> None:
    custom = launch("custom", template="wezterm start --cwd {repo} -- sh -c {cmd}")
    assert custom.argv == [
        "wezterm",
        "start",
        "--cwd",
        str(REPO),
        "--",
        "sh",
        "-c",
        shlex.join(CMD),
    ]
    assert custom.detached  # a terminal emulator may keep running
    with pytest.raises(LaunchError, match="terminal_command"):
        launch("custom")


def test_custom_template_quotes_embedded_placeholders() -> None:
    branch = "x;$(touch pwned)`id`"
    template = 'sh -c "cd {repo} && printf %s {branch} && {cmd}" --title {branch}'
    argv = launchers.external_launch(
        "custom", CMD, branch, Path("/my repo"), template, env={}, which=lambda _: None
    ).argv
    assert argv[:2] == ["sh", "-c"]
    # Embedded: quoted values, {cmd} stays a command line.
    assert argv[2] == (
        f"cd {shlex.quote('/my repo')} && printf %s {shlex.quote(branch)} && {shlex.join(CMD)}"
    )
    assert argv[3:] == ["--title", branch]  # a whole argument is passed as is
    # The shell sees the branch name as one literal word.
    printed = subprocess.run(
        ["sh", "-c", f"printf %s {shlex.quote(branch)}"], capture_output=True, text=True
    )
    assert printed.stdout == branch


def test_fill_command_does_not_expand_values_again() -> None:
    values = {"title": "{message}", "message": "m"}
    assert launchers.fill_command("n {title} '-t={title}'", values) == [
        "n",
        "{message}",
        f"-t={shlex.quote('{message}')}",
    ]


def test_missing_multiplexer_is_reported() -> None:
    with pytest.raises(LaunchError, match="tmux is not on PATH"):
        external_launch("tmux", CMD, "b", REPO, env={"TMUX": "session"}, which=lambda _n: None)


def test_start_surfaces_multiplexer_errors(tmp_path: Path) -> None:
    failing = launchers.ExternalLaunch(
        [sys.executable, "-c", "import sys; sys.exit('duplicate session: sbx-x')"], "", False
    )
    with pytest.raises(LaunchError, match="duplicate session"):
        launchers.start(failing, cwd=tmp_path)
    launchers.start(launchers.ExternalLaunch(["true"], "", False), cwd=tmp_path)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_real_tmux_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Use a private server to check window execution and failure acknowledgement."""
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)
    subprocess.run(["tmux", "new-session", "-d", "-s", "host"], check=True)
    try:
        address = subprocess.check_output(
            ["tmux", "display-message", "-p", "#{socket_path},#{pid},0"], text=True
        ).strip()
        monkeypatch.setenv("TMUX", address)
        marker = tmp_path / "ran"
        cmd = [sys.executable, "-c", f"import pathlib; pathlib.Path({str(marker)!r}).touch()"]
        launchers.start(external_launch("tmux", cmd, "success", tmp_path), cwd=tmp_path)
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists()

        def windows() -> list[str]:
            return subprocess.check_output(
                ["tmux", "list-windows", "-t", "host", "-F", "#{window_name}"], text=True
            ).splitlines()

        while "success" in windows() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "success" not in windows()
        failing = external_launch("tmux", ["sh", "-c", "exit 4"], "bad", tmp_path)
        launchers.start(failing, cwd=tmp_path)
        assert "bad" in windows()
        subprocess.run(["tmux", "send-keys", "-t", "host:bad", "Enter"], check=True)
        deadline = time.monotonic() + 10
        while "bad" in windows() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "bad" not in windows()
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)
