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
    assert LastUsed.load(git_repo).agent == ""  # failed launch did not become the default


def test_filter_box_is_visible_above_footer(
    git_repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        sheet = app.main.query_one("#cheatsheet")
        box = app.main.query_one("#filter")
        footer = app.main.query_one("#footer")
        await pilot.press("slash", "m")
        await pilot.pause()
        # with the cheat-sheet: the sheet replaces the footer, filter sits below it
        assert sheet.region.height > 1 and footer.region.height == 0
        assert box.region.height == 1 and box.region.y >= sheet.region.bottom
        await pilot.press("enter", "H")
        await pilot.press("slash")
        await pilot.pause()
        # without it: footer is back and the filter is stacked above it
        assert sheet.region.height == 0
        assert box.region.height == 1 and footer.region.height == 1
        assert box.region.y < footer.region.y  # stacked, not drawn on top of each other

    run_app(git_repo, fake_cfg(), scenario)


def test_run_on_current_git_branch_uses_main_checkout(
    git_repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        assert app.selected_branch_name() == "main"  # the checked-out branch
        await pilot.press("r")
        form = app.screen
        assert isinstance(form, LaunchScreen) and form.main_checkout
        form.query_one("#prompt", TextArea).text = "sleep:3"
        await pilot.press("ctrl+s")
        await wait_until(pilot, lambda: "main" in app.jobs.jobs)
        argv = app.jobs.jobs["main"].argv
        assert "--branch" not in argv and "--keep-workspace" not in argv
        # the fake's session runs in the repo itself and is attributed to main
        await wait_until(pilot, lambda: "main" in app.sessions)
        app.jobs.stop("main", app.sessions)
        await wait_until(pilot, lambda: not app.jobs.jobs["main"].running)

    run_app(git_repo, fake_cfg(), scenario)


def test_enter_does_not_launch_unsupervised_run(
    git_repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("r")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        form.query_one("#prompt", TextArea).text = "sleep:0.1"
        form.query_one("#timeout", Input).focus()
        await pilot.press("enter")
        assert app.screen is form  # still in the form; focus moved on
        await pilot.press("escape")

    run_app(git_repo, fake_cfg(), scenario)


def test_launch_rechecks_checkout_after_form_opens(
    git_repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    from tests.conftest import run

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("r")
        form = app.screen
        assert isinstance(form, LaunchScreen) and form.main_checkout
        # Another terminal switches the checkout while the form is open.
        run("git", "checkout", "-q", "feature/x", cwd=git_repo)
        form.query_one("#prompt", TextArea).text = "sleep:0.1"
        await pilot.press("ctrl+s")
        await wait_until(pilot, lambda: "main" in app.jobs.jobs)
        assert "--branch" in app.jobs.jobs["main"].argv
        await wait_until(pilot, lambda: not app.jobs.jobs["main"].running)

    run_app(git_repo, fake_cfg(), scenario)


def test_external_launch_rechecks_busy_state(
    git_repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    from project_sandbox_tui.jobs import JobManager
    from project_sandbox_tui.sandbox import LaunchSpec

    cfg = fake_cfg()
    cfg.ui.terminal_command = "true {cmd}"

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("i")
        assert isinstance(app.screen, LaunchScreen)
        other = JobManager(git_repo, cfg)
        reserved = other.record_external(LaunchSpec(branch="main", agent="codex"), ["other"])
        await pilot.press("enter")
        assert app.jobs.jobs["main"].started_at == reserved.started_at
        assert app.jobs.jobs["main"].spec.agent == "codex"
        assert LastUsed.load(git_repo).agent == ""

    run_app(git_repo, cfg, scenario)


def test_failed_external_launch_releases_reservation(
    git_repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    cfg = fake_cfg()
    cfg.ui.terminal_command = "definitely-not-an-installed-terminal {cmd}"

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("i", "enter")
        assert "main" not in app.jobs.jobs
        assert LastUsed.load(git_repo).agent == ""

    run_app(git_repo, cfg, scenario)


def test_log_follows_partial_utf8_and_truncation(
    git_repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    from textual.widgets import Log

    from project_sandbox_tui.jobs import JobRecord
    from project_sandbox_tui.sandbox import LaunchSpec

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        path = git_repo / "agent.log"
        path.write_bytes(b"first\n\xe2\x82")  # partial euro sign at a poll boundary
        job = JobRecord(
            "main",
            LaunchSpec(branch="main", agent="claude"),
            1,
            "",
            log_path=str(path),
            stdout_path=str(git_repo / "out"),
        )
        screen = LogScreen(job)
        app.push_screen(screen)
        await pilot.pause()
        with path.open("ab") as stream:
            stream.write(b"\xac\n")
        screen._poll()
        log = screen.query_one(Log)
        assert log.lines[:2] == ["first", "€"]
        path.write_bytes(b"new\n")
        screen._poll()
        assert log.lines == ["new", ""]
        # A replacement file can be larger, so size alone cannot detect rotation.
        replacement = path.with_suffix(".replacement")
        replacement.write_text("replacement contents\n")
        replacement.replace(path)
        screen._poll()
        assert log.lines == ["replacement contents", ""]
        await pilot.press("q")

    run_app(git_repo, fake_cfg(), scenario)


def test_jj_repo_without_bookmarks_runs_in_working_copy(
    jj_repo_no_bookmarks: Path, fake_cfg: Callable[..., Config]
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        assert [r[1] for r in row_texts(app)] == ["@ working copy ●"]
        await pilot.press("r")
        form = app.screen
        assert isinstance(form, LaunchScreen) and form.main_checkout
        assert "@ working copy" in str(form.query_one(".title").render())
        form.query_one("#prompt", TextArea).text = "sleep:3"
        await pilot.press("ctrl+s")
        await wait_until(pilot, lambda: "@" in app.jobs.jobs)
        assert "--branch" not in app.jobs.jobs["@"].argv
        await wait_until(pilot, lambda: "@" in app.sessions)  # session in the repo itself
        app.jobs.stop("@", app.sessions)
        await wait_until(pilot, lambda: not app.jobs.jobs["@"].running)

    run_app(jj_repo_no_bookmarks, fake_cfg(), scenario)


def test_prompt_history_completion_browse_and_search(
    git_repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    from project_sandbox_tui.history import PromptHistory
    from project_sandbox_tui.screens.history import HistorySearchScreen
    from project_sandbox_tui.widgets import PromptArea

    history = PromptHistory.for_repo(git_repo)
    for p in ("fix the flaky parser test", "write docs for config", "fix lint warnings"):
        history.add(p)

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j", "r")  # feature/x: no previous run, empty prompt
        form = app.screen
        assert isinstance(form, LaunchScreen)
        area = form.query_one("#prompt", PromptArea)
        assert "3 prompts in history" in str(form.query_one("#prompt-hint").render())

        # fish-style autosuggestion: newest entry extending the typed text
        await pilot.press("f", "i", "x")
        assert area.suggestion == " lint warnings"
        await pilot.press("space", "t")
        assert area.suggestion == "he flaky parser test"
        await pilot.press("right")  # accept
        assert area.text == "fix the flaky parser test" and area.suggestion == ""

        # ctrl+p / ctrl+n: walk entries starting with what was typed
        area.replace_text("fix")
        await pilot.pause()
        await pilot.press("ctrl+p")
        assert area.text == "fix lint warnings" and area.browse_position == (1, 2)
        await pilot.press("ctrl+p")
        assert area.text == "fix the flaky parser test"
        await pilot.press("ctrl+n", "ctrl+n")  # past the newest: back to the typed text
        assert area.text == "fix" and area.browse_position is None

        # ctrl+r: search modal, filtered by the words typed so far
        area.replace_text("")
        await pilot.pause()
        await pilot.press("ctrl+r")
        assert isinstance(app.screen, HistorySearchScreen)
        await pilot.press("d", "o", "c", "s", "enter")
        assert app.screen is form and area.text == "write docs for config"

        # launching records the prompt as the newest entry
        area.replace_text("sleep:0.1 brand new prompt")
        await pilot.press("ctrl+s")
        await wait_until(pilot, lambda: "feature/x" in app.jobs.jobs)

    run_app(git_repo, fake_cfg(), scenario)
    assert PromptHistory.for_repo(git_repo).newest_first()[0] == "sleep:0.1 brand new prompt"


def test_cheatsheet_shown_on_start_and_toggles(
    git_repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        sheet = app.main.query_one("#cheatsheet")
        assert sheet.region.height > 0
        assert "unsupervised" in app.export_screenshot()  # drawn on the welcome screen
        await pilot.press("H")
        await pilot.pause()
        assert sheet.region.height == 0 and app.main.query_one("#footer").region.height == 1
        await pilot.press("H")
        await pilot.pause()
        assert sheet.region.height > 0

    run_app(git_repo, fake_cfg(), scenario)

    cfg = fake_cfg()
    cfg.ui.cheatsheet = False

    async def hidden(app: SandboxTUI, pilot: Any) -> None:
        assert app.main.query_one("#cheatsheet").region.height == 0

    run_app(git_repo, cfg, hidden)
