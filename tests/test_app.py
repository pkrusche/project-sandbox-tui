"""End-to-end UI tests with Textual's Pilot (no pytest-asyncio needed)."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, Static, TextArea

from project_sandbox_tui.app import SandboxTUI
from project_sandbox_tui.config import Config, LastUsed
from project_sandbox_tui.screens.dialogs import HelpScreen, NewBranchScreen
from project_sandbox_tui.screens.launch import LaunchScreen
from project_sandbox_tui.screens.log import LogScreen
from project_sandbox_tui.vcs import detect
from project_sandbox_tui.widgets import CycleField


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


@pytest.mark.parametrize("mode", ["r", "i"])
@pytest.mark.parametrize("multiple", [False, True])
def test_launch_inputs_remain_visible_when_focused(
    git_repo: Path, fake_cfg: Callable[..., Config], mode: str, multiple: bool
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        if multiple:
            await pilot.press("v", "G")
        await pilot.press(mode)
        form = app.screen
        assert isinstance(form, LaunchScreen)
        inputs = list(form.query(Input))
        for field in inputs:
            field.focus()
            await pilot.pause()
            # no select-all on focus: its highlight would hide the value
            assert field.selection.is_empty
            await pilot.press("end", "1")
            assert field.value.endswith("1")
            assert field.content_size.height == 1
            assert field.value in field.render_line(0).text
        # Focus changes must leave the other one-line fields readable too.
        assert all(field.content_size.height == 1 for field in inputs)

    run_app(git_repo, fake_cfg(), scenario)


@pytest.mark.parametrize("mode", ["r", "i"])
def test_launch_form_shows_base_branch(
    git_repo: Path, fake_cfg: Callable[..., Config], mode: str
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j")
        selected = app.selected_branch_name()
        await pilot.press(mode)
        form = app.screen
        assert isinstance(form, LaunchScreen)
        assert str(form.query_one("#start-branch", Static).render()) == selected

    run_app(git_repo, fake_cfg(), scenario)


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
        await pilot.press("ctrl+enter")
        assert app.screen is form
        form.query_one("#prompt", TextArea).text = "sleep:0.6 exit:0"
        await pilot.press("shift+tab", "shift+tab", "shift+tab", "shift+tab", "l")  # agent → codex
        assert form.current_spec().agent == "codex"
        assert form.current_spec().model == ""  # agent default until chosen
        form.query_one("#model", Input).value = "gpt-x"
        preview = str(form.query_one("#preview").render())
        assert "--agent codex" in preview and "--prompt-text" in preview
        await pilot.press("ctrl+enter")
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
        await pilot.press("ctrl+enter")
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
        await pilot.press("ctrl+enter")
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
        await pilot.press("ctrl+s")
        assert app.screen is form
        assert form.query_one("#prompt", TextArea).text == "sleep:0.1"
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
        await pilot.press("ctrl+enter")
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
        assert form.query_one("#branch", Input).value == "@"
        form.query_one("#prompt", TextArea).text = "sleep:3"
        await pilot.press("ctrl+enter")
        await wait_until(pilot, lambda: "@" in app.jobs.jobs)
        assert "--branch" not in app.jobs.jobs["@"].argv
        await wait_until(pilot, lambda: "@" in app.sessions)  # session in the repo itself
        app.jobs.stop("@", app.sessions)
        await wait_until(pilot, lambda: not app.jobs.jobs["@"].running)

    run_app(jj_repo_no_bookmarks, fake_cfg(), scenario)


def test_jj_working_copy_alongside_bookmarks(
    jj_repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        assert [r[1] for r in row_texts(app)] == ["@ working copy ●", "main ●"]
        assert "1 bookmark ·" in str(app.main.query_one("#header").render())
        await pilot.press("r")  # on @: runs in the repo's own workspace
        form = app.screen
        assert isinstance(form, LaunchScreen) and form.main_checkout
        form.query_one("#prompt", TextArea).text = "sleep:3"
        await pilot.press("ctrl+enter")
        await wait_until(pilot, lambda: "@" in app.jobs.jobs)
        assert "--branch" not in app.jobs.jobs["@"].argv
        await wait_until(pilot, lambda: "@" in app.sessions)
        assert "main" not in app.sessions  # the bookmark stays free for its own sandbox

        await pilot.press("j", "r")  # on the bookmark: a separate jj workspace via --branch
        form = app.screen
        assert isinstance(form, LaunchScreen) and not form.main_checkout
        form.query_one("#prompt", TextArea).text = "sleep:0.1"
        await pilot.press("ctrl+enter")
        await wait_until(pilot, lambda: "main" in app.jobs.jobs)
        argv = app.jobs.jobs["main"].argv
        assert argv[argv.index("--branch") + 1] == "main"

        app.jobs.stop("@", app.sessions)
        await wait_until(pilot, lambda: not app.jobs.jobs["@"].running)

    run_app(jj_repo, fake_cfg(), scenario)


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
        assert "3 prompts in history" in str(form.query_one("#prompt-hint", Static).content)

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

        # ctrl+l clears the prompt, ctrl+z brings it back
        await pilot.press("ctrl+l")
        assert area.text == ""
        await pilot.press("ctrl+z")
        assert area.text == "write docs for config"

        # launching records the prompt as the newest entry
        area.replace_text("sleep:0.1 brand new prompt")
        await pilot.press("ctrl+enter")
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


def test_enter_that_launches_interactive_session_does_not_leak(
    git_repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the Enter that submitted the launch form used to bubble up to the
    App after the form was popped, where it matched the branch list's Enter binding
    and opened the log screen, so after the session the TUI looked frozen."""
    import contextlib

    from project_sandbox_tui.screens.log import LogScreen

    cfg = fake_cfg()
    cfg.sandbox.command = "true"  # a real child that exits 0 straight away

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        # headless drivers cannot suspend; the session itself is what we stub out
        monkeypatch.setattr(app, "suspend", contextlib.nullcontext)
        await pilot.press("i")
        assert isinstance(app.screen, LaunchScreen)
        await pilot.press("enter")  # launch from the agent field
        await wait_until(pilot, lambda: "main" in app.jobs.jobs)
        await wait_until(pilot, lambda: not app.jobs.jobs["main"].running)
        await pilot.pause(0.3)
        assert app.screen is app.main, f"stray screen after session: {app.screen!r}"
        assert not isinstance(app.screen, LogScreen)
        await pilot.press("question_mark")  # and the list still takes keys
        assert isinstance(app.screen, HelpScreen)

    run_app(git_repo, cfg, scenario)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_interactive_session_in_detached_tmux_session(
    git_repo: Path, fake_cfg: Callable[..., Config], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess
    import sys

    from project_sandbox_tui import launchers

    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))  # private tmux server
    monkeypatch.delenv("TMUX", raising=False)  # as if sbx-tui runs outside tmux
    agent = Path(__file__).with_name("fake_interactive_agent.py")
    cfg = fake_cfg()
    cfg.sandbox.command = f"{sys.executable} {agent}"
    cfg.ui.terminal = "tmux"
    name = launchers.session_name(git_repo, "main")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("i")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        assert "opens in tmux" in str(form.query_one(".title").render())
        await pilot.press("enter")
        await wait_until(pilot, lambda: "main" in app.jobs.jobs)
        job = app.jobs.jobs["main"]
        assert job.external and job.running
        has = ["tmux", "has-session", "-t", name]
        await wait_until(pilot, lambda: subprocess.run(has).returncode == 0)
        assert app.screen is app.main  # the TUI is not suspended
        # a second interactive launch on the branch is refused while it runs
        await pilot.press("i")
        assert not isinstance(app.screen, LaunchScreen)
        subprocess.run(["tmux", "send-keys", "-t", name, "q"], check=True)  # quit the agent
        await wait_until(pilot, lambda: subprocess.run(has, capture_output=True).returncode != 0)

    try:
        run_app(git_repo, cfg, scenario)
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)


