"""End-to-end UI tests with Textual's Pilot (no pytest-asyncio needed)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from textual.widgets import Input, TextArea

from project_sandbox_tui.app import SandboxTUI
from project_sandbox_tui.config import Config, LastUsed
from project_sandbox_tui.screens.dialogs import HelpScreen, NewBranchScreen
from project_sandbox_tui.screens.launch import LaunchScreen
from project_sandbox_tui.screens.log import LogScreen
from project_sandbox_tui.vcs import detect


def run_app(
    repo: Path, cfg: Config, scenario: Callable[[SandboxTUI, Any], Coroutine[Any, Any, None]]
) -> SandboxTUI:
    cfg.ui.refresh_seconds = 0.2
    cfg.ui.bell = False
    cfg.ui.notify_command = "true"  # don't pop real desktop notifications
    app = SandboxTUI(repo, detect(repo), cfg)

    async def main() -> None:
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await scenario(app, pilot)

    asyncio.run(main())
    return app


async def wait_until(pilot: Any, predicate: Callable[[], bool], timeout: float = 10) -> None:
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition not met in time")


def row_texts(app: SandboxTUI) -> list[list[str]]:
    table = app.table
    return [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]


def test_list_navigation_and_filter(git_repo: Path, fake_cfg: Callable[..., Config]) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        assert [r[1] for r in row_texts(app)] == ["main ●", "feature/x"]
        assert app.selected_branch_name() == "main"
        await pilot.press("j")
        assert app.selected_branch_name() == "feature/x"
        await pilot.press("g", "g")
        assert app.selected_branch_name() == "main"
        await pilot.press("G")
        assert app.selected_branch_name() == "feature/x"

        await pilot.press("slash", "m", "a")
        assert isinstance(app.focused, Input)
        assert [r[1] for r in row_texts(app)] == ["main ●"]
        await pilot.press("enter")  # keep filter, back to list
        assert app.filter_text == "ma"
        await pilot.press("escape")  # clear filter
        assert len(row_texts(app)) == 2

        await pilot.press("question_mark")
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("q")  # any key closes help (and must not quit the app)
        assert not isinstance(app.screen, HelpScreen)
        assert app.is_running

    run_app(git_repo, fake_cfg(), scenario)


def test_create_branch(git_repo: Path, fake_cfg: Callable[..., Config]) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("n")
        assert isinstance(app.screen, NewBranchScreen)
        assert app.screen.query_one("#start", Input).value == "main"
        await pilot.press("b", "a", "d", "space", "x", "enter")  # invalid name
        assert isinstance(app.screen, NewBranchScreen)
        app.screen.query_one("#name", Input).value = "topic/new"
        await pilot.press("enter")
        await wait_until(pilot, lambda: app.selected_branch_name() == "topic/new")

    run_app(git_repo, fake_cfg(), scenario)
    assert "topic/new" in {b.name for b in detect(git_repo).list_branches()}


def test_background_run_notifies_and_shows_log(
    git_repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    cfg = fake_cfg()
    cfg.models = {"codex": ["gpt-x"]}

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j", "r")  # feature/x, unsupervised
        form = app.screen
        assert isinstance(form, LaunchScreen)
        assert isinstance(app.focused, TextArea)
        # empty prompt is refused
        await pilot.press("ctrl+s")
        assert app.screen is form
        form.query_one("#prompt", TextArea).text = "sleep:0.6 exit:0"
        await pilot.press("shift+tab", "shift+tab", "shift+tab", "shift+tab", "l")  # agent → codex
        assert form.current_spec().agent == "codex"
        assert form.current_spec().model == ""  # agent default until chosen
        form.query_one("#model", Input).value = "gpt-x"
        preview = str(form.query_one("#preview").render())
        assert "--agent codex" in preview and "--prompt-text" in preview
        await pilot.press("ctrl+s")
        assert not isinstance(app.screen, LaunchScreen)

        await wait_until(pilot, lambda: "feature/x" in app.jobs.jobs)
        assert app.jobs.jobs["feature/x"].running
        # one sandbox per branch: a second launch is refused before the form opens
        await pilot.press("r")
        assert not isinstance(app.screen, LaunchScreen)

        await wait_until(pilot, lambda: app.jobs.jobs["feature/x"].status == "completed")
        await wait_until(pilot, lambda: app.jobs.jobs["feature/x"].notified)
        row = next(r for r in row_texts(app) if r[1] == "feature/x")
        assert row[0] == "✓" and row[4] == "completed" and row[5] == "codex/gpt-x"

        await pilot.press("l")
        assert isinstance(app.screen, LogScreen)
        await pilot.pause(0.2)
        assert app.screen.current is not None and app.screen.current.suffix == ".md"
        await pilot.press("q")
        assert not isinstance(app.screen, LogScreen)

        await pilot.press("c")
        assert "feature/x" not in app.jobs.jobs

    run_app(git_repo, cfg, scenario)
    # the choice became the new default for this repo
    assert LastUsed.load(git_repo).agent == "codex"


def test_launch_form_keys_do_not_trigger_list_actions(
    git_repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("i")
        assert isinstance(app.screen, LaunchScreen)
        await pilot.press("q", "r", "n")  # on the agent CycleField: no quit / no new form
        assert isinstance(app.screen, LaunchScreen) and app.is_running
        await pilot.press("escape")
        assert not isinstance(app.screen, LaunchScreen)

    run_app(git_repo, fake_cfg(), scenario)


def test_missing_image_config_is_reported(git_repo: Path) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("r")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        assert "no image configured" in str(form.query_one("#preview").render())
        form.query_one("#prompt", TextArea).text = "go"
        await pilot.press("ctrl+s")
        await pilot.pause(0.1)
        assert "main" not in app.jobs.jobs

    run_app(git_repo, Config(), scenario)
