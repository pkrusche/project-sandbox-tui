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
from project_sandbox_tui.jobs import JobManager
from project_sandbox_tui.screens.dialogs import (
    ConfirmScreen,
    HelpScreen,
    MessageScreen,
    NewBranchScreen,
)
from project_sandbox_tui.screens.launch import CUSTOM_MODEL, LaunchScreen
from project_sandbox_tui.screens.log import LogScreen
from project_sandbox_tui.vcs import detect
from project_sandbox_tui.widgets import CycleField
from tests.conftest import add_bookmark


def run_app(
    repo: Path,
    cfg: Config,
    scenario: Callable[[SandboxTUI, Any], Coroutine[Any, Any, None]],
    *,
    size: tuple[int, int] = (120, 30),
) -> SandboxTUI:
    cfg.ui.refresh_seconds = 0.2
    cfg.ui.bell = False
    cfg.ui.notify_command = "true"  # don't pop real desktop notifications
    app = SandboxTUI(repo, detect(repo), cfg)

    async def main() -> None:
        async with app.run_test(size=size) as pilot:
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


def test_refresh_preserves_horizontal_scroll(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    detect(repo).create_branch("long/" + "bookmark" * 15, "main")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app._refresh_timer.pause()
        await pilot.press("G")
        selected = app.selected_branch_name()
        table = app.table
        assert table.max_scroll_x > 40
        for offset in (table.max_scroll_x, 20):
            table.scroll_to(x=offset, animate=False)
            await pilot.pause()
            await app.refresh_data()
            await pilot.pause()
            assert table.scroll_x == table.scroll_target_x == offset
            assert app.selected_branch_name() == selected

        app._refresh_timer.resume()
        await pilot.pause(0.6)  # periodic refresh uses the same restoration
        assert table.scroll_x == table.scroll_target_x == 20
        app._refresh_timer.pause()

        offset = table.max_scroll_x
        table.scroll_to(x=offset, animate=False)
        app.filter_text = "main"
        app._render_table()
        await pilot.pause()
        assert table.max_scroll_x < offset
        assert table.scroll_x == table.scroll_target_x == table.max_scroll_x

    run_app(repo, fake_cfg(), scenario, size=(70, 25))


@pytest.mark.parametrize("main", ["main", "release"])
def test_bookmark_comparison_markers(
    repo: Path, fake_cfg: Callable[..., Config], main: str
) -> None:
    from textual.widgets.data_table import ColumnKey

    from tests.conftest import jj

    if main != "main":
        jj(repo, "bookmark", "rename", "main", main)

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        def marker(name: str) -> str:
            return str(app.table.get_cell(name, "vs main"))

        assert str(app.table.columns[ColumnKey("vs main")].label) == f"vs {main}"
        assert app.table.columns[ColumnKey("vs main")].content_width >= len(f"vs {main}")
        assert marker("@") == "↑1"
        (repo / "working.txt").write_text("work in progress\n")
        await app.refresh_data()
        assert marker("@") == "↑1 Δ1"
        assert marker(main) == "="
        assert marker("feature/x") == "↑1 Δ1"
        jj(repo, "bookmark", "create", "old", "-r", main)
        jj(repo, "bookmark", "move", main, "--to", "feature/x")
        await app.refresh_data()
        assert marker("old") == "↓1 Δ1"
        assert marker("feature/x") == "="
        assert marker("@") == "↑1 ↓1 Δ2"
        app.filter_text = "old"
        app._render_table()
        assert marker("old") == "↓1 Δ1"  # baseline survives filtering
        app.cfg.project.main_branch = "missing"
        await app.refresh_data()
        assert marker("old") == "?"

    run_app(repo, fake_cfg(main_branch=main), scenario)


def test_list_navigation_and_filter(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        rows = row_texts(app)
        assert [r[1] for r in rows] == ["@ working copy ●", "feature/x", "main ●"]
        assert [r[0] for r in rows] == [" ", "○", "○"]  # history graph: feature/x on main
        assert "2 bookmarks ·" in str(app.main.query_one("#header").render())
        assert app.selected_branch_name() == "@"
        await pilot.press("j")
        assert app.selected_branch_name() == "feature/x"
        await pilot.press("g", "g")
        assert app.selected_branch_name() == "@"
        await pilot.press("G")
        assert app.selected_branch_name() == "main"

        await pilot.press("slash", "m", "a")
        assert isinstance(app.focused, Input)
        assert [r[1] for r in row_texts(app)] == ["main ●"]
        await pilot.press("enter")  # keep filter, back to list
        assert app.filter_text == "ma"
        await pilot.press("escape")  # clear filter
        assert len(row_texts(app)) == 3

        await pilot.press("question_mark")
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("q")  # any key closes help (and must not quit the app)
        assert not isinstance(app.screen, HelpScreen)
        assert app.is_running

        await pilot.press("slash", *"no-matches", "enter")
        assert app.table.row_count == 0 and app.selected_branch_name() is None
        await pilot.press(
            "j", "k", "G", "g", "g", "space", "v", "r", "i", "l", "f", "@", "d", "d", "equals_sign"
        )
        assert app.screen is app.main and not app.jobs.jobs
        assert app.selection() == []
        await pilot.press("escape")
        assert app.table.row_count == 3

    run_app(repo, fake_cfg(), scenario)


def test_create_branch(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("n")
        assert isinstance(app.screen, NewBranchScreen)
        assert app.screen.query_one("#start", Input).value == "main"
        await pilot.press("enter")
        assert isinstance(app.screen, NewBranchScreen)
        assert "bookmark name is empty" in str(app.screen.query_one("#error", Static).render())
        await pilot.press("b", "a", "d", "space", "x", "enter")  # invalid name
        assert isinstance(app.screen, NewBranchScreen)
        app.screen.query_one("#name", Input).value = "topic/new"
        await pilot.press("enter")
        await wait_until(pilot, lambda: app.selected_branch_name() == "topic/new")

    run_app(repo, fake_cfg(), scenario)
    assert "topic/new" in {b.name for b in detect(repo).list_branches()}


def test_undo_right_after_creating_undoes_the_creation(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("n")
        app.screen.query_one("#name", Input).value = "topic/quick"
        await pilot.press("enter", "u")  # before the creation has finished
        await app.workers.wait_for_complete()
        assert any(
            "undo creation: deleted topic/quick" in str(n.message) for n in app._notifications
        )
        assert "topic/quick" not in {b.name for b in app.backend.list_branches()}

    run_app(repo, fake_cfg(), scenario)


def test_delete_undo_and_redo_bookmarks(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    import os

    from project_sandbox_tui.jobs import JobRecord
    from project_sandbox_tui.sandbox import LaunchSpec

    def names(app: SandboxTUI) -> list[str]:
        return [r.branch.name for r in app.visible_rows()]

    def messages(app: SandboxTUI) -> list[str]:
        return [str(n.message) for n in app._notifications]

    async def vim(pilot: Any, app: SandboxTUI, *keys: str) -> None:
        await pilot.press(*keys)
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app._prefix.TIMEOUT = 60  # dd spans waits for workers; only other keys cancel it here
        feature = app.backend._bookmark_commit("feature/x")
        # n records a lineage started from main; its creation can be undone.
        await pilot.press("n")
        app.screen.query_one("#name", Input).value = "topic/new"
        app.screen.query_one("#start", Input).value = "feature/x"
        await vim(pilot, app, "enter")
        assert names(app) == ["@", "topic/new", "feature/x", "main"]
        assert row_texts(app)[1][0] == "○"
        assert app.graph.state.bookmark_parents(names(app)[1:])["topic/new"] == ["feature/x"]

        await vim(pilot, app, "d", "j", "d")  # another key in between cancels dd
        assert names(app) == ["@", "topic/new", "feature/x", "main"]
        await vim(pilot, app, "d")  # the second d in a row: feature/x, keeping its history
        assert names(app) == ["@", "topic/new", "main"]
        assert any("deleted feature/x (u restores)" in m for m in messages(app))
        # topic/new still connects to main through the deleted bookmark.
        assert app.graph.state.bookmark_parents(names(app)[1:])["topic/new"] == ["main"]

        await vim(pilot, app, "u")
        assert names(app) == ["@", "topic/new", "feature/x", "main"]
        assert app.backend._bookmark_commit("feature/x") == feature
        assert app.selected_branch_name() == "feature/x"
        assert app.graph.state.bookmark_parents(names(app)[1:])["topic/new"] == ["feature/x"]
        await vim(pilot, app, "u")  # undo n
        assert names(app) == ["@", "feature/x", "main"]
        await vim(pilot, app, "u")
        assert "nothing to undo" in messages(app)[-1]
        await vim(pilot, app, "ctrl+r", "ctrl+r")  # redo n, then the deletion
        assert names(app) == ["@", "topic/new", "main"]
        await vim(pilot, app, "ctrl+r")
        assert "nothing to redo" in messages(app)[-1]
        # Another instance sees the same history and can undo there.
        assert SandboxTUI(repo, detect(repo), app.cfg).graph.state.undo_entry() is not None

        # Refused: the main bookmark, the working copy and busy bookmarks.
        await vim(pilot, app, "G", "d", "d")
        await vim(pilot, app, "g", "g", "d", "d")
        assert "main is the main bookmark; not deleted" in messages(app)
        assert "the working copy is not a bookmark" in messages(app)
        app.jobs.save(
            JobRecord(
                branch="topic/new",
                spec=LaunchSpec(branch="topic/new", agent="claude"),
                pid=os.getpid(),
                started_at="2026-01-01T00:00:00Z",
            )
        )
        await vim(pilot, app, "j", "d", "d")
        assert "topic/new" in names(app)
        assert any("not deleted: topic/new already has" in m for m in messages(app))
        await vim(pilot, app, "u")  # restoring feature/x is fine…
        assert "undo deletion: restored feature/x" in messages(app)[-1]
        await vim(pilot, app, "u")  # …but undoing the busy bookmark's creation is not
        assert messages(app)[-1].startswith("undo failed: topic/new already has")
        assert names(app) == ["@", "topic/new", "feature/x", "main"]
        assert app.graph.state.position == 1

    run_app(repo, fake_cfg(), scenario)


@pytest.mark.parametrize("marked", [False, True])
def test_fast_forward_main_shortcut(
    repo: Path, fake_cfg: Callable[..., Config], marked: bool
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        if marked:
            app.marked = ["feature/x"]  # selection wins over the highlighted working copy
        else:
            await pilot.press("j")
        target = app.backend._bookmark_commit("feature/x")
        await pilot.press("f")
        await app.workers.wait_for_complete()
        assert app.backend._bookmark_commit("main") == target
        by_name = {b.name: b for b in app.branches}
        assert by_name["main"].head == by_name["feature/x"].head
        assert any("fast-forwarded main to feature/x" in str(n.message) for n in app._notifications)
        await pilot.press("f")
        await app.workers.wait_for_complete()
        assert any("already points to" in str(n.message) for n in app._notifications)

    run_app(repo, fake_cfg(), scenario)


@pytest.mark.parametrize("case", ["multiple", "missing-main", "configured", "diverged"])
def test_fast_forward_main_guards_and_configuration(
    repo: Path, fake_cfg: Callable[..., Config], case: str
) -> None:
    if case == "diverged":
        add_bookmark(repo, "side", "side.txt", "side\n")
        from tests.conftest import jj

        jj(repo, "bookmark", "move", "main", "--to", "side")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        expected = {
            "multiple": "select one target",
            "missing-main": "no main bookmark found",
            "configured": "fast-forwarded feature/x to next",
            "diverged": "not an ancestor",
        }[case]
        old = app.backend._bookmark_commit("main")
        if case == "multiple":
            app.marked = ["main", "feature/x"]
        elif case == "missing-main":
            app.cfg.project.main_branch = "missing"
            app.marked = ["feature/x"]
        elif case == "configured":
            app.cfg.project.main_branch = "feature/x"
            add_bookmark(repo, "next", "next.txt", "next\n", start="feature/x")
            await app.refresh_data()
            app.marked = ["next"]
        elif case == "diverged":
            app.marked = ["feature/x"]
        await pilot.press("f")
        await app.workers.wait_for_complete()
        assert any(expected in str(n.message) for n in app._notifications)
        assert app.backend._bookmark_commit("main") == old
        if case == "configured":
            assert app.backend._bookmark_commit("feature/x") == app.backend._bookmark_commit("next")

    run_app(repo, fake_cfg(), scenario)


def test_fast_forward_main_to_working_copy(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        (repo / "a.txt").write_text("working copy edit\n")
        await pilot.press("f")
        await app.workers.wait_for_complete()
        assert (
            app.backend._bookmark_commit("main")
            == app.backend._jj("log", "--no-graph", "-r", "@", "-T", "commit_id").strip()
        )
        assert any("fast-forwarded main to @" in str(n.message) for n in app._notifications)

    run_app(repo, fake_cfg(), scenario)


@pytest.mark.parametrize("marked", [False, True])
def test_move_working_copy_shortcut(
    repo: Path, fake_cfg: Callable[..., Config], marked: bool
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        if marked:
            app.marked = ["feature/x"]
        else:
            await pilot.press("j")
        await pilot.press("@")
        await app.workers.wait_for_complete()
        assert app.backend._jj("log", "--no-graph", "-r", "@-", "-T", "commit_id").strip() == (
            app.backend._bookmark_commit("feature/x")
        )
        assert any(b.name.startswith("saved/") for b in app.branches)
        assert any(
            "working copy moved onto feature/x" in str(n.message) for n in app._notifications
        )

    run_app(repo, fake_cfg(), scenario)


@pytest.mark.parametrize("case", ["working-copy", "multiple"])
def test_move_working_copy_shortcut_requires_one_bookmark(
    repo: Path, fake_cfg: Callable[..., Config], case: str
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        old = app.backend._jj("log", "--no-graph", "-r", "@", "-T", "commit_id")
        if case == "multiple":
            app.marked = ["main", "feature/x"]
        await pilot.press("@")
        await app.workers.wait_for_complete()
        assert app.backend._jj("log", "--no-graph", "-r", "@", "-T", "commit_id") == old
        assert any("select" in str(n.message) for n in app._notifications)

    run_app(repo, fake_cfg(), scenario)


@pytest.mark.parametrize("source", ["job", "session"])
def test_move_working_copy_refuses_running_session(
    repo: Path, fake_cfg: Callable[..., Config], source: str
) -> None:
    import json
    import os

    from project_sandbox_tui.jobs import JobManager
    from project_sandbox_tui.sandbox import LaunchSpec

    cfg = fake_cfg()

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        old = app.backend._jj("log", "--no-graph", "-r", "@", "-T", "commit_id")
        if source == "job":
            other = JobManager(repo, cfg)
            other.record_external(LaunchSpec(branch="@", agent="codex"), ["other"])
        else:
            app.sessions_dir = repo.parent / "sessions"
            app.sessions_dir.mkdir()
            (app.sessions_dir / "active.json").write_text(
                json.dumps({"status": "running", "pid": os.getpid(), "workspace_path": str(repo)})
            )
        app.marked = ["feature/x"]
        # Call directly so the guard must read the new records without a UI refresh.
        await app._move_working_copy("feature/x").wait()
        assert app.backend._jj("log", "--no-graph", "-r", "@", "-T", "commit_id") == old
        assert not any(b.name.startswith("saved/") for b in app.branches)
        assert any("already has" in str(n.message) for n in app._notifications)

    run_app(repo, cfg, scenario)


@pytest.mark.parametrize("mode", ["r", "i"])
@pytest.mark.parametrize("multiple", [False, True])
def test_launch_inputs_remain_visible_when_focused(
    repo: Path, fake_cfg: Callable[..., Config], mode: str, multiple: bool
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        if multiple:
            await pilot.press("v", "G")
        await pilot.press(mode)
        form = app.screen
        assert isinstance(form, LaunchScreen)
        form.query_one("#model", CycleField).value = CUSTOM_MODEL
        await pilot.pause()
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

    run_app(repo, fake_cfg(), scenario)


@pytest.mark.parametrize("mode", ["r", "i"])
def test_launch_form_shows_base_branch(
    repo: Path, fake_cfg: Callable[..., Config], mode: str
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j")
        selected = app.selected_branch_name()
        await pilot.press(mode)
        form = app.screen
        assert isinstance(form, LaunchScreen)
        assert str(form.query_one("#start-branch", Static).render()) == selected

    run_app(repo, fake_cfg(), scenario)


def test_background_run_notifies_and_shows_log(repo: Path, fake_cfg: Callable[..., Config]) -> None:
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
        model = form.query_one("#model", CycleField)
        model.focus()
        await pilot.press("left", "tab")  # custom model
        await pilot.press(*"gpt-x")
        preview = str(form.query_one("#preview").render())
        assert "--agent=codex" in preview and "--prompt-text" in preview
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
        assert row[0] == "✓"
        assert row[5].startswith("completed · ") and row[5].endswith(" ago")
        # The history records the outcome once a listing after the run's end is seen.
        await wait_until(pilot, lambda: app.graph.state.history("feature/x")[0].status != "")
        await pilot.press("P")  # the agent moved from the list to the details
        assert isinstance(app.screen, MessageScreen)
        assert "last run    completed (exit 0)" in app.screen.body
        assert "codex/gpt-x · background" in app.screen.body
        assert "background codex/gpt-x → completed: sleep:0.6 exit:0" in app.screen.body
        await pilot.press("q")

        await pilot.press("l")
        assert isinstance(app.screen, LogScreen)
        await pilot.pause(0.2)
        assert app.screen.current is not None and app.screen.current.suffix == ".md"
        await pilot.press("q")
        assert not isinstance(app.screen, LogScreen)

        await pilot.press("x")  # a finished run is cleared at once
        assert "feature/x" not in app.jobs.jobs
        # The history keeps the run, with the commit it ended on.
        (run,) = [n for n in app.graph.state.history("feature/x") if n.kind == "run"]
        assert run.status == "completed" and run.agent == "codex/gpt-x"
        assert run.commit == app.backend._bookmark_commit("feature/x")

    run_app(repo, cfg, scenario)
    # the choice became the new default for this repo
    last = LastUsed.load(repo)
    assert last.agent == "codex"
    assert last.model_by_agent["codex"] == "gpt-x"


def test_quit_without_runs_exits(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("q")
        await wait_until(pilot, lambda: not app.is_running)

    run_app(repo, fake_cfg(), scenario)


@pytest.mark.parametrize("confirm", [("y",), ("ctrl+q",)])
def test_quit_confirms_while_runs_continue(
    repo: Path, fake_cfg: Callable[..., Config], confirm: tuple[str, ...]
) -> None:
    from project_sandbox_tui.sandbox import LaunchSpec

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app.jobs.record_external(LaunchSpec(branch="feature/x", agent="codex"), ["other"])
        await app.refresh_data()
        for key in ("q", "ctrl+q"):
            await pilot.press(key)
            assert isinstance(app.screen, ConfirmScreen)
            assert "feature/x  codex" in app.screen.body
            await pilot.press("n")  # any other key cancels
            assert app.screen is app.main and app.is_running
        await pilot.press("q", *confirm)
        await wait_until(pilot, lambda: not app.is_running)

    run_app(repo, fake_cfg(), scenario)
    assert JobManager(repo, fake_cfg()).running_count() == 1  # quitting stops nothing


def test_launch_rejects_model_name_with_spaces(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("i")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        form.query_one("#model", CycleField).value = CUSTOM_MODEL
        form._update_custom_model()
        form.query_one("#custom-model", Input).value = "my model"
        form.action_launch()
        await pilot.pause()
        assert app.screen is form
        assert form.focused is form.query_one("#custom-model")
        assert any("invalid model name" in str(n.message) for n in app._notifications)

    run_app(repo, fake_cfg(), scenario)


def test_launch_form_keys_do_not_trigger_list_actions(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("i")
        assert isinstance(app.screen, LaunchScreen)
        await pilot.press("q", "r", "n")  # on the agent CycleField: no quit / no new form
        assert isinstance(app.screen, LaunchScreen) and app.is_running
        await pilot.press("escape")
        assert not isinstance(app.screen, LaunchScreen)

    run_app(repo, fake_cfg(), scenario)


def test_missing_image_config_is_reported(repo: Path) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("r")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        assert "no image configured" in str(form.query_one("#preview").render())
        form.query_one("#prompt", TextArea).text = "go"
        await pilot.press("ctrl+enter")
        await pilot.pause(0.1)
        assert "@" not in app.jobs.jobs

    run_app(repo, Config(), scenario)
    assert LastUsed.load(repo).agent == ""  # failed launch did not become the default


def test_filter_box_is_visible_above_footer(repo: Path, fake_cfg: Callable[..., Config]) -> None:
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

    run_app(repo, fake_cfg(), scenario)


def test_enter_does_not_launch_unsupervised_run(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("r")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        area = form.query_one("#prompt", TextArea)
        area.text = "sleep:0.1"
        area.move_cursor(area.document.end)
        await pilot.press("enter")
        assert app.screen is form
        assert area.text == "sleep:0.1\n"
        form.query_one("#timeout", Input).focus()
        await pilot.press("enter")
        assert app.screen is form  # still in the form; focus moved on
        await pilot.press("escape")

    run_app(repo, fake_cfg(), scenario)


def test_unsupervised_run_launches_with_ctrl_s_or_button(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("r")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        form.query_one("#prompt", TextArea).text = "sleep:0.1"
        await pilot.click("#launch")
        assert not isinstance(app.screen, LaunchScreen)
        await wait_until(pilot, lambda: "@" in app.jobs.jobs and not app.jobs.jobs["@"].running)

        await pilot.press("r")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        form.query_one("#prompt", TextArea).text = "sleep:0.1"
        await pilot.press("ctrl+s")
        assert not isinstance(app.screen, LaunchScreen)

    run_app(repo, fake_cfg(), scenario)


def test_external_launch_rechecks_busy_state(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    from project_sandbox_tui.jobs import JobManager
    from project_sandbox_tui.sandbox import LaunchSpec

    cfg = fake_cfg()
    cfg.ui.terminal_command = "true {cmd}"

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("i")
        assert isinstance(app.screen, LaunchScreen)
        other = JobManager(repo, cfg)
        reserved = other.record_external(LaunchSpec(branch="@", agent="codex"), ["other"])
        await pilot.press("enter")
        assert app.jobs.jobs["@"].started_at == reserved.started_at
        assert app.jobs.jobs["@"].spec.agent == "codex"
        assert LastUsed.load(repo).agent == ""

    run_app(repo, cfg, scenario)


def test_failed_external_launch_releases_reservation(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    cfg = fake_cfg()
    cfg.ui.terminal_command = "definitely-not-an-installed-terminal {cmd}"

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("i", "enter")
        assert "@" not in app.jobs.jobs
        assert LastUsed.load(repo).agent == ""

    run_app(repo, cfg, scenario)


def test_jj_repo_without_bookmarks_runs_in_working_copy(
    jj_repo_no_bookmarks: Path, fake_cfg: Callable[..., Config]
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        assert [r[1] for r in row_texts(app)] == ["@ working copy ●"]
        await pilot.press("r")
        form = app.screen
        assert isinstance(form, LaunchScreen) and form.current_spec().main_checkout
        assert form.query_one("#branch", Input).value == "@"
        form.query_one("#prompt", TextArea).text = "sleep:3"
        await pilot.press("ctrl+enter")
        await wait_until(pilot, lambda: "@" in app.jobs.jobs)
        assert not [a for a in app.jobs.jobs["@"].argv if a.startswith("--branch")]
        await wait_until(pilot, lambda: "@" in app.sessions)  # session in the repo itself
        app.jobs.cancel("@", app.sessions)
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
        assert isinstance(form, LaunchScreen) and form.current_spec().main_checkout
        form.query_one("#prompt", TextArea).text = "sleep:3"
        await pilot.press("ctrl+enter")
        await wait_until(pilot, lambda: "@" in app.jobs.jobs)
        assert not [a for a in app.jobs.jobs["@"].argv if a.startswith("--branch")]
        await wait_until(pilot, lambda: "@" in app.sessions)
        assert "main" not in app.sessions  # the bookmark stays free for its own sandbox

        await pilot.press("j", "r")  # on the bookmark: a separate jj workspace via --branch
        form = app.screen
        assert isinstance(form, LaunchScreen) and not form.current_spec().main_checkout
        form.query_one("#prompt", TextArea).text = "sleep:0.1"
        await pilot.press("ctrl+enter")
        await wait_until(pilot, lambda: "main" in app.jobs.jobs)
        argv = app.jobs.jobs["main"].argv
        assert "--branch=main" in argv

        app.jobs.cancel("@", app.sessions)
        await wait_until(pilot, lambda: not app.jobs.jobs["@"].running)

    run_app(jj_repo, fake_cfg(), scenario)


def test_prompt_history_completion_browse_and_search(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    from project_sandbox_tui.history import PromptHistory
    from project_sandbox_tui.screens.history import HistorySearchScreen
    from project_sandbox_tui.widgets import PromptArea

    history = PromptHistory.for_repo(repo)
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

    run_app(repo, fake_cfg(), scenario)
    assert PromptHistory.for_repo(repo).newest_first()[0] == "sleep:0.1 brand new prompt"


def test_cheatsheet_shown_on_start_and_toggles(repo: Path, fake_cfg: Callable[..., Config]) -> None:
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

    run_app(repo, fake_cfg(), scenario)

    cfg = fake_cfg()
    cfg.ui.cheatsheet = False

    async def hidden(app: SandboxTUI, pilot: Any) -> None:
        assert app.main.query_one("#cheatsheet").region.height == 0

    run_app(repo, cfg, hidden)


@pytest.mark.parametrize("size", [(80, 24), (40, 12), (120, 12)])
def test_small_window_keeps_bookmarks_and_help_visible(
    repo: Path, fake_cfg: Callable[..., Config], size: tuple[int, int]
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        sheet = app.main.query_one("#cheatsheet")
        footer = app.main.query_one("#footer", Static)
        assert sheet.region.height == 0
        assert footer.region.height == 1
        assert footer.region.bottom <= app.main.size.height
        assert app.table.region.height >= 4  # header, @, bookmark, scrollbar
        assert "? full help" in app.export_screenshot().replace("&#160;", " ")
        assert "main" in app.export_screenshot()

        await pilot.press("slash", "m")
        assert app.main.query_one("#filter").region.height == 1
        assert app.table.region.height >= 4
        assert "main" in app.export_screenshot()
        await pilot.press("escape", "question_mark")
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        assert app.is_running

        await pilot.resize_terminal(120, 40)
        await pilot.pause()
        assert sheet.region.height > 1  # expands again without changing the preference
        assert footer.region.height == 0
        await pilot.press("H")
        await pilot.resize_terminal(*size)
        await pilot.resize_terminal(120, 40)
        assert sheet.region.height == 0  # an explicit hide survives resizing
        assert footer.region.height == 1

    run_app(repo, fake_cfg(), scenario, size=size)


@pytest.mark.parametrize("terminal", ["inline", "tmux", "zellij", "screen"])
def test_enter_that_launches_interactive_session_does_not_leak(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch, terminal: str
) -> None:
    """Regression: the Enter that submitted the launch form used to bubble up to the
    App after the form was popped, where it matched the branch list's Enter binding
    and opened the log screen, so after the session the TUI looked frozen."""
    import contextlib

    from project_sandbox_tui.screens.log import LogScreen

    cfg = fake_cfg()
    cfg.sandbox.command = "true"  # a real child that exits 0 straight away
    cfg.ui.terminal = terminal
    for variable in ("TMUX", "ZELLIJ", "STY"):
        monkeypatch.delenv(variable, raising=False)

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        # headless drivers cannot suspend; the session itself is what we stub out
        monkeypatch.setattr(app, "suspend", contextlib.nullcontext)
        await pilot.press("i")
        assert isinstance(app.screen, LaunchScreen)
        assert "opens in" not in str(app.screen.query_one(".title").render())
        await pilot.press("enter")  # launch from the agent field
        await wait_until(pilot, lambda: "@" in app.jobs.jobs)
        await wait_until(pilot, lambda: not app.jobs.jobs["@"].running)
        assert not app.jobs.jobs["@"].external
        assert app.jobs.jobs["@"].exit_code == 0
        await pilot.pause(0.3)
        assert app.screen is app.main, f"stray screen after session: {app.screen!r}"
        assert not isinstance(app.screen, LogScreen)
        await pilot.press("question_mark")  # and the list still takes keys
        assert isinstance(app.screen, HelpScreen)

    run_app(repo, cfg, scenario)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_interactive_session_in_tmux_window(
    repo: Path, fake_cfg: Callable[..., Config], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess
    import sys

    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))  # private tmux server
    monkeypatch.delenv("TMUX", raising=False)
    subprocess.run(["tmux", "new-session", "-d", "-s", "host"], check=True)
    agent = Path(__file__).with_name("fake_interactive_agent.py")
    cfg = fake_cfg()
    cfg.sandbox.command = f"{sys.executable} {agent}"
    cfg.ui.terminal = "tmux"
    name = "host:main"

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j", "i")  # main
        form = app.screen
        assert isinstance(form, LaunchScreen)
        assert "opens in tmux" in str(form.query_one(".title").render())
        await pilot.press("enter")
        await wait_until(pilot, lambda: "main" in app.jobs.jobs)
        job = app.jobs.jobs["main"]
        assert job.external and job.running

        def window_exists() -> bool:
            return (
                "main"
                in subprocess.check_output(
                    ["tmux", "list-windows", "-t", "host", "-F", "#{window_name}"], text=True
                ).splitlines()
            )

        await wait_until(pilot, window_exists)
        assert app.screen is app.main  # the TUI is not suspended
        # a second interactive launch on the branch is refused while it runs
        await pilot.press("i")
        assert not isinstance(app.screen, LaunchScreen)
        subprocess.run(["tmux", "send-keys", "-t", name, "q"], check=True)  # quit the agent
        await wait_until(pilot, lambda: not window_exists())

    try:
        address = subprocess.check_output(
            ["tmux", "display-message", "-p", "#{socket_path},#{pid},0"], text=True
        ).strip()
        monkeypatch.setenv("TMUX", address)
        run_app(repo, cfg, scenario)
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)


def test_reload_config(repo: Path, tmp_path: Path) -> None:
    from project_sandbox_tui.config import ProjectTrust, load

    global_path = tmp_path / "custom.toml"
    global_path.write_text('[defaults]\nagent = "codex"\n')
    project_path = repo / ".project-sandbox-tui.toml"
    project_path.write_text('[project]\nbase_image = "old"\n')
    ProjectTrust.check(repo).trust(repo)
    cfg = load(repo, global_path)
    cfg.sandbox.extra_args.append("--dry-run")
    app = SandboxTUI(repo, detect(repo), cfg, config_path=global_path, dry_run=True)

    async def scenario() -> None:
        async with app.run_test(size=(120, 30)) as pilot:
            old_timer = app._refresh_timer
            global_path.write_text('[defaults]\nagent = "bash"\n')
            project_path.write_text(
                '[project]\nbase_image = "new"\ndefault_branch_start = "feature/x"\n'
                "[ui]\nrefresh_seconds = 7\ncheatsheet = false\n"
            )
            # A changed project config is reviewed first; declining keeps the trusted copy.
            await pilot.press("ctrl+l")
            assert isinstance(app.screen, ConfirmScreen)
            assert '+base_image = "new"' in app.screen.body
            await pilot.press("n")
            await pilot.pause()
            assert app.cfg.defaults.agent == "bash"  # the global config is not reviewed
            assert app.cfg.project.base_image == "old"
            assert any("changed since it was trusted" in str(n.message) for n in app._notifications)

            await pilot.press("ctrl+l")
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("y")
            await pilot.pause()
            assert app.cfg.project.base_image == "new"
            assert app.cfg.sandbox.extra_args == ["--dry-run"]
            assert app.jobs.cfg is app.cfg
            assert app.default_start == "feature/x"
            assert not app.cheatsheet_visible
            assert app._refresh_timer is not old_timer
            assert app.cfg.ui.refresh_seconds == 7
            good_cfg, good_timer = app.cfg, app._refresh_timer
            project_path.write_text("[ui]\nrefresh_seconds = 0\n")
            await pilot.press("ctrl+l")  # invalid: not offered; the trusted copy stays in effect
            assert not isinstance(app.screen, ConfirmScreen)
            assert app.cfg.ui.refresh_seconds == 7
            assert any("Project config not trusted" in str(n.message) for n in app._notifications)
            good_cfg, good_timer = app.cfg, app._refresh_timer
            global_path.write_text("[ui]\nrefresh_seconds = 0\n")
            project_path.write_text('[project]\nbase_image = "new"\n')
            ProjectTrust.check(repo).trust(repo)
            await pilot.press("ctrl+l")
            assert app.cfg is good_cfg
            assert app.jobs.cfg is good_cfg
            assert app._refresh_timer is good_timer
            assert any("Config reload failed" in str(n.message) for n in app._notifications)

    asyncio.run(scenario())


def test_untrusted_project_config_is_reviewed_at_start(repo: Path) -> None:
    from project_sandbox_tui.config import ProjectTrust, load

    (repo / ".project-sandbox-tui.toml").write_text('[project]\nbase_image = "img"\n')
    app = SandboxTUI(repo, detect(repo), load(repo))
    assert not app.cfg.has_image_source()

    async def scenario() -> None:
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            assert "can set commands that run on this machine" in app.screen.body
            await pilot.press("y")
            await pilot.pause()
            assert app.cfg.project.base_image == "img"
            assert not ProjectTrust.check(repo).needs_review

    asyncio.run(scenario())


def test_modifier_hints(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("left_control")
        assert "ctrl+l reload" in str(app.main.query_one("#cheatsheet", Static).content)
        await pilot.press("right_shift")
        assert "G bottom" in str(app.main.query_one("#cheatsheet", Static).content)
        await pilot.press("left_alt")
        assert "no shortcuts" in str(app.main.query_one("#footer", Static).content)
        await pilot.press("r")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        await pilot.press("right_control")
        hint = str(form.query_one("#launch-hint", Static).content)
        assert "ctrl+s / ctrl+enter launch" in hint and "ctrl+r search" in hint
        await pilot.press("left_shift")
        assert "shift+tab" in str(form.query_one("#launch-hint", Static).content)
        await pilot.press("a")
        assert "ctrl+s launch" in str(form.query_one("#launch-hint", Static).content)
        await pilot.press("escape", "question_mark", "left_control")
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("q")
        assert app.screen is app.main


# ---------------------------------------------------------------- octopus


@pytest.mark.parametrize("visual_key", ["v", "V"])
def test_selection_keys(repo: Path, fake_cfg: Callable[..., Config], visual_key: str) -> None:
    add_bookmark(repo, "fix/y", "y.txt", "y\n")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        names = [r[1] for r in row_texts(app)]
        assert len(names) == 4
        await pilot.press("space")  # the working copy cannot be marked
        assert app.selection() == []
        await pilot.press("j", "space")  # mark the first bookmark and move down
        assert app.selection() == [app.visible_rows()[1].branch.name]
        assert row_texts(app)[1][1].startswith("+ ")
        await pilot.press("space")
        assert len(app.selection()) == 2
        assert "2 selected" in str(app.main.query_one("#header").render())
        await pilot.press("escape")
        assert app.selection() == []
        # visual range: v at the top, G extends to the bottom
        await pilot.press("g", "g", visual_key, "G")
        assert len(app.selection()) == 3
        assert "VISUAL" in str(app.main.query_one("#header").render())
        await pilot.press(visual_key)  # end visual, keep the range as marks
        await pilot.press("g", "g")
        assert len(app.selection()) == 3 and app._visual_anchor is None
        await pilot.press("escape")
        assert app.selection() == []
        # one mark launches on it, as f and @ act on it, instead of a merge
        await pilot.press("j", "space", "r")
        assert isinstance(app.screen, LaunchScreen) and app.screen.octopus is None
        assert app.screen.query_one("#branch", Input).value == app.visible_rows()[1].branch.name

    run_app(repo, fake_cfg(), scenario)


@pytest.mark.parametrize("keys", [("v", "j"), ("space", "space"), ()])
def test_cancel_clears_finished_and_forgets_on_third_press(
    repo: Path, fake_cfg: Callable[..., Config], keys: tuple[str, ...]
) -> None:
    import os

    from project_sandbox_tui.jobs import JobManager, JobRecord
    from project_sandbox_tui.sandbox import LaunchSpec

    add_bookmark(repo, "fix/y", "y.txt", "y\n")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        names = [r.branch.name for r in app.visible_rows() if not r.branch.working_copy]
        for i, name in enumerate(names):
            app.jobs.save(
                JobRecord(
                    branch=name,
                    # An interactive record: x never signals the TUI holding it.
                    spec=LaunchSpec(branch=name, agent="claude", mode="interactive"),
                    pid=os.getpid(),
                    started_at="2026-01-01T00:00:00Z",
                    status="running" if i == 0 else "completed",
                    notified=True,
                )
            )
        await app.refresh_data()
        await pilot.press("j", *keys)  # skip the unmarkable working-copy row
        chosen = names[:2] if keys else names[:1]
        app.jobs.STOP_DEBOUNCE_S = 0
        await pilot.press("x")
        # A running record must not prevent later selected records from clearing.
        remaining = set(names) - (set(chosen) - {names[0]})
        assert set(app.jobs.jobs) == remaining
        assert set(JobManager(repo, app.cfg).jobs) == remaining
        assert any("x 2 more times forgets" in str(n.message) for n in app._notifications)
        for row in app.visible_rows():
            assert (row.job is None) == (row.branch.name not in remaining)

        await pilot.press("x")
        assert names[0] in app.jobs.jobs
        await pilot.press("x")
        assert set(app.jobs.jobs) == set(names) - set(chosen)
        assert set(JobManager(repo, app.cfg).jobs) == set(names) - set(chosen)
        assert any("no process was signalled" in str(n.message) for n in app._notifications)

    run_app(repo, fake_cfg(), scenario)


@pytest.mark.parametrize("keys", [("v", "j"), ("space", "space"), ()])
def test_cancel_selected_runs(
    repo: Path,
    fake_cfg: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
    keys: tuple[str, ...],
) -> None:
    import signal

    from project_sandbox_tui.jobs import JobRecord
    from project_sandbox_tui.sandbox import LaunchSpec

    add_bookmark(repo, "fix/y", "y.txt", "y\n")
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr("project_sandbox_tui.jobs.pid_alive", lambda pid: True)
    monkeypatch.setattr("os.killpg", lambda pid, sig: signals.append((pid, sig)))

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        names = [r.branch.name for r in app.visible_rows() if not r.branch.working_copy]
        pids = {name: 1_000_000 + i for i, name in enumerate(names)}
        for name in names:
            app.jobs.save(
                JobRecord(
                    branch=name,
                    spec=LaunchSpec(branch=name, agent="claude", mode="background"),
                    pid=pids[name],
                    started_at="2026-01-01T00:00:00Z",
                )
            )
        await app.refresh_data()
        await pilot.press("j", *keys)  # skip the unmarkable working-copy row
        chosen = names[:2] if keys else names[:1]
        if keys:  # several runs at once: ask before the first signal
            await pilot.press("x")
            assert isinstance(app.screen, ConfirmScreen) and signals == []
            await pilot.press("n")
            assert app.screen is app.main and signals == []
            await pilot.press("x")
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("y")
        else:
            await pilot.press("x")
        assert signals == [(pids[name], signal.SIGINT) for name in chosen]
        assert app.selection() == (chosen if keys else [])
        # Retain the selection so escalation reaches the same runs.
        monkeypatch.setattr(app.jobs, "STOP_DEBOUNCE_S", 0)
        await pilot.press("x")
        assert signals[len(chosen) :] == [(pids[name], signal.SIGTERM) for name in chosen]
        await pilot.press("x")
        assert signals[2 * len(chosen) :] == [(pids[name], signal.SIGKILL) for name in chosen]
        assert set(app.jobs.jobs) == set(names) - set(chosen)  # and forgotten

    run_app(repo, fake_cfg(), scenario)


def test_open_selected_workspaces(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib

    from project_sandbox_tui.vcs import workspace_path

    add_bookmark(repo, "missing", "missing.txt", "missing\n")
    feature_path = workspace_path(repo, "feature/x", None)
    feature_path.mkdir(parents=True)
    main_path = workspace_path(repo, "main", None)
    main_path.mkdir()
    opened: list[tuple[list[str], Path]] = []

    def editor(argv: list[str], *, cwd: Path) -> int:
        opened.append((argv, cwd))
        return 0

    monkeypatch.setenv("VISUAL", "test-editor")
    monkeypatch.setattr("subprocess.call", editor)
    monkeypatch.setattr("project_sandbox_tui.terminal.preserved", contextlib.nullcontext)

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        monkeypatch.setattr(app, "_suspended", contextlib.nullcontext)
        app.marked = ["missing", "feature/x", "main"]
        await pilot.press("o")
        assert opened == [
            (["test-editor", str(feature_path)], feature_path),
            (["test-editor", str(main_path)], main_path),
        ]
        assert any("missing: no workspace yet" in str(n.message) for n in app._notifications)

        # A bookmark on @'s parent still uses its own workspace; only @ opens the repo.
        main_path.rmdir()
        app.marked = ["main"]
        await pilot.press("o")
        assert len(opened) == 2
        assert any("main: no workspace yet" in str(n.message) for n in app._notifications)
        await pilot.press("escape", "g", "g", "o")
        assert opened[-1] == (["test-editor", str(repo)], repo)

    run_app(repo, fake_cfg(), scenario)


def test_log_and_preview_respect_selection(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    from textual.containers import VerticalScroll

    from project_sandbox_tui.jobs import JobRecord
    from project_sandbox_tui.sandbox import LaunchSpec
    from project_sandbox_tui.screens.dialogs import MessageScreen

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app.jobs.save(
            JobRecord(
                branch="main",
                spec=LaunchSpec(branch="main", agent="claude"),
                pid=0,
                started_at="2026-01-01T00:00:00Z",
                status="completed",
                notified=True,
            )
        )
        await app.refresh_data()
        await pilot.press("G", "space", "k")  # main selected; cursor on feature/x
        await pilot.press("l")
        assert isinstance(app.screen, LogScreen) and app.screen.job.branch == "main"
        await pilot.press("q", "P")
        assert isinstance(app.screen, MessageScreen)
        assert app.screen.title_text == "main"
        assert "last run    completed" in app.screen.body
        assert "interactive command\n" in app.screen.body
        assert "--branch=feature/x" not in app.screen.body
        await pilot.press("q", "space")  # both branches marked
        for key in ("l", "enter"):
            await pilot.press(key)
            assert app.screen is app.main
            assert any("select one bookmark" in str(n.message) for n in app._notifications)
        app.cfg.sandbox.extra_args = ["--example"] * 200
        await pilot.press("P")
        assert isinstance(app.screen, MessageScreen)
        assert "── main ──\n" in app.screen.body and "── feature/x ──\n" in app.screen.body
        assert "--branch=feature/x" in app.screen.body
        await pilot.press("end")
        await pilot.pause()
        assert isinstance(app.screen, MessageScreen)
        assert app.screen.query_one(VerticalScroll).scroll_y > 0
        await pilot.press("q")
        assert app.screen is app.main and len(app.selection()) == 2

    run_app(repo, fake_cfg(), scenario)


@pytest.mark.parametrize("target", ["@", "main", "topic/from-form"])
def test_launch_branch_field(repo: Path, fake_cfg: Callable[..., Config], target: str) -> None:
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
        assert form.current_spec().main_checkout == (target == "@")
        await pilot.press("ctrl+enter")
        await wait_until(pilot, lambda: target in app.jobs.jobs)
        assert app.jobs.jobs[target].spec.branch == target
        if target.startswith("topic/"):
            branches = {b.name: b for b in detect(repo).list_branches()}
            assert branches[target].head == branches["feature/x"].head

    run_app(repo, fake_cfg(), scenario)


def test_launch_form_rejects_empty_bookmark(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j", "r")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        form.query_one("#branch", Input).value = "  "
        form.query_one("#prompt", TextArea).text = "exit:0"
        await pilot.press("ctrl+enter")
        assert app.screen is form
        assert any("bookmark name is empty" in str(n.message) for n in app._notifications)

    run_app(repo, fake_cfg(), scenario)


def test_launch_command_preview_scrolls(repo: Path, fake_cfg: Callable[..., Config]) -> None:
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

    run_app(repo, fake_cfg(), scenario)


def test_cleanup_workspaces_key(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    from project_sandbox_tui.screens.dialogs import CleanupScreen, MessageScreen
    from project_sandbox_tui.vcs import workspace_path
    from tests.conftest import run

    path = workspace_path(repo, "feature/x", None)
    path.parent.mkdir()
    run("jj", "workspace", "add", str(path), "--name", "feature/x", "-r", "feature/x", cwd=repo)
    dirty = repo.parent / "dirty"
    run("jj", "workspace", "add", str(dirty), "--name", "dirty", "-r", "main", cwd=repo)
    (dirty / "untracked").write_text("keep")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("W")
        await wait_until(pilot, lambda: isinstance(app.screen, CleanupScreen))
        assert isinstance(app.screen, CleanupScreen)
        assert [(ws.name, ws.removable) for ws in app.screen.workspaces] == [
            ("feature/x", True),
            ("dirty", False),
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
        await pilot.press("enter")  # asks first
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await wait_until(pilot, lambda: not path.exists())
        assert dirty.exists()
        assert app.is_running

        run("jj", "workspace", "forget", "dirty", cwd=repo)
        await pilot.press("W")  # nothing left: just a message
        await wait_until(pilot, lambda: isinstance(app.screen, MessageScreen))
        assert isinstance(app.screen, MessageScreen)
        assert "No workspaces found" in app.screen.body
        await pilot.press("q")
        assert app.is_running

    run_app(repo, fake_cfg(), scenario)
    assert "feature/x" in run("jj", "bookmark", "list", cwd=repo)


def test_octopus_merge_background(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    from project_sandbox_tui.history import PromptHistory
    from tests.conftest import run

    add_bookmark(repo, "fix/y", "y.txt", "y\n")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        order = [r.branch.name for r in app.visible_rows() if not r.branch.working_copy]
        await pilot.press("v", "G", "r")  # select all three bookmarks, unsupervised
        form = app.screen
        assert isinstance(form, LaunchScreen) and form.octopus is not None
        assert form.octopus.inputs == order
        result = form.query_one("#result", Input)
        assert result.value.startswith("merge/")
        prompt = form.query_one("#prompt", TextArea)
        assert prompt.text.startswith("Integrate the bookmarks")
        form.query_one("#agent", CycleField).value = "bash"
        await pilot.pause()
        assert prompt.text.startswith("jj status")
        form.query_one("#operation", CycleField).value = "rebase"
        await pilot.pause()
        assert "jj diff --stat --from '\"main\"' --to @-" in prompt.text
        form.query_one("#operation", CycleField).value = "merge"
        form.query_one("#agent", CycleField).value = "claude"
        await pilot.pause()
        assert prompt.text.startswith("Integrate the bookmarks")
        # switching to rebase updates the untouched name and prompt
        form.query_one("#operation").focus()
        await pilot.press("l")
        await pilot.pause()
        assert result.value.startswith("rebase/") and "linear history" in prompt.text
        await pilot.press("h")
        await pilot.pause()
        assert result.value.startswith("merge/")
        # an existing bookmark name or empty result is refused; the form stays open
        result.value = "main"
        await pilot.press("ctrl+enter")
        assert app.screen is form
        result.value = "  "
        await pilot.press("ctrl+enter")
        assert app.screen is form
        assert any("bookmark name is empty" in str(n.message) for n in app._notifications)
        result.value = "merge/all"
        prompt.text = prompt.text + "\nsleep:0.3"
        await pilot.press("ctrl+enter")
        await wait_until(pilot, lambda: "merge/all" in app.jobs.jobs, timeout=15)
        job = app.jobs.jobs["merge/all"]
        assert job.spec.operation == "merge" and job.spec.inputs == order
        assert "## Current state" in job.spec.prompt_text
        assert "--branch=merge/all" in job.argv
        assert app.selection() == []  # cleared after launching
        await wait_until(pilot, lambda: not app.jobs.jobs["merge/all"].running, timeout=15)
        # The history graph joins the inputs into the result, above all of them.
        rows = app.visible_rows()
        assert app.graph.state.bookmark_parents([r.branch.name for r in rows])["merge/all"] == order
        assert rows[1].branch.name == "merge/all"
        assert row_texts(app)[1][0][1:] == "─┬─╮"  # the node shows the run status
        assert [n.kind for n in app.graph.state.history("merge/all")] == ["run", "merge"]

    run_app(repo, fake_cfg(), scenario)
    parents = run("jj", "--ignore-working-copy", "log", "--no-graph", "-r", '"merge/all"', "-T",
                  'parents.map(|p| p.bookmarks()).join(",")', cwd=repo)  # fmt: skip
    assert sorted(parents.split(",")) == ["feature/x", "fix/y", "main"]  # merged on the host
    # history keeps what was typed, not the generated state report
    newest = PromptHistory.for_repo(repo).newest_first()[0]
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
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib

    add_bookmark(repo, "fix/y", "y.txt", "y\n")
    cfg = fake_cfg()
    cfg.sandbox.command = "true"  # the "agent" exits at once

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        monkeypatch.setattr(app, "suspend", contextlib.nullcontext)
        await pilot.press("j", "v", "j", "i")  # main and the bookmark below it
        form = app.screen
        assert isinstance(form, LaunchScreen) and form.octopus is not None
        assert "interactive session · merge 2 bookmarks" in str(form.query_one("#title").render())
        await pilot.press("enter")  # launch from the operation field
        await wait_until(
            pilot, lambda: bool([j for j in app.jobs.jobs.values() if j.spec.operation])
        )
        job = next(j for j in app.jobs.jobs.values() if j.spec.operation)
        assert job.mode == "interactive"
        await wait_until(pilot, lambda: not job.running)
        assert app.screen is app.main

    run_app(repo, cfg, scenario)


def test_cleanup_force_delete(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    from project_sandbox_tui.screens.dialogs import CleanupScreen, ConfirmScreen
    from tests.conftest import run

    dirty = repo.parent / "dirty"
    run("jj", "workspace", "add", str(dirty), "--name", "dirty", "-r", "main", cwd=repo)
    (dirty / "untracked").write_text("lost")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("W")
        await wait_until(pilot, lambda: isinstance(app.screen, CleanupScreen))
        await pilot.press("D")
        await wait_until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        assert isinstance(app.screen, ConfirmScreen)
        assert "uncommitted changes" in app.screen.body
        assert f"Delete directory and all contents: {dirty}" in app.screen.body
        assert "  untracked" in app.screen.body
        assert "  a.txt" in app.screen.body
        assert "Unregister workspace: dirty" in app.screen.body
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

    run_app(repo, fake_cfg(), scenario)
    assert "dirty" not in run("jj", "workspace", "list", cwd=repo)


def test_diff_selection(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    shown: list[tuple[str, str]] = []

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        monkeypatch.setattr(app, "_show_diff", lambda base, target: shown.append((base, target)))
        await pilot.press("j", "equals_sign")
        assert shown[-1] == ("main", "feature/x")
        app.marked = ["feature/x"]
        await pilot.press("equals_sign")
        assert shown[-1] == ("main", "feature/x")
        app.marked = ["feature/x", "main"]
        await pilot.press("equals_sign")
        assert shown[-1] == ("feature/x", "main")
        app.cfg.project.main_branch = "feature/x"
        app.marked = ["main"]
        await pilot.press("equals_sign")
        assert shown[-1] == ("feature/x", "main")
        assert len(shown) == 4

    run_app(repo, fake_cfg(), scenario)


@pytest.mark.parametrize("failure", [FileNotFoundError("jj missing"), 1, KeyboardInterrupt()])
def test_diff_viewer_failure_recovers(
    repo: Path,
    fake_cfg: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException | int,
) -> None:
    import contextlib

    def viewer(argv: list[str], *, cwd: Path) -> int:
        assert argv == ["jj", "diff", "--from", '"main"', "--to", '"feature/x"']
        assert cwd == repo
        if isinstance(failure, BaseException):
            raise failure
        return failure

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        monkeypatch.setattr(app, "_suspended", contextlib.nullcontext)
        monkeypatch.setattr("subprocess.call", viewer)
        monkeypatch.setattr("builtins.input", lambda _: "")
        await pilot.press("j", "equals_sign")
        if not isinstance(failure, KeyboardInterrupt):
            assert any("jj diff" in str(n.message) for n in app._notifications)
        await pilot.press("?")
        assert isinstance(app.screen, HelpScreen)

    run_app(repo, fake_cfg(), scenario)


def test_notifications_are_plain_text(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app.notify("bookmark [bold]x[/] failed: a[b")
        app.main.notify("from a screen: [/oops]")  # Widget.notify passes markup=True
        await pilot.pause()
        assert [n.markup for n in app._notifications] == [False, False]

    run_app(repo, fake_cfg(), scenario)


def test_launch_rechecks_bookmark_deleted_while_form_is_open(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    from tests.conftest import jj

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j", "r")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        form.query_one("#prompt", TextArea).text = "do not launch"
        jj(repo, "bookmark", "delete", "feature/x")
        await pilot.press("ctrl+s")
        assert app.screen is app.main
        assert not app.jobs.jobs and app.prompt_history.entries == []
        assert LastUsed.load(repo).agent == ""
        assert any("feature/x no longer exists" in str(n.message) for n in app._notifications)

    run_app(repo, fake_cfg(), scenario)


def test_octopus_changes_preserve_user_edited_prompt_and_result(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j", "v", "j", "r")
        form = app.screen
        assert isinstance(form, LaunchScreen) and form.octopus is not None
        form.query_one("#result", Input).value = "my-result"
        prompt = form.query_one("#prompt", TextArea)
        prompt.text = "My instructions, including {base} literally"
        await pilot.pause()
        form.query_one("#operation", CycleField).value = "rebase"
        form.query_one("#agent", CycleField).value = "bash"
        await pilot.pause()
        assert form.current_spec().branch == "my-result"
        assert prompt.text == "My instructions, including {base} literally"
        assert form.current_spec().operation == "rebase"
        await pilot.press("escape")
        assert not app.jobs.jobs and app.prompt_history.entries == []

    run_app(repo, fake_cfg(), scenario)


def test_overlapping_refreshes_wait_for_coalesced_update(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    app = SandboxTUI(repo, detect(repo), fake_cfg())

    async def scenario() -> None:
        started = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        calls = 0

        async def refresh() -> None:
            nonlocal calls
            index = calls
            calls += 1
            started[index].set()
            await release[index].wait()

        monkeypatch.setattr(app, "_refresh_once", refresh)
        first = asyncio.create_task(app.refresh_data())
        await started[0].wait()
        followers = [asyncio.create_task(app.refresh_data()) for _ in range(2)]
        await asyncio.sleep(0)  # let both callers request a fresh pass
        release[0].set()
        await started[1].wait()
        assert all(not task.done() for task in followers)
        release[1].set()
        await asyncio.gather(first, *followers)
        assert calls == 2 and not app._refreshing

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_marked_rows_stay_visible_under_a_filter(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    """Regression: dd with a mark hidden by the filter deleted a row the user could not see."""
    add_bookmark(repo, "fix/y", "y.txt", "y\n")

    def names(app: SandboxTUI) -> list[str]:
        return [r.branch.name for r in app.visible_rows()]

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app.marked = ["fix/y"]
        await pilot.press("slash", *"feature", "enter")
        assert names(app) == ["fix/y", "feature/x"]  # the mark stays on screen
        header = str(app.main.query_one("#header").render())
        assert "1 selected (1 outside the filter)" in header
        await pilot.press("escape")  # clears the filter, keeps the mark
        assert app.marked == ["fix/y"]
        assert "fix/y" in names(app)

    run_app(repo, fake_cfg(), scenario)


def test_actions_end_visual_mode_keeping_the_range(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j", "v", "j", "P")
        assert isinstance(app.screen, MessageScreen)
        await pilot.press("q")
        assert app._visual_anchor is None
        assert app.selection() == ["feature/x", "main"]
        await pilot.press("k", "k")  # no longer follows the cursor
        assert app.selection() == ["feature/x", "main"]

    run_app(repo, fake_cfg(), scenario)


def test_pending_prefix_is_shown_and_cancelled(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    def header(app: SandboxTUI) -> str:
        return str(app.main.query_one("#header").render())

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("G", "d")
        assert "d…" in header(app)
        await pilot.press("k")  # another key cancels dd
        assert "d…" not in header(app)
        await pilot.press("d")
        await pilot.pause(1.0)  # and so does waiting
        assert "d…" not in header(app)
        await pilot.press("d")
        await app.workers.wait_for_complete()
        assert "feature/x" in {b.name for b in app.backend.list_branches()}
        await pilot.press("g", "j", "g")  # gg with a key in between does not jump
        assert app.selected_branch_name() == "main"
        await pilot.press("g", "g")
        assert app.selected_branch_name() == "@"

    run_app(repo, fake_cfg(), scenario)


def test_fast_forward_main_undo_and_redo(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    from tests.conftest import jj

    async def vim(pilot: Any, app: SandboxTUI, *keys: str) -> None:
        await pilot.press(*keys)
        await app.workers.wait_for_complete()
        await pilot.pause()

    def messages(app: SandboxTUI) -> list[str]:
        return [str(n.message) for n in app._notifications]

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        old = app.backend._bookmark_commit("main")
        new = app.backend._bookmark_commit("feature/x")
        await vim(pilot, app, "j", "f")
        assert app.backend._bookmark_commit("main") == new
        assert "fast-forwarded main to feature/x (u undoes)" in messages(app)
        await vim(pilot, app, "u")
        assert app.backend._bookmark_commit("main") == old
        assert "undo fast-forward: moved main back" in messages(app)
        assert app.selected_branch_name() == "main"
        await vim(pilot, app, "ctrl+r")
        assert app.backend._bookmark_commit("main") == new
        # Moved again elsewhere: undo refuses instead of losing that move.
        add_bookmark(repo, "next", "next.txt", "next\n", start="feature/x")
        jj(repo, "bookmark", "move", "main", "--to", "next")
        await vim(pilot, app, "u")
        assert messages(app)[-1] == "undo failed: main has moved since"
        assert app.backend._bookmark_commit("main") == app.backend._bookmark_commit("next")

    run_app(repo, fake_cfg(), scenario)


def test_cleanup_bookmark_deletion_can_be_undone(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    from project_sandbox_tui.screens.dialogs import CleanupScreen
    from tests.conftest import jj

    jj(repo, "bookmark", "create", "old", "-r", "main")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        commit = app.backend._bookmark_commit("old")
        await pilot.press("W")
        await wait_until(pilot, lambda: isinstance(app.screen, CleanupScreen))
        await pilot.press("enter")
        assert isinstance(app.screen, ConfirmScreen)
        assert "delete bookmark old" in app.screen.body
        await pilot.press("y")
        await app.workers.wait_for_complete()
        assert "old" not in {b.name for b in app.backend.list_branches()}
        assert any(str(n.message) == "deleted old (u restores)" for n in app._notifications)
        assert app.screen is app.main  # nothing was skipped: no report
        await pilot.press("u")
        await app.workers.wait_for_complete()
        assert app.backend._bookmark_commit("old") == commit

    run_app(repo, fake_cfg(), scenario)


def test_unsent_prompt_is_kept_as_a_draft(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j", "r")
        assert isinstance(app.screen, LaunchScreen)
        await pilot.press(*"fix it", "escape")
        assert app.screen is app.main
        assert any("kept as a draft" in str(n.message) for n in app._notifications)
        await pilot.press("i", "escape")  # an interactive form leaves the draft alone
        await pilot.press("r")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        assert form.query_one("#prompt", TextArea).text == "fix it"
        await pilot.press("escape", "k", "r")  # drafts belong to their bookmark
        assert isinstance(app.screen, LaunchScreen)
        assert app.screen.query_one("#prompt", TextArea).text == ""
        await pilot.press("escape", "j", "r")
        area = app.screen.query_one("#prompt", TextArea)
        area.text = "exit:0"
        await pilot.press("ctrl+s")
        await wait_until(pilot, lambda: "feature/x" in app.jobs.jobs)
        assert app._drafts == {}  # launched: nothing left to keep

    run_app(repo, fake_cfg(), scenario)


def test_open_many_workspaces_asks_first(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    add_bookmark(repo, "a", "a2.txt", "a\n")
    add_bookmark(repo, "b", "b2.txt", "b\n")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        monkeypatch.setattr(app, "_open_editor", opened.append)
        app.marked = ["a", "b", "feature/x", "main"]
        await pilot.press("o")
        assert isinstance(app.screen, ConfirmScreen) and opened == []
        await pilot.press("escape")
        assert opened == []
        await pilot.press("o", "y")
        assert opened == ["a", "b", "feature/x", "main"]

    run_app(repo, fake_cfg(), scenario)


def test_log_key_without_runs_shows_details(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j", "l")
        assert isinstance(app.screen, MessageScreen)
        assert "interactive command" in app.screen.body

    run_app(repo, fake_cfg(), scenario)


def test_launch_form_flags_a_new_bookmark_name(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j", "i")
        form = app.screen
        assert isinstance(form, LaunchScreen)

        def hint() -> str:
            return str(form.query_one("#branch-hint", Static).content)

        assert "existing name selects it" in hint()
        form.query_one("#branch", Input).value = "feature/typo"
        await pilot.pause()
        assert hint() == "new bookmark feature/typo, created from feature/x"
        form.query_one("#branch", Input).value = "main"
        await pilot.pause()
        assert "existing name selects it" in hint()

    run_app(repo, fake_cfg(), scenario)