def test_reload_config(git_repo: Path, tmp_path: Path) -> None:
    from project_sandbox_tui.config import load

    global_path = tmp_path / "custom.toml"
    global_path.write_text('[defaults]\nagent = "codex"\n')
    project_path = git_repo / ".project-sandbox-tui.toml"
    project_path.write_text('[project]\nbase_image = "old"\n')
    cfg = load(git_repo, global_path)
    cfg.sandbox.extra_args.append("--dry-run")
    app = SandboxTUI(git_repo, detect(git_repo), cfg, config_path=global_path, dry_run=True)

    async def scenario() -> None:
        async with app.run_test(size=(120, 30)) as pilot:
            old_timer = app._refresh_timer
            global_path.write_text('[defaults]\nagent = "bash"\n')
            project_path.write_text(
                '[project]\nbase_image = "new"\ndefault_branch_start = "feature/x"\n'
                "[ui]\nrefresh_seconds = 7\ncheatsheet = false\n"
            )
            await pilot.press("ctrl+r")
            assert app.cfg.defaults.agent == "bash"
            assert app.cfg.project.base_image == "new"
            assert app.cfg.sandbox.extra_args == ["--dry-run"]
            assert app.jobs.cfg is app.cfg
            assert app.default_start == "feature/x"
            assert not app.cheatsheet_visible
            assert app._refresh_timer is not old_timer
            assert app.cfg.ui.refresh_seconds == 7
            good_cfg, good_timer = app.cfg, app._refresh_timer
            project_path.write_text("[ui]\nrefresh_seconds = 0\n")
            await pilot.press("ctrl+r")
            assert app.cfg is good_cfg
            assert app.jobs.cfg is good_cfg
            assert app._refresh_timer is good_timer
            assert any("Config reload failed" in str(n.message) for n in app._notifications)

    asyncio.run(scenario())


