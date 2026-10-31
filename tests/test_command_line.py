"""The ``:`` command line and leader-key mappings in the running app."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, Static

from project_sandbox_tui.app import SandboxTUI
from project_sandbox_tui.config import Config
from project_sandbox_tui.screens.command import CommandScreen
from project_sandbox_tui.screens.dialogs import HelpScreen
from tests.test_app import run_app


def messages(app: SandboxTUI) -> list[str]:
    return [str(n.message) for n in app._notifications]


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Headless drivers cannot suspend; collect the Enter prompts instead."""
    prompts: list[str] = []
    monkeypatch.setattr("project_sandbox_tui.terminal.preserved", contextlib.nullcontext)
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "")
    return prompts


async def type_line(pilot: Any, line: str) -> None:
    await pilot.press("colon")
    assert isinstance(pilot.app.screen, CommandScreen)
    await pilot.press(*line)


def test_shell_command_runs_in_the_repository(
    repo: Path,
    fake_cfg: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
    terminal: list[str],
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        monkeypatch.setattr(app, "_suspended", contextlib.nullcontext)
        await pilot.press("j")  # feature/x
        await type_line(pilot, '!printf "%s\\n" "$SBX_BOOKMARK" "$SBX_REPO" "$SBX_WORKSPACE" >out')
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is app.main
        assert (repo / "out").read_text().splitlines() == ["feature/x", str(repo), ""]
        assert terminal == ["\n── exited with 0; press Enter to return to sbx-tui ──"]

        await pilot.press("g", "g")  # the working copy's workspace is the repository
        await type_line(pilot, "!echo $SBX_WORKSPACE >out; exit 3")
        await pilot.press("enter")
        await pilot.pause()
        assert (repo / "out").read_text() == f"{repo}\n"
        assert any("exited with 3" in m for m in messages(app))
        assert app.command_history.newest_first()[:2] == [
            "!echo $SBX_WORKSPACE >out; exit 3",
            '!printf "%s\\n" "$SBX_BOOKMARK" "$SBX_REPO" "$SBX_WORKSPACE" >out',
        ]

    run_app(repo, fake_cfg(), scenario)


def test_configured_commands_take_arguments(
    repo: Path,
    fake_cfg: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
    terminal: list[str],
) -> None:
    cfg = fake_cfg()
    cfg.commands = {"save": "printf '%s\\n' >out"}

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        monkeypatch.setattr(app, "_suspended", contextlib.nullcontext)
        await type_line(pilot, "save a 'b c'")
        await pilot.press("enter")
        await pilot.pause()
        assert (repo / "out").read_text() == "a\nb c\n"

        await type_line(pilot, "nope")
        await pilot.press("enter")
        await pilot.pause()
        assert any("unknown command :nope" in m for m in messages(app))

        await type_line(pilot, "help")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        assert app.screen.extra == (("configured commands", ((":save", "printf '%s\\n' >out"),)),)

    run_app(repo, cfg, scenario)


def test_command_line_completion_and_history(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    cfg = fake_cfg()
    cfg.commands = {"test": "true", "tidy": "true"}

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app.command_history.add("tidy now")
        app.command_history.add("!jj st")
        app.command_history.add("test -x")
        await type_line(pilot, "t")
        screen = app.screen
        assert isinstance(screen, CommandScreen)
        box = screen.query_one(Input)
        matches = screen.query_one("#matches", Static)
        await pilot.press("tab")
        assert box.value == "test"
        assert matches.has_class("visible")
        await pilot.press("tab")
        assert box.value == "tidy"
        await pilot.press("tab")
        assert box.value == "t"  # past the last match: what was typed
        await pilot.press("shift+tab")
        assert box.value == "tidy"
        await pilot.press("space")
        assert not matches.has_class("visible")  # typing ends the cycle

        await pilot.press("ctrl+u")  # Input: delete to the start
        await pilot.press("r", "e", "l", "tab")
        assert box.value == "reload"  # one match: completed at once

        box.value = "t"
        await pilot.pause()
        await pilot.press("up")
        assert box.value == "test -x"
        await pilot.press("up")
        assert box.value == "tidy now"  # only lines starting with "t"
        await pilot.press("up")
        assert box.value == "tidy now"  # the oldest match stays
        await pilot.press("down", "down")
        assert box.value == "t"

        box.value = ""
        await pilot.pause()
        await pilot.press("backspace")  # over the colon: cancel
        assert app.screen is app.main

        await type_line(pilot, "x")
        await pilot.press("escape")
        assert app.screen is app.main
        assert app.command_history.newest_first()[0] == "test -x"  # nothing ran

    run_app(repo, cfg, scenario)


def test_leader_mappings(
    repo: Path,
    fake_cfg: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
    terminal: list[str],
) -> None:
    cfg = fake_cfg()
    cfg.commands = {"mark": "echo marked >out"}
    cfg.leader = {"t": "mark", "gs": "!echo gs >out", "h": "help"}

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        monkeypatch.setattr(app, "_suspended", contextlib.nullcontext)
        header = app.main.query_one("#header", Static)
        await pilot.press("backslash")
        assert "\\…" in str(header.content)
        await pilot.press("t")
        await pilot.pause()
        assert (repo / "out").read_text() == "marked\n"
        assert app.command_history.entries == []  # mappings are not typed lines

        await pilot.press("backslash", "g")
        assert "\\g…" in str(header.content)
        await pilot.press("s")
        await pilot.pause()
        assert (repo / "out").read_text() == "gs\n"

        await pilot.press("backslash", "q")  # unmapped: q must not quit
        await pilot.pause()
        assert app.is_running and app.screen is app.main
        assert any("no leader mapping \\q" in m for m in messages(app))

        await pilot.press("backslash", "escape", "j")  # esc gives up; j moves again
        assert app.selected_branch_name() == "feature/x"

        await pilot.press("backslash", "h")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        assert (
            "leader mappings",
            (("\\t", ":mark"), ("\\gs", ":!echo gs >out"), ("\\h", ":help")),
        ) in app.screen.extra

    run_app(repo, cfg, scenario)
