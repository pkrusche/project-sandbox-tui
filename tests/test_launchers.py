from __future__ import annotations

import os
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


def test_session_name_is_multiplexer_safe() -> None:
    assert launchers.session_name(Path("/a/my.repo"), "feat/x:1") == "sbx-my-repo-feat-x-1"


def test_tmux_inside_opens_window_outside_detached_session() -> None:
    inside = launch("tmux", {"TMUX": "/tmp/tmux-1/default,1,0"})
    assert inside.argv == ["tmux", "new-window", "-n", "feat/x", "-c", str(REPO), SHELL_CMD]
    outside = launch("tmux")
    assert outside.argv == [
        "tmux", "new-session", "-d", "-s", "sbx-my-repo-feat-x", "-c", str(REPO), SHELL_CMD,
    ]  # fmt: skip
    assert "tmux attach -t sbx-my-repo-feat-x" in outside.message
    assert not inside.detached and not outside.detached  # exit code is checked


def test_zellij_pane_inside_only() -> None:
    inside = launch("zellij", {"ZELLIJ": "0"})
    assert inside.argv == [
        "zellij", "run", "--name", "feat/x", "--close-on-exit", "--cwd", str(REPO),
        "--", "sh", "-c", SHELL_CMD,
    ]  # fmt: skip
    # outside, `zellij run` prints "There is no active session!" and still exits 0
    with pytest.raises(LaunchError, match="inside Zellij"):
        launch("zellij")


def test_screen_inside_window_outside_detached_session() -> None:
    script = launchers.shell_script(CMD, cwd=str(REPO))
    assert launch("screen", {"STY": "123.pts-0.host"}).argv == [
        "screen", "-t", "feat/x", "sh", "-c", script,
    ]  # fmt: skip
    outside = launch("screen")
    assert outside.argv == ["screen", "-dmS", "sbx-my-repo-feat-x", "sh", "-c", script]
    assert "screen -r sbx-my-repo-feat-x" in outside.message


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
        external_launch("tmux", CMD, "b", REPO, env={}, which=lambda _n: None)


def test_start_surfaces_multiplexer_errors(tmp_path: Path) -> None:
    failing = launchers.ExternalLaunch(
        [sys.executable, "-c", "import sys; sys.exit('duplicate session: sbx-x')"], "", False
    )
    with pytest.raises(LaunchError, match="duplicate session"):
        launchers.start(failing, cwd=tmp_path)
    launchers.start(launchers.ExternalLaunch(["true"], "", False), cwd=tmp_path)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_real_tmux_detached_session(tmp_path: Path) -> None:
    """End to end with a private tmux server: the session runs the command, then ends."""
    env = {**os.environ, "TMUX_TMPDIR": str(tmp_path)}
    env.pop("TMUX", None)
    marker = tmp_path / "ran"
    cmd = [
        sys.executable,
        "-c",
        f"import pathlib, time; pathlib.Path({str(marker)!r}).touch(); time.sleep(1)",
    ]
    old = os.environ.copy()
    os.environ.clear()
    os.environ.update(env)  # start() inherits the environment, like the TUI does
    try:
        spec = external_launch("tmux", cmd, "feat/x", tmp_path, env=env)
        launchers.start(spec, cwd=tmp_path)
        name = launchers.session_name(tmp_path, "feat/x")
        assert subprocess.run(["tmux", "has-session", "-t", name], env=env).returncode == 0
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists()
        while time.monotonic() < deadline:  # the session closes when the command exits
            if (
                subprocess.run(
                    ["tmux", "has-session", "-t", name], env=env, capture_output=True
                ).returncode
                != 0
            ):
                break
            time.sleep(0.1)
        else:
            pytest.fail("tmux session did not end with the command")
        # a failing command keeps its window open until Enter, so errors stay readable
        failing = external_launch("tmux", ["sh", "-c", "exit 4"], "bad", tmp_path, env=env)
        launchers.start(failing, cwd=tmp_path)
        bad = launchers.session_name(tmp_path, "bad")
        time.sleep(0.5)
        has = ["tmux", "has-session", "-t", bad]
        assert subprocess.run(has, env=env).returncode == 0
        subprocess.run(["tmux", "send-keys", "-t", bad, "Enter"], env=env, check=True)
        for _ in range(50):
            if subprocess.run(has, env=env, capture_output=True).returncode != 0:
                break
            time.sleep(0.1)
        else:
            pytest.fail("failed session did not close after Enter")
        # a second launch while one is running is reported, not silently lost
        cmd_long = [sys.executable, "-c", "import time; time.sleep(30)"]
        spec = external_launch("tmux", cmd_long, "feat/x", tmp_path, env=env)
        launchers.start(spec, cwd=tmp_path)
        with pytest.raises(LaunchError, match="duplicate session"):
            launchers.start(spec, cwd=tmp_path)
    finally:
        subprocess.run(["tmux", "kill-server"], env=env, capture_output=True)
        os.environ.clear()
        os.environ.update(old)