def test_modifier_hints(git_repo: Path, fake_cfg: Callable[..., Config]) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("left_control")
        assert "ctrl+r reload" in str(app.main.query_one("#cheatsheet", Static).content)
        await pilot.press("right_shift")
        assert "G bottom" in str(app.main.query_one("#cheatsheet", Static).content)
        await pilot.press("left_alt")
        assert "no shortcuts" in str(app.main.query_one("#footer", Static).content)
        await pilot.press("r")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        await pilot.press("right_control")
        hint = str(form.query_one("#launch-hint", Static).content)
        assert "ctrl+enter launch" in hint and "ctrl+r search" in hint
        await pilot.press("left_shift")
        assert "shift+tab" in str(form.query_one("#launch-hint", Static).content)
        await pilot.press("a")
        assert "ctrl+enter launch" in str(form.query_one("#launch-hint", Static).content)
        await pilot.press("escape", "question_mark", "left_control")
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("q")
        assert app.screen is app.main


# ---------------------------------------------------------------- octopus


def _add_git_branch(repo: Path, name: str, path: str, text: str) -> None:
    from tests.conftest import run

    run("git", "checkout", "-q", "-b", name, "main", cwd=repo)
    (repo / path).write_text(text)
    run("git", "add", path, cwd=repo)
    run("git", "commit", "-q", "-m", f"{name}: {path}", cwd=repo)
    run("git", "checkout", "-q", "main", cwd=repo)


def test_selection_keys(git_repo: Path, fake_cfg: Callable[..., Config]) -> None:
    _add_git_branch(git_repo, "fix/y", "y.txt", "y\n")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        names = [r[1] for r in row_texts(app)]
        assert len(names) == 3
        await pilot.press("space")  # mark the first row and move down
        assert app.selection() == [app.branches[0].name]
        assert row_texts(app)[0][1].startswith("+ ")
        await pilot.press("space")
        assert len(app.selection()) == 2
        assert "2 selected" in str(app.main.query_one("#header").render())
        await pilot.press("escape")
        assert app.selection() == []
        # visual range: v at the top, G extends to the bottom
        await pilot.press("g", "g", "v", "G")
        assert len(app.selection()) == 3
        assert "VISUAL" in str(app.main.query_one("#header").render())
        await pilot.press("v")  # end visual, keep the range as marks
        await pilot.press("g", "g")
        assert len(app.selection()) == 3 and app._visual_anchor is None
        await pilot.press("escape")
        assert app.selection() == []
        # one branch is not enough
        await pilot.press("space", "r")
        assert not isinstance(app.screen, LaunchScreen)

    run_app(git_repo, fake_cfg(), scenario)


@pytest.mark.parametrize("target", ["main", "topic/from-form"])
def test_launch_branch_field(git_repo: Path, fake_cfg: Callable[..., Config], target: str) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j", "r")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        field = form.query_one("#branch", Input)
        assert field.value == "feature/x"
        field.value = target
        form.query_one("#prompt", TextArea).text = "exit:0"
        await pilot.pause()
        assert form.current_spec().branch == target
        assert form.current_spec().main_checkout == (target == "main")
        await pilot.press("ctrl+enter")
        await wait_until(pilot, lambda: target in app.jobs.jobs)
        assert app.jobs.jobs[target].spec.branch == target
        if target != "main":
            branches = {b.name: b for b in detect(git_repo).list_branches()}
            assert branches[target].head == branches["feature/x"].head

    run_app(git_repo, fake_cfg(), scenario)


def test_launch_command_preview_scrolls(git_repo: Path, fake_cfg: Callable[..., Config]) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("r")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        panel = form.query_one("Vertical")
        height = panel.size.height
        form.query_one("#prompt", TextArea).text = "long prompt " * 1000
        await pilot.pause()
        scroll = form.query_one("#command-scroll")
        assert panel.size.height == height
        assert scroll.size.height == 3
        assert scroll.max_scroll_y > 0
        scroll.scroll_end(animate=False)
        await pilot.pause()
        assert scroll.scroll_y > 0
        await pilot.press("escape")

    run_app(git_repo, fake_cfg(), scenario)


def test_cleanup_workspaces_key(git_repo: Path, fake_cfg: Callable[..., Config]) -> None:
    from project_sandbox_tui.screens.dialogs import CleanupScreen, MessageScreen
    from project_sandbox_tui.vcs import workspace_path
    from tests.conftest import run

    path = workspace_path(git_repo, "git", "feature/x", None)
    run("git", "worktree", "add", str(path), "feature/x", cwd=git_repo)
    dirty = git_repo.parent / "dirty"
    run("git", "worktree", "add", "--detach", str(dirty), "main", cwd=git_repo)
    (dirty / "untracked").write_text("keep")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("W")
        await wait_until(pilot, lambda: isinstance(app.screen, CleanupScreen))
        assert isinstance(app.screen, CleanupScreen)
        assert [(ws.name, ws.removable) for ws in app.screen.workspaces] == [
            ("feature/x", True),
            ("dirty (detached)", False),
        ]
        assert app.screen.selected == {0}
        await pilot.press("escape")
        assert not isinstance(app.screen, CleanupScreen)
        await pilot.pause(0.2)
        assert path.exists()

        await pilot.press("W")
        await wait_until(pilot, lambda: isinstance(app.screen, CleanupScreen))
        await pilot.press("a")  # deselect all
        await pilot.press("enter")  # nothing selected: nothing happens
        await pilot.pause(0.2)
        assert path.exists()

        await pilot.press("W")
        await wait_until(pilot, lambda: isinstance(app.screen, CleanupScreen))
        await pilot.press("enter")
        await wait_until(pilot, lambda: not path.exists())
        assert dirty.exists()
        assert app.is_running

        (dirty / "untracked").unlink()
        run("git", "worktree", "remove", str(dirty), cwd=git_repo)
        await pilot.press("W")  # nothing left: just a message
        await wait_until(pilot, lambda: isinstance(app.screen, MessageScreen))
        assert isinstance(app.screen, MessageScreen)
        assert "No worktrees found" in app.screen.body
        await pilot.press("q")
        assert app.is_running

    run_app(git_repo, fake_cfg(), scenario)
    assert "feature/x" in run("git", "branch", cwd=git_repo)


def test_octopus_merge_background_git(git_repo: Path, fake_cfg: Callable[..., Config]) -> None:
    from project_sandbox_tui.history import PromptHistory
    from project_sandbox_tui.vcs import workspace_path
    from tests.conftest import run

    _add_git_branch(git_repo, "fix/y", "y.txt", "y\n")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        order = [b.name for b in app.branches]
        await pilot.press("v", "G", "r")  # select all three, unsupervised
        form = app.screen
        assert isinstance(form, LaunchScreen) and form.octopus is not None
        assert form.octopus.inputs == order
        result = form.query_one("#result", Input)
        assert result.value.startswith("merge/")
        prompt = form.query_one("#prompt", TextArea)
        assert prompt.text.startswith("Integrate the branches")
        form.query_one("#agent", CycleField).value = "bash"
        await pilot.pause()
        assert prompt.text.startswith("git status")
        form.query_one("#operation", CycleField).value = "rebase"
        await pilot.pause()
        assert "git diff --stat main --" in prompt.text
        form.query_one("#operation", CycleField).value = "merge"
        form.query_one("#agent", CycleField).value = "claude"
        await pilot.pause()
        assert prompt.text.startswith("Integrate the branches")
        # switching to rebase updates the untouched name and prompt
        form.query_one("#operation").focus()
        await pilot.press("l")
        await pilot.pause()
        assert result.value.startswith("rebase/") and "linear history" in prompt.text
        await pilot.press("h")
        await pilot.pause()
        assert result.value.startswith("merge/")
        # an existing branch name is refused, the form stays open
        result.value = "main"
        await pilot.press("ctrl+enter")
        assert app.screen is form
        result.value = "merge/all"
        prompt.text = prompt.text + "\nsleep:0.3"
        await pilot.press("ctrl+enter")
        await wait_until(pilot, lambda: "merge/all" in app.jobs.jobs, timeout=15)
        job = app.jobs.jobs["merge/all"]
        assert job.spec.operation == "merge" and job.spec.inputs == order
        assert "## Current state" in job.spec.prompt_text
        assert "--branch" in job.argv and "merge/all" in job.argv
        assert app.selection() == []  # cleared after launching
        await wait_until(pilot, lambda: not app.jobs.jobs["merge/all"].running, timeout=15)

    run_app(git_repo, fake_cfg(), scenario)
    wt = workspace_path(git_repo, "git", "merge/all", None)
    assert (wt / "b.txt").exists() and (wt / "y.txt").exists()  # merged on the host
    assert len(run("git", "log", "-1", "--format=%P", cwd=wt).split()) == 3  # octopus
    # history keeps what was typed, not the generated state report
    newest = PromptHistory.for_repo(git_repo).newest_first()[0]
    assert "## Current state" not in newest and newest.endswith("sleep:0.3")


def test_octopus_rebase_background_jj(jj_repo: Path, fake_cfg: Callable[..., Config]) -> None:
    from tests.conftest import run

    for name, path in (("a", "a.txt"), ("b", "b.txt")):
        run("jj", "new", "--quiet", "main", cwd=jj_repo)
        (jj_repo / path).write_text(name)
        run("jj", "commit", "--quiet", "-m", f"{name}: {path}", cwd=jj_repo)
        run("jj", "bookmark", "create", "--quiet", name, "-r", "@-", cwd=jj_repo)

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        rows = [b.name for b in app.branches]
        # the working copy row cannot be selected
        await pilot.press("space")
        assert app.selection() == []
        for name in ("a", "b"):
            app.table.move_cursor(row=rows.index(name), animate=False)
            await pilot.press("space")
        assert app.selection() == ["a", "b"]  # marking order
        await pilot.press("r")
        form = app.screen
        assert isinstance(form, LaunchScreen) and form.octopus is not None
        form.query_one("#operation").focus()
        await pilot.press("l")  # rebase
        await pilot.pause()
        assert form.query_one("#base", Input).value == "main"
        form.query_one("#prompt", TextArea).text += "\nsleep:0.2"
        await pilot.press("ctrl+enter")
        await wait_until(pilot, lambda: "rebase/a-b" in app.jobs.jobs, timeout=15)
        assert any(b.name == "rebase/a-b" for b in app.branches)
        await wait_until(pilot, lambda: not app.jobs.jobs["rebase/a-b"].running, timeout=15)

    run_app(jj_repo, fake_cfg(), scenario)
    stack = run("jj", "--ignore-working-copy", "log", "--no-graph", "-r", 'main.."rebase/a-b"',
                "-T", 'description.first_line() ++ "\\n"', cwd=jj_repo).splitlines()  # fmt: skip
    assert stack == ["b: b.txt", "a: a.txt"]


def test_octopus_interactive_resolution(
    git_repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib

    _add_git_branch(git_repo, "fix/y", "y.txt", "y\n")
    cfg = fake_cfg()
    cfg.sandbox.command = "true"  # the "agent" exits at once

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        monkeypatch.setattr(app, "suspend", contextlib.nullcontext)
        await pilot.press("v", "j", "i")
        form = app.screen
        assert isinstance(form, LaunchScreen) and form.octopus is not None
        assert "interactive session · merge 2 branches" in str(form.query_one("#title").render())
        await pilot.press("enter")  # launch from the operation field
        await wait_until(
            pilot, lambda: bool([j for j in app.jobs.jobs.values() if j.spec.operation])
        )
        job = next(j for j in app.jobs.jobs.values() if j.spec.operation)
        assert job.mode == "interactive"
        await wait_until(pilot, lambda: not job.running)
        assert app.screen is app.main

    run_app(git_repo, cfg, scenario)


def test_cleanup_force_delete(git_repo: Path, fake_cfg: Callable[..., Config]) -> None:
    from project_sandbox_tui.screens.dialogs import CleanupScreen, ConfirmScreen
    from tests.conftest import run

    dirty = git_repo.parent / "dirty"
    run("git", "worktree", "add", "--detach", str(dirty), "main", cwd=git_repo)
    (dirty / "untracked").write_text("lost")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("W")
        await wait_until(pilot, lambda: isinstance(app.screen, CleanupScreen))
        await pilot.press("D")
        await wait_until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        assert isinstance(app.screen, ConfirmScreen)
        assert "uncommitted or untracked files" in app.screen.body
        assert f"Delete directory and all contents: {dirty}" in app.screen.body
        assert "  untracked" in app.screen.body
        assert "  a.txt" in app.screen.body
        assert "Unregister worktree:" in app.screen.body
        await pilot.press("n")  # cancelled: nothing happens
        await pilot.pause(0.2)
        assert dirty.exists()

        await pilot.press("W")
        await wait_until(pilot, lambda: isinstance(app.screen, CleanupScreen))
        await pilot.press("D")
        await wait_until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await wait_until(pilot, lambda: not dirty.exists())
        assert app.is_running

    run_app(git_repo, fake_cfg(), scenario)
    assert "dirty" not in run("git", "worktree", "list", cwd=git_repo)


def test_diff_selection(git_repo: Path, fake_cfg: Callable[..., Config]) -> None:
    from project_sandbox_tui.screens.diff import DiffScreen

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j", "d")
        assert isinstance(app.screen, DiffScreen)
        assert (app.screen.base, app.screen.target) == ("main", "feature/x")
        await app.workers.wait_for_complete()
        await pilot.press("q")
        app.marked = ["feature/x"]
        await pilot.press("d")
        assert isinstance(app.screen, DiffScreen)
        assert (app.screen.base, app.screen.target) == ("main", "feature/x")
        await pilot.press("escape")
        app.marked = ["feature/x", "main"]
        await pilot.press("d")
        assert isinstance(app.screen, DiffScreen)
        assert (app.screen.base, app.screen.target) == ("feature/x", "main")
        await pilot.press("q")
        app.cfg.project.main_branch = "feature/x"
        app.marked = ["main"]
        await pilot.press("d")
        assert isinstance(app.screen, DiffScreen)
        assert (app.screen.base, app.screen.target) == ("feature/x", "main")

    run_app(git_repo, fake_cfg(), scenario)
