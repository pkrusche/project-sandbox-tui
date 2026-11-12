"""End-to-end UI tests with Textual's Pilot (no pytest-asyncio needed)."""

from __future__ import annotations

import asyncio
import shutil
import signal
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, cast

import pytest
from rich.cells import cell_len
from textual.screen import Screen
from textual.widgets import DataTable, Input, Static, TextArea
from textual.widgets.data_table import ColumnKey

from project_sandbox_tui.app import SandboxTUI
from project_sandbox_tui.config import Config, LastUsed
from project_sandbox_tui.jobs import JobManager
from project_sandbox_tui.screens.dialogs import (
    ConfirmScreen,
    DescriptionScreen,
    HelpScreen,
    MessageScreen,
    NewBranchScreen,
)
from project_sandbox_tui.screens.launch import CUSTOM_MODEL, LaunchScreen
from project_sandbox_tui.screens.log import LogScreen
from project_sandbox_tui.screens.review import CommitReviewScreen, DiffReviewScreen
from project_sandbox_tui.vcs import detect
from project_sandbox_tui.widgets import CycleField
from tests.conftest import add_bookmark, jj


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


async def wait_for_screen[T: Screen[Any]](app: SandboxTUI, pilot: Any, screen_type: type[T]) -> T:
    """Wait for on_mount, even when auto-focus or an empty table looks ready."""
    await wait_until(pilot, lambda: isinstance(app.screen, screen_type) and app.screen.is_mounted)
    return cast(T, app.screen)


def row_texts(app: SandboxTUI) -> list[list[str]]:
    table = app.table
    return [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]


@pytest.mark.parametrize("name", ["long/" + "bookmark" * 15, "分支/" + "界" * 45])
def test_refresh_preserves_horizontal_scroll(
    repo: Path, fake_cfg: Callable[..., Config], name: str
) -> None:
    detect(repo).create_branch(name, "main")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app._refresh_timer.pause()
        await pilot.press("G")
        selected = app.selected_branch_name()
        table = app.table
        assert table.columns[ColumnKey("bookmark")].width >= cell_len(name) + 2
        assert table.max_scroll_x > 40
        for offset in (table.max_scroll_x, 20):
            table.scroll_to(x=offset, animate=False)
            await pilot.pause()
            await app.refresh_data()
            await pilot.pause()
            assert table.scroll_x == table.scroll_target_x == offset
            assert app.selected_branch_name() == selected

        before = app._refreshed
        app._refresh_timer.resume()  # periodic refresh uses the same restoration
        await wait_until(pilot, lambda: app._refreshed is not before and app._refreshed.is_set())
        await pilot.pause()
        assert table.scroll_x == table.scroll_target_x == 20
        app._refresh_timer.pause()

        offset = table.max_scroll_x
        table.scroll_to(x=offset, animate=False)
        app.filter_text = "main"
        app._render_table()
        # The table recomputes its width on idle; the clamp must follow it.
        await wait_until(pilot, lambda: table.max_scroll_x < offset)
        await pilot.pause()
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


def test_op_history_view(repo: Path, tmp_path: Path, fake_cfg: Callable[..., Config]) -> None:
    from textual.containers import VerticalScroll
    from textual.widgets import Log

    from project_sandbox_tui.jobs import JobRecord
    from project_sandbox_tui.sandbox import LaunchSpec
    from project_sandbox_tui.screens.oplog import OpLogScreen

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        long_prompt = "second try\n" + "Keep [brackets] literal and show all prompt details. " * 40
        with app.graph.transaction() as state:
            for i, prompt in enumerate(["first try", "second try"]):
                log = tmp_path / f"run{i}.log"
                log.write_text(f"output of {prompt}\n")
                spec = LaunchSpec(
                    "feature/x",
                    "codex",
                    mode="background",
                    prompt_text=long_prompt if i else prompt,
                )
                job = JobRecord("feature/x", spec, 1, f"2026-01-0{i + 1}T00:00:00Z")
                job.argv = ["project-sandbox", "--prompt", prompt]
                job.log_path, job.stdout_path = str(log), str(log.with_suffix(".out"))
                state.add_run(job).status = "completed" if i else "failed"
        await pilot.press("j", "L")  # starts on feature/x's newest run
        screen = app.screen
        assert isinstance(screen, OpLogScreen)
        rows = [[str(c) for c in screen.table.get_row_at(i)] for i in range(3)]
        assert [(r[0], r[2], r[4]) for r in rows] == [
            ("✓", "feature/x", "run · completed"),
            ("✗", "feature/x", "run · failed"),
            ("○", "feature/x", "existing"),
        ]
        head = app.graph.state.head("feature/x")
        assert head is not None and screen.selected_id() == head.id
        details = screen.query_one("#oplog-details", Static)
        pane = screen.query_one("#oplog-details-pane", VerticalScroll)
        assert long_prompt.strip() in str(details.content)
        assert "agent       codex · background" in str(details.content)
        assert "command     project-sandbox --prompt 'second try'" in str(details.content)
        assert pane.region.y >= screen.table.region.bottom
        assert pane.max_scroll_y > 0 and pane.max_scroll_x == 0
        assert screen.table.max_scroll_x == 0
        await pilot.press("tab", "end")
        assert app.focused is pane
        await wait_until(pilot, lambda: pane.scroll_y > 0)  # scroll_end waits for a refresh
        screen._render_rows()
        await pilot.pause()
        assert pane.scroll_y > 0  # refresh does not reset reading position
        await pilot.press("shift+tab", "enter")
        assert isinstance(app.screen, MessageScreen)
        assert "after       feature/x: background codex → failed: first try" in app.screen.body
        await pilot.press("q", "j")
        assert "prompt      first try" in str(details.content)
        assert "second try" not in str(details.content)
        assert pane.scroll_y == 0
        await pilot.press("j")
        assert "existing bookmark from main" in str(details.content)
        assert "agent" not in str(details.content)
        await pilot.press("k", "l")  # an earlier run's log
        screen = await wait_for_screen(app, pilot, LogScreen)
        assert "output of first try" in "\n".join(screen.query_one(Log).lines)
        await pilot.press("q", "q")
        assert app.screen is app.main

    run_app(repo, fake_cfg(), scenario, size=(80, 24))


def test_list_navigation_and_filter(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        rows = row_texts(app)
        assert [r[1] for r in rows] == ["@ working copy ●", "feature/x", "main ●"]
        assert [r[0] for r in rows] == [" ", "○", "○"]  # history graph: feature/x on main
        assert "2 bookmarks ·" in str(app.main.query_one("#header", Static).content)
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
            "j",
            "k",
            "G",
            "g",
            "g",
            "space",
            "v",
            "r",
            "i",
            "l",
            "e",
            "f",
            "@",
            "d",
            "d",
            "equals_sign",
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


@pytest.mark.parametrize("name", ["main", "feature/x", "@"])
def test_edit_description(repo: Path, fake_cfg: Callable[..., Config], name: str) -> None:
    from tests.conftest import jj

    jj(repo, "describe", "-r", name, "-m", "old subject\n\nexisting body")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        if name == "main":
            await pilot.press("G")
        elif name != "@":
            await pilot.press("j")
        await pilot.press("e")
        await wait_for_screen(app, pilot, DescriptionScreen)
        editor = app.screen.query_one(TextArea)
        assert app.focused is editor
        assert editor.text == "old subject\n\nexisting body\n"
        editor.load_text("cancelled")
        await pilot.press("escape")
        assert app.screen is app.main
        assert app.backend.get_description(name)[1] == "old subject\n\nexisting body\n"

        await pilot.press("e")
        await wait_for_screen(app, pilot, DescriptionScreen)
        app.screen.query_one(TextArea).load_text("new subject")
        await pilot.press("end", "enter", "enter", *"body jkrq")
        assert isinstance(app.screen, DescriptionScreen)  # typing doesn't run list actions
        assert app.screen.query_one(TextArea).text == "new subject\n\nbody jkrq"
        await pilot.press("ctrl+s")
        await wait_until(
            pilot, lambda: app.screen is app.main and app.branch(name).subject == "new subject"
        )
        assert app.selected_branch_name() == name
        assert app.backend.get_description(name)[1] == "new subject\n\nbody jkrq\n"

    run_app(repo, fake_cfg(), scenario)


def test_description_editor_keeps_draft_on_error(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    from tests.conftest import jj

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j", "e")
        await wait_for_screen(app, pilot, DescriptionScreen)
        app.screen.query_one(TextArea).load_text("my draft")
        jj(repo, "describe", "feature/x", "-m", "external edit")
        await pilot.press("ctrl+s")
        assert isinstance(app.screen, DescriptionScreen)
        assert "changed since" in str(app.screen.query_one("#error", Static).render())
        assert app.screen.query_one(TextArea).text == "my draft"
        assert app.backend.get_description("feature/x")[1] == "external edit\n"
        await pilot.press("escape")
        await pilot.press("g", "g", "j", "v", "j", "e")
        assert app.screen is app.main
        assert any("select one bookmark" in str(n.message) for n in app._notifications)

    run_app(repo, fake_cfg(), scenario)


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


def test_partial_redo_preserves_commits_for_a_retry(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    from project_sandbox_tui.vcs import VcsError

    backend = detect(repo)
    backend.create_branch("second", "main")
    app = SandboxTUI(repo, backend, fake_cfg())
    app._delete_idle(["feature/x", "second"])
    app._undo_step(redo=False)
    # New work on the first bookmark must survive a failure deleting the second.
    jj(repo, "new", "feature/x")
    (repo / "new-work.txt").write_text("keep this\n")
    jj(repo, "bookmark", "set", "feature/x", "-r", "@")
    updated = backend._bookmark_commit("feature/x")
    delete = backend.delete_bookmark

    def fail_second(name: str) -> str:
        if name == "second":
            raise VcsError("injected failure")
        return delete(name)

    with monkeypatch.context() as patch:
        patch.setattr(backend, "delete_bookmark", fail_second)
        with pytest.raises(VcsError, match="injected failure"):
            app._undo_step(redo=True)
    app._undo_step(redo=True)
    app._undo_step(redo=False)
    assert backend._bookmark_commit("feature/x") == updated


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


@pytest.mark.parametrize("configured", ["", "release"])
def test_move_working_copy_skips_saving_ancestor_of_main(
    repo: Path, fake_cfg: Callable[..., Config], configured: str
) -> None:
    from tests.conftest import jj

    jj(repo, "describe", "-m", "previous work")
    jj(repo, "new")
    jj(repo, "bookmark", "create", "release", "-r", "@")
    if not configured:
        jj(repo, "bookmark", "move", "main", "--to", "release")
    jj(repo, "edit", "@-")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app.marked = ["feature/x"]
        await pilot.press("@")
        await app.workers.wait_for_complete()
        assert app.backend._jj("log", "--no-graph", "-r", "@-", "-T", "commit_id").strip() == (
            app.backend._bookmark_commit("feature/x")
        )
        assert not any(b.name.startswith("saved/") for b in app.branches)
        notifications = "\n".join(str(n.message) for n in app._notifications)
        assert "working copy moved onto feature/x" in notifications
        assert "previous change saved" not in notifications

    run_app(repo, fake_cfg(main_branch=configured), scenario)


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


@pytest.mark.parametrize("answer", ["y", "n"])
def test_move_working_copy_asks_about_held_workspace(
    repo: Path, fake_cfg: Callable[..., Config], answer: str
) -> None:
    from project_sandbox_tui.vcs import workspace_path
    from tests.conftest import jj

    path = workspace_path(repo, "feature/x", None)

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app.backend.add_workspace("feature/x", path)
        (path / "b.txt").write_text("finished\n")
        jj(path, "bookmark", "set", "--allow-backwards", "feature/x", "-r", "@")
        old = jj(repo, "log", "--no-graph", "-r", "@", "-T", "commit_id")
        await app.refresh_data()
        app.marked = ["feature/x"]
        await pilot.press("@")
        await wait_for_screen(app, pilot, ConfirmScreen)
        assert isinstance(app.screen, ConfirmScreen)
        assert "working copy of workspace feature/x" in app.screen.body
        await pilot.press(answer)
        await app.workers.wait_for_complete()
        target = app.backend._bookmark_commit("feature/x")
        if answer == "y":
            assert jj(repo, "log", "--no-graph", "-r", "@-", "-T", "commit_id") == target
            assert jj(path, "log", "--no-graph", "-r", "@-", "-T", "commit_id") == target
        else:
            assert jj(repo, "log", "--no-graph", "-r", "@", "-T", "commit_id") == old
            assert any("W removes workspace" in str(n.message) for n in app._notifications)

    run_app(repo, fake_cfg(), scenario)


def test_move_working_copy_keeps_held_workspace_with_session(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    from project_sandbox_tui.jobs import JobManager
    from project_sandbox_tui.sandbox import LaunchSpec
    from project_sandbox_tui.vcs import workspace_path
    from tests.conftest import jj

    cfg = fake_cfg()
    path = workspace_path(repo, "feature/x", None)

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app.backend.add_workspace("feature/x", path)
        jj(path, "bookmark", "set", "--allow-backwards", "feature/x", "-r", "@")
        held = jj(path, "log", "--no-graph", "-r", "@", "-T", "commit_id")
        JobManager(repo, cfg).record_external(LaunchSpec(branch="feature/x", agent="codex"), [])
        await app._move_working_copy("feature/x", new_in_holders=True).wait()
        assert jj(path, "log", "--no-graph", "-r", "@", "-T", "commit_id") == held
        assert any("already has" in str(n.message) for n in app._notifications)

    run_app(repo, cfg, scenario)


@pytest.mark.parametrize("mode", ["r", "i"])
@pytest.mark.parametrize("multiple", [False, True])
def test_launch_inputs_remain_visible_when_focused(
    repo: Path, fake_cfg: Callable[..., Config], mode: str, multiple: bool
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j")  # select feature/x for sessions
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


@pytest.mark.parametrize("mode", ["r", "i"])
@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_launch_form_fast_mode(
    repo: Path, fake_cfg: Callable[..., Config], mode: str, agent: str
) -> None:
    cfg = fake_cfg()
    cfg.defaults.agent = agent

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j", mode)
        form = app.screen
        assert isinstance(form, LaunchScreen)
        fast = form.query_one("#fast", CycleField)
        assert fast.display and fast.value == "off"
        fast.focus()
        await pilot.press("l")
        assert form.current_spec().fast
        assert "--fast" in str(form.query_one("#preview", Static).render())
        agent_field = form.query_one("#agent", CycleField)
        for other in ("pi", "opencode", "bash"):
            agent_field.value = other
            await pilot.pause()
            assert not fast.display and not form.current_spec().fast
            assert "--fast" not in str(form.query_one("#preview", Static).render())
        agent_field.value = agent
        await pilot.pause()
        assert fast.display and fast.value == "on"
        fast.focus()
        await pilot.press("h")
        assert not form.current_spec().fast
        assert "--fast" not in str(form.query_one("#preview", Static).render())
        await pilot.press("l")
        if mode == "r":
            form.query_one("#prompt", TextArea).text = "exit:0"
            await pilot.press("ctrl+s")
            await wait_until(pilot, lambda: "feature/x" in app.jobs.jobs)
            await wait_until(pilot, lambda: not app.jobs.jobs["feature/x"].running)
            assert LastUsed.load(repo).fast_by_agent == {agent: True}
            restored = JobManager(repo, cfg).jobs["feature/x"]
            assert restored.spec.fast and "--fast" in restored.argv
            await pilot.press("r")
            assert isinstance(app.screen, LaunchScreen)
            assert app.screen.current_spec().fast
        await pilot.press("escape")

    run_app(repo, cfg, scenario)


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
        form.query_one("#agent", CycleField).focus()
        await pilot.press("l")  # agent → codex
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
        # Records are refreshed in a thread; the table is redrawn when the pass ends.
        await wait_until(
            pilot, lambda: any(r[1] == "feature/x" and r[0] == "✓" for r in row_texts(app))
        )
        row = next(r for r in row_texts(app) if r[1] == "feature/x")
        assert row[0] == "✓"
        assert row[6].startswith("completed · ") and row[6].endswith(" ago")
        # The history records the outcome once a listing after the run's end is seen.
        await wait_until(pilot, lambda: app.graph.state.history("feature/x")[0].status != "")
        await pilot.press("P")  # the agent moved from the list to the details
        assert isinstance(app.screen, MessageScreen)
        assert "last run    completed (exit 0)" in app.screen.body
        assert "codex/gpt-x · background" in app.screen.body
        assert "background codex/gpt-x → completed: sleep:0.6 exit:0" in app.screen.body
        await pilot.press("q")

        await pilot.press("l")
        screen = await wait_for_screen(app, pilot, LogScreen)
        assert screen.current is not None and screen.current.suffix == ".md"
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
        await pilot.press("j")  # select feature/x for sessions
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
        await pilot.press("j")  # select feature/x for sessions
        await pilot.press("i")
        assert isinstance(app.screen, LaunchScreen)
        await pilot.press("q", "r", "n")  # on the agent CycleField: no quit / no new form
        assert isinstance(app.screen, LaunchScreen) and app.is_running
        await pilot.press("escape")
        assert not isinstance(app.screen, LaunchScreen)

    run_app(repo, fake_cfg(), scenario)


def test_missing_image_config_is_reported(repo: Path) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j")  # select feature/x for sessions
        await pilot.press("r")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        assert "no image configured" in str(form.query_one("#preview").render())
        form.query_one("#prompt", TextArea).text = "go"
        await pilot.press("ctrl+enter")
        await wait_until(
            pilot, lambda: any("no image configured" in str(n.message) for n in app._notifications)
        )
        assert "feature/x" not in app.jobs.jobs

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
        await pilot.press("j")  # select feature/x for sessions
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


@pytest.mark.parametrize("key", ["r", "i"])
@pytest.mark.parametrize("target", ["main", "master", "release"])
def test_protected_revisions_reject_sessions(
    repo: Path, fake_cfg: Callable[..., Config], key: str, target: str
) -> None:
    if target in ("master", "release"):
        jj(repo, "bookmark", "rename", "main", target)
    cfg = fake_cfg(main_branch="release" if target == "release" else "")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        rows = {r.branch.name: r for r in app.visible_rows()}
        assert rows[target].name_cell().style == "dim"
        assert rows[target].name_cell(selected=True).style == "dim"
        assert rows["feature/x"].name_cell().style != "dim"
        # A mark takes precedence over the cursor on an ordinary bookmark.
        app.table.move_cursor(row=list(rows).index("feature/x"))
        app.marked = [target]
        await pilot.press(key)
        assert app.screen is app.main and not app.jobs.jobs
        assert any(
            f"{target} is protected; create a bookmark" in str(n.message)
            for n in app._notifications
        )
        app.clear_branch_selection()
        app.table.move_cursor(row=list(rows).index(target))
        await pilot.press(key)
        assert app.screen is app.main and not app.jobs.jobs

    run_app(repo, cfg, scenario)


@pytest.mark.parametrize("target", ["main", "feature/x"])
@pytest.mark.parametrize("mode", ["background", "interactive"])
def test_launch_rechecks_protected_target(
    repo: Path, fake_cfg: Callable[..., Config], target: str, mode: str
) -> None:
    from project_sandbox_tui.sandbox import LaunchSpec, Mode

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j", "r")
        assert isinstance(app.screen, LaunchScreen)
        await pilot.press("escape")
        if target == "feature/x":
            app.cfg.project.main_branch = target  # config changed after opening the form
        app._start_launch(LaunchSpec(target, "bash", mode=cast(Mode, mode), prompt_text="exit:0"))
        await pilot.pause()
        assert not app.jobs.jobs
        assert any(f"{target} is protected" in str(n.message) for n in app._notifications)
        assert LastUsed.load(repo).agent == ""

    run_app(repo, fake_cfg(), scenario)


def test_unsupervised_run_launches_with_ctrl_s_or_button(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j")  # select feature/x for sessions
        await pilot.press("r")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        form.query_one("#prompt", TextArea).text = "sleep:0.1"
        await pilot.click("#launch")
        assert not isinstance(app.screen, LaunchScreen)
        await wait_until(
            pilot, lambda: "feature/x" in app.jobs.jobs and not app.jobs.jobs["feature/x"].running
        )

        await pilot.press("r")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        form.query_one("#prompt", TextArea).text = "sleep:0.1"
        await pilot.press("ctrl+s")
        assert not isinstance(app.screen, LaunchScreen)

    run_app(repo, fake_cfg(), scenario)


@pytest.mark.parametrize("target", ["@", "feature/x"])
def test_external_launch_rechecks_busy_state(
    repo: Path, fake_cfg: Callable[..., Config], target: str
) -> None:
    from project_sandbox_tui.jobs import JobManager
    from project_sandbox_tui.sandbox import LaunchSpec

    cfg = fake_cfg()
    cfg.ui.terminal_command = "true {cmd}"

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        if target != "@":
            await pilot.press("j")
        await pilot.press("i")
        assert isinstance(app.screen, LaunchScreen)
        other = JobManager(repo, cfg)
        reserved = other.record_external(LaunchSpec(branch=target, agent="codex"), ["other"])
        await pilot.press("enter")
        assert app.jobs.jobs[target].started_at == reserved.started_at
        assert app.jobs.jobs[target].spec.agent == "codex"
        assert LastUsed.load(repo).agent == ""

    run_app(repo, cfg, scenario)


def test_working_copy_background_session(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        row = app.visible_rows()[0]
        assert row.branch.working_copy and row.name_cell().style != "dim"
        await pilot.press("r")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        assert form.current_spec().main_checkout
        form.query_one("#prompt", TextArea).text = "sleep:3 exit:0"
        await pilot.press("ctrl+s")
        await wait_until(pilot, lambda: "@" in app.sessions)
        job = app.jobs.jobs["@"]
        assert job.running and job.spec.main_checkout
        assert Path(app.sessions["@"].workspace_path) == repo
        assert not any(
            arg.startswith(("--branch", "--worktree-dir", "--keep-workspace")) for arg in job.argv
        )
        for key in ("i", "r"):
            await pilot.press(key)
            assert app.screen is app.main
        assert any("@ already has" in str(n.message) for n in app._notifications)
        await wait_until(pilot, lambda: not app.jobs.jobs["@"].running)
        assert (app.jobs.jobs["@"].status, app.jobs.jobs["@"].exit_code) == ("completed", 0)
        assert not (repo.parent / f"{repo.name}-workspaces").exists()

    run_app(repo, fake_cfg(), scenario)


def test_failed_external_launch_releases_reservation(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    cfg = fake_cfg()
    cfg.ui.terminal_command = "definitely-not-an-installed-terminal {cmd}"

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j")  # select feature/x for sessions
        await pilot.press("i", "enter")
        assert "feature/x" not in app.jobs.jobs
        assert LastUsed.load(repo).agent == ""

    run_app(repo, cfg, scenario)


def test_jj_repo_without_bookmarks_allows_working_copy_and_new_bookmark(
    jj_repo_no_bookmarks: Path, fake_cfg: Callable[..., Config]
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        assert [r[1] for r in row_texts(app)] == ["@ working copy ●"]
        await pilot.press("r")
        assert isinstance(app.screen, LaunchScreen)
        assert app.screen.current_spec().main_checkout
        await pilot.press("escape", "n")
        assert isinstance(app.screen, NewBranchScreen)
        app.screen.query_one("#name", Input).value = "topic"
        await pilot.press("enter")
        await wait_until(pilot, lambda: app.selected_branch_name() == "topic")
        await pilot.press("r")
        assert isinstance(app.screen, LaunchScreen)
        app.screen.query_one("#prompt", TextArea).text = "exit:0"
        for protected in ("main", "master", "trunk"):
            app.screen.query_one("#branch", Input).value = protected
            await pilot.press("ctrl+s")
            assert isinstance(app.screen, LaunchScreen) and not app.jobs.jobs
            assert app.branch(protected) is None
        app.screen.query_one("#branch", Input).value = "topic"
        await pilot.press("ctrl+s")
        await wait_until(pilot, lambda: "topic" in app.jobs.jobs)
        assert "--branch=topic" in app.jobs.jobs["topic"].argv
        assert "@" not in app.jobs.jobs

    run_app(jj_repo_no_bookmarks, fake_cfg(), scenario)


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


@pytest.mark.parametrize("command", ["", "my-agent --model example"])
def test_host_agent_hint_and_preview_match_command(
    repo: Path, fake_cfg: Callable[..., Config], command: str
) -> None:
    from project_sandbox_tui.config import remember

    cfg = fake_cfg()
    cfg.ui.host_agent_command = command
    remember(repo, "codex", "sandbox-model", "high", 300)
    executable = "my-agent" if command else "codex"

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        assert f"host agent ({executable})" in app.export_screenshot().replace("&#160;", " ")
        await pilot.press("j", "P")
        assert isinstance(app.screen, MessageScreen)
        assert f"host agent command (I)\n{command or executable}" in app.screen.body

    run_app(repo, cfg, scenario)


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


def test_narrow_terminal_shortens_headings(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    def shot(app: SandboxTUI) -> str:
        return app.export_screenshot().replace("&#160;", " ")

    def header(app: SandboxTUI) -> str:
        widget = app.main.query_one("#header", Static)
        return widget.render_line(0).text

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        assert f"{repo}   2 bookmarks · 0 running" in header(app)
        assert "vs main" in shot(app)

        await pilot.resize_terminal(60, 30)
        await pilot.pause()
        assert "  repo   2 bookmarks · 0 running" in header(app)
        screen = shot(app)
        assert "vs main" not in screen and "status" in screen  # the subject column shrinks
        # Cheat-sheet sections that share a row are not cut off by wider ones below.
        assert "cancel · 3×: forget" in screen and "working copy to bookmark" in screen
        await pilot.press("H")
        await pilot.pause()
        assert "q quit" in shot(app)

        await pilot.resize_terminal(40, 30)
        await pilot.pause()
        assert "repo   2 bm · 0 run" in header(app)

        await pilot.resize_terminal(wide, 30)
        await pilot.pause()
        assert "vs main" in shot(app) and str(repo) in header(app)

    # Room for the full path beside the cheat-sheet: macOS temp paths are longer than Linux ones.
    wide = 120 + len(str(repo))
    run_app(repo, fake_cfg(), scenario, size=(wide, 30))


@pytest.mark.parametrize("mode", ["i", "r"])
def test_launch_form_layout_survives_resizing(
    repo: Path, fake_cfg: Callable[..., Config], mode: str
) -> None:
    """A scrollbar from a short terminal must not stay and hide the hint once it grows."""
    from textual.containers import Vertical

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j")  # select feature/x for sessions
        await pilot.press(mode)
        form = app.screen
        assert isinstance(form, LaunchScreen)
        box = form.query_one(Vertical)

        def layout() -> tuple[object, ...]:
            return (box.size, box.show_vertical_scrollbar, box.max_scroll_y)

        await pilot.pause()
        fresh = layout()
        for size in ((40, 12), (60, 12)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
            await pilot.resize_terminal(60, 30)
            await pilot.pause()
            assert layout() == fresh
        if mode == "i":  # the unsupervised-run form is taller than 30 rows and scrolls
            assert not box.show_vertical_scrollbar
            assert "esc cancel" in app.export_screenshot().replace("&#160;", " ")

    run_app(repo, fake_cfg(), scenario, size=(60, 30))


@pytest.mark.parametrize("target", ["@", "feature/x"])
@pytest.mark.parametrize("terminal", ["inline", "tmux", "zellij", "screen"])
def test_enter_that_launches_interactive_session_does_not_leak(
    repo: Path,
    fake_cfg: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
    target: str,
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
        if target != "@":
            await pilot.press("j")
        # headless drivers cannot suspend; the session itself is what we stub out
        monkeypatch.setattr(app, "suspend", contextlib.nullcontext)
        await pilot.press("i")
        assert isinstance(app.screen, LaunchScreen)
        assert "opens in" not in str(app.screen.query_one(".title").render())
        await pilot.press("enter")  # launch from the agent field
        await wait_until(pilot, lambda: target in app.jobs.jobs)
        await wait_until(pilot, lambda: not app.jobs.jobs[target].running)
        job = app.jobs.jobs[target]
        assert not job.external
        assert job.exit_code == 0
        assert (f"--branch={target}" in job.argv) == (target != "@")
        await wait_until(pilot, lambda: app.screen is app.main)
        await pilot.pause(0.3)  # and nothing is pushed afterwards
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
    agent = Path(__file__).with_name("fake_interactive_agent.py")
    cfg = fake_cfg()
    cfg.sandbox.command = f"{sys.executable} {agent}"
    cfg.ui.terminal = "tmux"
    name = "host:feature/x"

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j")  # feature/x
        assert app.selected_branch_name() == "feature/x"
        await pilot.press("i")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        assert "opens in tmux" in str(form.query_one(".title").render())
        await pilot.press("enter")
        await wait_until(pilot, lambda: "feature/x" in app.jobs.jobs)
        job = app.jobs.jobs["feature/x"]
        assert job.external and job.running

        def window_exists() -> bool:
            return (
                "feature/x"
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
        subprocess.run(["tmux", "-f", "/dev/null", "new-session", "-d", "-s", "host"], check=True)
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
        await pilot.press("j")  # select feature/x for sessions
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
        await pilot.press("space")  # the working copy can be marked, for :* and actions
        assert app.selection() == ["@"]
        await pilot.press("escape", "space")  # space moved down: mark the first bookmark
        assert app.selection() == [app.visible_rows()[1].branch.name]
        assert row_texts(app)[1][1].startswith("+ ")
        await pilot.press("space")
        assert len(app.selection()) == 2
        assert "2 selected" in str(app.main.query_one("#header", Static).content)
        await pilot.press("escape")
        assert app.selection() == []
        # visual range: v at the top, G extends to the bottom
        await pilot.press("g", "g", visual_key, "G")
        assert len(app.selection()) == 4
        assert "VISUAL" in str(app.main.query_one("#header", Static).content)
        await pilot.press(visual_key)  # end visual, keep the range as marks
        await pilot.press("g", "g")
        assert len(app.selection()) == 4 and app._visual_anchor is None
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

    from project_sandbox_tui.sandbox import LaunchSpec
    from project_sandbox_tui.vcs import workspace_path

    add_bookmark(repo, "missing", "missing.txt", "missing\n")
    add_bookmark(repo, "spare", "spare.txt", "spare\n")
    missing_path = workspace_path(repo, "missing", None)
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
            (["test-editor", str(missing_path)], missing_path),
            (["test-editor", str(feature_path)], feature_path),
            (["test-editor", str(main_path)], main_path),
        ]
        # The missing workspace is created on the bookmark, named like project-sandbox's.
        assert (missing_path / "missing.txt").read_text() == "missing\n"
        assert "missing" in jj(repo, "workspace", "list", "-T", 'stringify(name) ++ "\\n"').split()
        assert any("missing: created workspace" in str(n.message) for n in app._notifications)

        # A registered workspace whose directory is gone is reported, not opened.
        shutil.rmtree(missing_path)
        app.marked = ["missing"]
        await pilot.press("o")
        assert len(opened) == 3
        assert any(
            "missing: could not create a workspace" in str(n.message) for n in app._notifications
        )

        # A session that is starting creates its own workspace: never race it.
        app.jobs.record_external(LaunchSpec(branch="spare", agent="bash"), ["true"])
        app.marked = ["spare"]
        await pilot.press("o")
        assert len(opened) == 3 and not workspace_path(repo, "spare", None).exists()
        assert any("spare: workspace not created yet" in str(n.message) for n in app._notifications)

        # A bookmark on @'s parent still uses its own workspace; only @ opens the repo.
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
        screen = app.screen
        assert isinstance(screen, MessageScreen)
        await wait_until(pilot, lambda: screen.query_one(VerticalScroll).scroll_y > 0)
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
        if target == "main":
            assert app.screen is form and not app.jobs.jobs
            assert form.query_one("#prompt", TextArea).text == "exit:0"
            assert any(f"{target} is protected" in str(n.message) for n in app._notifications)
            return
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
        await pilot.press("j")  # select feature/x for sessions
        await pilot.press("r")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        panel = form.query_one("Vertical")
        height = panel.size.height
        form.query_one("#prompt", TextArea).text = "long prompt " * 1000
        scroll = form.query_one("#command-scroll")
        await wait_until(pilot, lambda: scroll.max_scroll_y > 0)  # preview updated and laid out
        assert panel.size.height == height
        assert scroll.size.height == 3
        assert scroll.max_scroll_y > 0
        scroll.scroll_end(animate=False)  # deferred until after a refresh
        await wait_until(pilot, lambda: scroll.scroll_y > 0)
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
        await wait_for_screen(app, pilot, CleanupScreen)
        assert isinstance(app.screen, CleanupScreen)
        assert [(ws.name, ws.removable) for ws in app.screen.workspaces] == [
            ("feature/x", True),
            ("dirty", False),
        ]
        assert app.screen.selected == {0}
        await pilot.press("escape")
        assert not isinstance(app.screen, CleanupScreen)
        await app.workers.wait_for_complete()
        assert path.exists()

        await pilot.press("W")
        await wait_for_screen(app, pilot, CleanupScreen)
        await pilot.press("a")  # deselect all
        await pilot.press("enter")  # nothing selected: nothing happens
        await app.workers.wait_for_complete()
        assert path.exists()

        await pilot.press("W")
        await wait_for_screen(app, pilot, CleanupScreen)
        await pilot.press("enter")  # asks first
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await wait_until(pilot, lambda: not path.exists())
        assert dirty.exists()
        assert app.is_running

        run("jj", "workspace", "forget", "dirty", cwd=repo)
        await pilot.press("W")  # nothing left: just a message
        await wait_for_screen(app, pilot, MessageScreen)
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
        await pilot.press("space")  # the working copy: marked, but left out of the rebase
        for name in ("a", "b"):
            app.table.move_cursor(row=rows.index(name), animate=False)
            await pilot.press("space")
        assert app.selection() == ["@", "a", "b"]  # marking order
        await pilot.press("r")
        form = app.screen
        assert isinstance(form, LaunchScreen) and form.octopus is not None
        assert form.octopus.inputs == ["a", "b"]
        assert any("working copy is left out" in str(n.message) for n in app._notifications)
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
        await wait_for_screen(app, pilot, CleanupScreen)
        await pilot.press("D")
        await wait_for_screen(app, pilot, ConfirmScreen)
        assert isinstance(app.screen, ConfirmScreen)
        assert "uncommitted changes" in app.screen.body
        assert f"Delete directory and all contents: {dirty}" in app.screen.body
        assert "  untracked" in app.screen.body
        assert "  a.txt" in app.screen.body
        assert "Unregister workspace: dirty" in app.screen.body
        await pilot.press("n")  # cancelled: nothing happens
        await app.workers.wait_for_complete()
        assert dirty.exists()

        await pilot.press("W")
        await wait_for_screen(app, pilot, CleanupScreen)
        await pilot.press("D")
        await wait_for_screen(app, pilot, ConfirmScreen)
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
        monkeypatch.setattr(app, "_review_diff", lambda base, target: shown.append((base, target)))
        monkeypatch.setattr(
            app, "_review_commits", lambda base, target: shown.append((base, target))
        )
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


@pytest.mark.parametrize("failure", [FileNotFoundError("jj missing"), 1, 130, -signal.SIGINT])
def test_diff_viewer_failure_recovers(
    repo: Path,
    fake_cfg: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException | int,
) -> None:
    import contextlib

    commits = [
        jj(repo, "log", "--no-graph", "-r", r, "-T", "commit_id") for r in ("main", "feature/x")
    ]

    def viewer(argv: list[str], *, cwd: Path) -> int:
        base, target = (f'"{c}"' for c in commits)
        assert argv[:7] == ["jj", "diff", "--from", base, "--to", target, 'root-file:"b.txt"']
        assert "--config=ui.paginate=auto" in argv
        assert cwd == repo
        if isinstance(failure, BaseException):
            raise failure
        return failure

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        monkeypatch.setattr(app, "_suspended", contextlib.nullcontext)
        monkeypatch.setattr("project_sandbox_tui.terminal.run_foreground", viewer)
        app.marked = ["main", "feature/x"]
        await pilot.press("equals_sign")
        await wait_for_screen(app, pilot, DiffReviewScreen)
        await pilot.press("enter")
        if failure not in (130, -signal.SIGINT):
            await wait_until(
                pilot, lambda: any("jj diff" in str(n.message) for n in app._notifications)
            )
        await pilot.press("q", "question_mark")
        assert isinstance(app.screen, HelpScreen)

    run_app(repo, fake_cfg(), scenario)


def test_diff_review_marks_files_seen(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib

    (repo / "c.txt").write_text("c\n" * 3)
    (repo / "a.txt").write_text("changed\n")
    viewed: list[list[str]] = []

    def viewer(argv: list[str], *, cwd: Path) -> int:
        viewed.append([arg for arg in argv[6:] if not arg.startswith("--config=")])
        return 0

    def review_rows(app: SandboxTUI) -> list[list[str]]:
        table = app.screen.query_one(DataTable)
        return [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]

    async def open_review(app: SandboxTUI, pilot: Any, *keys: str) -> DiffReviewScreen:
        if keys:
            app.marked = ["main", "@"]
        await pilot.press(*keys, "equals_sign")
        return await wait_for_screen(app, pilot, DiffReviewScreen)

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        monkeypatch.setattr(app, "_suspended", contextlib.nullcontext)
        monkeypatch.setattr("project_sandbox_tui.terminal.run_foreground", viewer)
        assert str(app.table.get_cell("@", "reviewed")) == "0/2"
        assert str(app.table.get_cell("feature/x", "reviewed")) == "0/1"
        assert str(app.table.get_cell("main", "reviewed")) == ""
        app._refresh_timer.pause()  # returning from review must refresh immediately
        screen = await open_review(app, pilot, "g", "g")  # direct comparison: main to @
        assert review_rows(app) == [
            ["●", "M", "a.txt", "+1 −1", "8 B"],
            ["●", "A", "c.txt", "+3", "6 B"],
        ]
        title = str(screen.query_one("#review-title", Static).render())
        assert "@ → main" not in title and "main → @" in title and "0/2 seen" in title
        await pilot.press("space")  # a.txt seen, cursor moves to c.txt
        assert [r[0] for r in review_rows(app)] == ["✓", "●"]
        await pilot.press("l", "equals_sign")
        assert viewed == [['root-file:"c.txt"'], ['root-file:"c.txt"']]  # file, then unseen
        await pilot.press("A")
        assert [r[0] for r in review_rows(app)] == ["✓", "✓"]
        await app.refresh_data()
        assert str(app.table.get_cell("@", "reviewed")) == "0/2"
        await pilot.press("equals_sign")  # all seen: every file
        assert viewed[-1] == ['root-file:"a.txt" | root-file:"c.txt"']
        await pilot.press("A", "k", "space", "q")
        assert app.screen is app.main
        await wait_until(pilot, lambda: str(app.table.get_cell("@", "reviewed")) == "0/2")
        # Only c.txt changes: a.txt stays seen.
        (repo / "c.txt").write_text("c\n")
        await open_review(app, pilot, "g", "g")
        assert [r[:3] for r in review_rows(app)] == [["✓", "M", "a.txt"], ["●", "A", "c.txt"]]
        await pilot.press("q")
        # Direct-comparison marks do not count toward per-commit file review.
        (repo / "a.txt").write_text("changed again\n")
        await app.refresh_data()
        assert str(app.table.get_cell("@", "reviewed")) == "0/2"
        # Both diffs changed, so another bookmark also shows them as unseen.
        jj(repo, "bookmark", "create", "--quiet", "wip", "-r", "@")
        await app.refresh_data()
        assert str(app.table.get_cell("wip", "reviewed")) == "0/2"
        app.marked = ["main", "wip"]
        await open_review(app, pilot)
        assert [r[0] for r in review_rows(app)] == ["●", "●"]
        await pilot.press("q")
        app.marked = ["main", "main"]
        await pilot.press("equals_sign")
        await wait_until(
            pilot, lambda: any("no differences" in str(n.message) for n in app._notifications)
        )

    run_app(repo, fake_cfg(), scenario)


@pytest.mark.parametrize("direct", [False, True])
def test_review_marks_follow_shared_diffs_across_bookmarks(
    repo: Path, fake_cfg: Callable[..., Config], direct: bool
) -> None:
    jj(repo, "new", "feature/x", "-m", "shared changes")
    (repo / "c.txt").write_text("c\n")
    (repo / "d.txt").write_text("d\n")
    jj(repo, "bookmark", "create", "feature-1", "-r", "@")
    jj(repo, "new", "-m", "follow-up")
    (repo / "e.txt").write_text("e\n")
    jj(repo, "bookmark", "create", "feature-2", "-r", "@")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app._refresh_timer.pause()

        async def open_review(bookmark: str) -> DiffReviewScreen:
            app.marked = ["main", bookmark] if direct else [bookmark]
            await pilot.press("equals_sign")
            screen = await wait_for_screen(app, pilot, DiffReviewScreen)
            if not direct:
                await pilot.press("right_square_bracket")  # shared c.txt / d.txt commit
            return screen

        async def close_review() -> None:
            await pilot.press("q")
            await wait_until(pilot, lambda: app.screen is app.main)
            await app.workers.wait_for_complete()

        def count(bookmark: str) -> str:
            return str(app.table.get_cell(bookmark, "reviewed"))

        screen = await open_review("feature-1")
        row = next(i for i, f in enumerate(screen.files) if f.path == "c.txt")
        screen.query_one(DataTable).move_cursor(row=row)
        await pilot.press("space")
        assert screen.seen == {screen.files[row].key}
        await close_review()
        assert count("feature-1") == ("0/3" if direct else "1/3")
        assert count("feature-2") == ("0/4" if direct else "1/4")
        assert count("feature/x") == "0/1"

        screen = await open_review("feature-2")
        assert {f.path for f in screen.files if f.key in screen.seen} == {"c.txt"}
        await pilot.press("A")
        await close_review()
        assert count("feature-1") == ("0/3" if direct else "2/3")
        assert count("feature-2") == ("0/4" if direct else "2/4")

        # New and renamed bookmarks inherit saved marks without copying them.
        jj(repo, "bookmark", "create", "later", "-r", "feature-1")
        jj(repo, "bookmark", "rename", "feature-1", "renamed")
        await app.refresh_data()
        screen = await open_review("later")
        assert len(screen.seen) == len(screen.files)
        await pilot.press("A")  # unmark all from the new bookmark
        await close_review()
        assert count("renamed") == "0/3"
        assert count("feature-2") == "0/4"
        screen = await open_review("feature-2")
        assert {f.path for f in screen.files if f.key in screen.seen} == (
            {"e.txt"} if direct else set()
        )
        await close_review()

    run_app(repo, fake_cfg(), scenario)


def test_bookmark_review_counts_persist_and_follow_main(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    from project_sandbox_tui.review import SeenFiles

    backend = detect(repo)
    commits = backend.review_commits("main", "feature/x")
    key = backend.changed_files_for_commit(commits[0]).files[0].key
    seen = SeenFiles.for_repo(repo)
    seen.mark([key, "obsolete-diff"], True)

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        assert str(app.table.get_cell("feature/x", "reviewed")) == "1/1"

        # Changing marks in another instance refreshes counts without rereading the commit path.
        def unexpected_diff(*args: str, **kwargs: Any) -> None:
            raise AssertionError("unchanged tips should reuse commit identities")

        with monkeypatch.context() as patch:
            patch.setattr(app.backend, "review_file_keys", unexpected_diff)
            seen.mark([key], False)
            await app.refresh_data()
            assert str(app.table.get_cell("feature/x", "reviewed")) == "0/1"
            seen.mark([key], True)
            await app.refresh_data()
            assert str(app.table.get_cell("feature/x", "reviewed")) == "1/1"

        # Once main includes the change, there is no diff to review.
        jj(repo, "bookmark", "move", "main", "--to", "feature/x")
        await app.refresh_data()
        assert str(app.table.get_cell("feature/x", "reviewed")) == ""
        assert str(app.table.get_cell("@", "reviewed")) == ""
        app.cfg.project.main_branch = "missing"
        await app.refresh_data()
        assert str(app.table.get_cell("@", "reviewed")) == ""

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
        # The mark stays on screen regardless of commit recency (which can tie).
        assert sorted(names(app)) == ["feature/x", "fix/y"]
        header = str(app.main.query_one("#header", Static).content)
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
        return str(app.main.query_one("#header", Static).content)

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("G", "d")
        assert "d…" in header(app)
        await pilot.press("k")  # another key cancels dd
        assert "d…" not in header(app)
        await pilot.press("d")
        assert "d…" in header(app)
        await wait_until(pilot, lambda: "d…" not in header(app))  # and so does waiting
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
        await wait_for_screen(app, pilot, CleanupScreen)
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
        await pilot.press("escape")
        app.backend.create_branch("other", "main")
        await app.refresh_data()
        app.marked = ["other"]
        await pilot.press("r")  # drafts belong to their bookmark
        assert isinstance(app.screen, LaunchScreen)
        assert app.screen.query_one("#prompt", TextArea).text == ""
        await pilot.press("escape")
        app.marked = ["feature/x"]
        await pilot.press("r")
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


@pytest.mark.parametrize("target", ["@", "feature/x"])
@pytest.mark.parametrize("mode", ["tmux", "custom"])
def test_host_agent_opens_in_target_workspace(
    repo: Path,
    fake_cfg: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    mode: str,
) -> None:
    import shlex
    from functools import partial

    from project_sandbox_tui import launchers
    from project_sandbox_tui.config import remember

    cfg = fake_cfg(worktree_dir=str(repo.parent / "workspaces with 'quotes"))
    cfg.ui.terminal = mode
    if mode == "custom":
        cfg.ui.terminal_command = "terminal --cwd {repo} -- sh -c {cmd}"
        cfg.ui.host_agent_command = "wrapper {agent} --directory {path} --bookmark {branch}"
    monkeypatch.setenv("TMUX", "private-test-session")
    monkeypatch.setattr(
        launchers,
        "external_launch",
        partial(launchers.external_launch, which=lambda _: "/bin/tmux"),
    )
    opened: list[tuple[launchers.ExternalLaunch, Path]] = []
    monkeypatch.setattr(launchers, "start", lambda launch, cwd: opened.append((launch, cwd)))
    remember(repo, "codex", "sandbox-model", "high", 300)

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        path = app._workspace_dir(target)
        assert path.exists() == (target == "@")
        app.marked = [target]
        await pilot.press("I")
        assert app.screen is app.main
        assert path.is_dir()
        launch, cwd = opened.pop()
        assert cwd == path
        job = app.jobs.jobs[target]
        argv = ["codex"]
        if mode == "custom":
            argv = ["wrapper", "codex", "--directory", str(path), "--bookmark", target]
        assert job.host and job.external and job.running and job.argv == argv
        wrapped = launchers.host_command(argv, job.summary_path)
        if mode == "custom":
            assert launch.argv == [
                "terminal", "--cwd", str(path), "--", "sh", "-c",
                shlex.join(wrapped),
            ]  # fmt: skip
        else:
            assert launch.argv == [
                "tmux", "new-window", "-n", target, "-c", str(path),
                launchers.shell_script(wrapped),
            ]  # fmt: skip
        assert app.tasks.tasks == {}
        assert (
            next(r for r in app.visible_rows() if r.branch.name == target).status()[2]
            == "host agent (ext)"
        )
        assert " · host" in "\n".join(app._job_details(job))
        if target != "@":
            assert "host codex" in "\n".join(app._history_lines(target))
        # A duplicate host launch is refused and quitting sees the reservation.
        await pilot.press("I")
        assert not opened
        await pilot.press("q")
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("escape")
        job.status = "completed"
        app.jobs.save(job)
        # A later host launch reuses the workspace and archives the old run.
        await pilot.press("I")
        assert opened.pop()[1] == path
        assert app.jobs.history(target)[0].host

    run_app(repo, cfg, scenario)


def test_open_many_host_agents_asks_first(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    add_bookmark(repo, "a", "a2.txt", "a\n")
    add_bookmark(repo, "b", "b2.txt", "b\n")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        monkeypatch.setattr(app, "_open_host_agent", lambda name, argv=(): opened.append(name))
        app.marked = ["a", "b", "feature/x", "@"]
        await pilot.press("I")
        assert isinstance(app.screen, ConfirmScreen) and opened == []
        await pilot.press("escape")
        assert opened == []
        await pilot.press("I", "y")
        assert opened == ["a", "b", "feature/x", "@"]

    run_app(repo, fake_cfg(), scenario)


def test_host_agent_rejects_protected_or_busy_targets_and_reports_launch_failure(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    from project_sandbox_tui import launchers
    from project_sandbox_tui.sandbox import LaunchSpec

    cfg = fake_cfg()
    cfg.ui.terminal = "custom"
    cfg.ui.terminal_command = "terminal {cmd}"

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("terminal not found")

    monkeypatch.setattr(launchers, "start", fail)

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app._refresh_timer.pause()
        notices: list[str] = []
        monkeypatch.setattr(app, "notify", lambda message, **_kw: notices.append(message))
        app.marked = ["main"]
        await pilot.press("I")
        assert "main is protected" in notices[-1]
        assert not app._workspace_dir("main").exists()
        reservation = app.jobs.record_external(LaunchSpec("feature/x", "claude"), ["other"])
        app.marked = ["feature/x"]
        await pilot.press("I")
        assert "feature/x" in notices[-1] and "host agent" not in notices[-1]
        assert not app._workspace_dir("feature/x").exists()
        app.jobs.discard(reservation)
        await pilot.press("I")
        assert notices[-1] == "feature/x: could not start host agent: terminal not found"
        assert app.jobs.jobs == {}
        await app.run_command("host my-agent --model example")
        assert LastUsed.load(repo).host_command == []

    run_app(repo, cfg, scenario)


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
        assert "main is protected; create a bookmark" in hint()

    run_app(repo, fake_cfg(), scenario)


@pytest.mark.parametrize("key", ["u", "q"])
def test_stale_working_copy_dialog(
    repo: Path, tmp_path: Path, fake_cfg: Callable[..., Config], key: str
) -> None:
    from project_sandbox_tui.screens.dialogs import StaleWorkspaceScreen
    from tests.conftest import make_stale

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        make_stale(repo, tmp_path / "other")
        await wait_for_screen(app, pilot, StaleWorkspaceScreen)
        await pilot.press("x")  # neither choice: the dialog stays
        await pilot.pause(0.5)  # refreshes meanwhile do not stack more dialogs
        assert isinstance(app.screen, StaleWorkspaceScreen)
        assert sum(isinstance(s, StaleWorkspaceScreen) for s in app.screen_stack) == 1
        await pilot.press(key)
        if key == "u":
            await wait_until(
                pilot,
                lambda: (
                    not (repo / "wip.txt").exists()
                    and not isinstance(app.screen, StaleWorkspaceScreen)
                ),
            )
            await pilot.pause(0.5)  # later refreshes do not reopen it
            assert not isinstance(app.screen, StaleWorkspaceScreen)
            assert app.is_running
        else:
            await wait_until(pilot, lambda: not app.is_running)

    run_app(repo, fake_cfg(), scenario)


def test_unwritable_run_records_do_not_crash_the_app(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    notices: list[str] = []

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        def broken(*_args: Any) -> Any:
            raise OSError("No space left on device")

        monkeypatch.setattr(app, "notify", lambda message, **_kw: notices.append(message))
        monkeypatch.setattr(app.jobs, "refresh", broken)
        monkeypatch.setattr(app.tasks, "refresh", broken)
        await app.refresh_data()
        await app.refresh_data()
        assert app.is_running
        assert notices == [
            "could not update the run records: No space left on device",
            "could not update the task records: No space left on device",
        ]  # once each while the problem lasts

    run_app(repo, fake_cfg(), scenario)


def test_overlapping_repository_actions_keep_their_records(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    """A second action no longer cancels the first one's worker mid-way."""

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app._refresh_timer.pause()
        app._create_branch(("topic", "main"))
        app._fast_forward_main("main", "feature/x")  # same worker group, right after
        await app.workers.wait_for_complete()
        undo = [(e.op, e.names()) for e in app.graph.state.undo]
        assert undo == [("create", "topic"), ("move", "main")]
        assert app.graph.state.nodes[app.graph.state.current["topic"]].kind == "create"

    run_app(repo, fake_cfg(), scenario)


def test_forgetting_a_finished_run_records_its_outcome(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    from project_sandbox_tui.sandbox import LaunchSpec

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app._refresh_timer.pause()
        spec = LaunchSpec("feature/x", "claude", mode="background", prompt_text="exit:0")
        job = app.jobs.start_background(spec, {})
        app._record_run(job)
        await wait_until(pilot, lambda: bool(app.jobs.refresh({})) or not job.running)
        assert job.status == "completed"
        # x before any refresh recorded the outcome: the record goes, the outcome stays.
        app._cancel(["feature/x"])
        assert "feature/x" not in app.jobs.jobs
        assert app.graph.state.history("feature/x")[0].status == "completed"

    run_app(repo, fake_cfg(), scenario)


def test_workspace_creation_rechecks_sessions(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    import json
    import os

    from project_sandbox_tui.sandbox import sessions_dir
    from project_sandbox_tui.vcs import workspace_path

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app._refresh_timer.pause()
        app.sessions = {}  # the UI's cache, from before the session started
        path = workspace_path(repo, "feature/x", None)
        record = {
            "session_id": "s1",
            "workspace_path": str(path),
            "status": "running",
            "pid": os.getpid(),
        }
        sessions_dir().mkdir(parents=True, exist_ok=True)
        (sessions_dir() / "s1.json").write_text(json.dumps(record))
        assert app._ensure_workspace("feature/x") is None  # its session creates it
        assert not path.exists()

    run_app(repo, fake_cfg(), scenario)


@pytest.mark.parametrize("target", ["@", "feature/x"])
@pytest.mark.parametrize("code", [0, 7, 127])
def test_inline_host_agent_is_tracked_until_return(
    repo: Path,
    fake_cfg: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    code: int,
) -> None:
    import shlex
    import subprocess
    import sys
    from contextlib import nullcontext

    from project_sandbox_tui import terminal

    cfg = fake_cfg()
    cfg.ui.terminal = "inline"
    marker = repo.parent / "host-cwd"
    argv = [str(repo.parent / "missing-host-agent")] if code == 127 else [
        sys.executable, "-c",
        "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text(str(pathlib.Path.cwd())); "
        "sys.exit(int(sys.argv[2]))",
        str(marker), str(code),
    ]  # fmt: skip
    cfg.ui.host_agent_command = shlex.join(argv)
    spawn = subprocess.Popen

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        monkeypatch.setattr(app, "_suspended", nullcontext)
        monkeypatch.setattr(terminal, "preserved", nullcontext)
        monkeypatch.setattr("builtins.input", lambda *_args: "")
        spawned: list[bool] = []

        def checked_spawn(args, *args_, **kwargs):
            if args == argv:
                other = JobManager(repo, cfg)
                assert other.busy_reason(target, {})
                assert other.jobs[target].host and other.jobs[target].running
                assert kwargs["cwd"] == app._workspace_dir(target)
                spawned.append(True)
            return spawn(args, *args_, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", checked_spawn)
        app.marked = [target]
        await pilot.press("I")
        assert spawned == [True]
        done = JobManager(repo, cfg).jobs[target]
        assert done.host and not done.external and done.exit_code == code
        assert done.status == ("completed" if code == 0 else "failed")
        assert not app.jobs.busy_reason(target, {})
        if code != 127:
            assert marker.read_text() == str(app._workspace_dir(target))

    run_app(repo, cfg, scenario)


@pytest.mark.parametrize("mode", ["inline", "custom"])
def test_host_launch_rechecks_reservation_after_creating_workspace(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from project_sandbox_tui import launchers
    from project_sandbox_tui.sandbox import LaunchSpec

    cfg = fake_cfg()
    cfg.ui.terminal = mode
    cfg.ui.terminal_command = "terminal {cmd}"

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        ensure = app._ensure_workspace
        other = JobManager(repo, cfg)
        notices: list[str] = []
        monkeypatch.setattr(app, "notify", lambda message, **_kw: notices.append(message))

        def reserve(name: str) -> Path | None:
            path = ensure(name)
            other.record_external(LaunchSpec(name, "claude"), ["other"])
            return path

        def unexpected(*_args, **_kwargs):
            pytest.fail("busy target must not launch a host agent")

        monkeypatch.setattr(app, "_ensure_workspace", reserve)
        monkeypatch.setattr(app, "_suspended", unexpected)
        monkeypatch.setattr(launchers, "start", unexpected)
        app.marked = ["feature/x"]
        await pilot.press("I")
        assert "already has a interactive session" in notices[-1]
        assert app.jobs.jobs["feature/x"].argv == ["other"]

    run_app(repo, cfg, scenario)


@pytest.mark.parametrize("back", ["q", "escape", "h"])
def test_commit_review_resumes_commit_and_row(
    repo: Path, fake_cfg: Callable[..., Config], back: str
) -> None:
    jj(repo, "new", "feature/x", "-m", "many files")
    for index in range(35):
        (repo / f"file-{index:02}.txt").write_text(f"{index}\n")
    jj(repo, "bookmark", "move", "feature/x", "--to", "@")
    jj(repo, "new", "feature/x", "-m", "follow-up")
    (repo / "tail.txt").write_text("tail\n")
    jj(repo, "bookmark", "move", "feature/x", "--to", "@")
    jj(repo, "bookmark", "create", "other", "-r", "feature/x")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app._refresh_timer.pause()

        async def open_review(bookmark: str) -> CommitReviewScreen:
            app.marked = [bookmark]
            await pilot.press("equals_sign")
            return await wait_for_screen(app, pilot, CommitReviewScreen)

        async def close_review() -> None:
            await pilot.press(back)
            await wait_until(pilot, lambda: app.screen is app.main)

        screen = await open_review("feature/x")
        first = screen.current_commit.commit
        await pilot.press("right_square_bracket", "G")
        saved = screen.current_commit.commit
        assert screen.current_row == 34
        await close_review()

        # A second bookmark has its own position, even when its commits match.
        screen = await open_review("other")
        assert screen.current_commit.commit == first and screen.current_row == 0
        await pilot.press("right_square_bracket", "j")
        await close_review()
        screen = await open_review("feature/x")
        assert screen.current_commit.commit == saved and screen.current_row == 34
        assert screen.files[screen.current_row].display == "file-34.txt"
        await wait_until(pilot, lambda: screen.query_one(DataTable).scroll_y > 0)
        await close_review()

        # Resolve the saved ID again: its index changes when main advances.
        jj(repo, "bookmark", "move", "main", "--to", first)
        screen = await open_review("feature/x")
        assert screen.commit_index == 0
        assert screen.current_commit.commit == saved and screen.current_row == 34
        await close_review()

        # Rewriting the saved commit invalidates both the ID and its file row.
        jj(repo, "describe", "-r", saved, "-m", "rewritten")
        screen = await open_review("feature/x")
        assert screen.current_commit.commit != saved
        assert screen.commit_index == 0 and screen.current_row == 0
        await close_review()

    run_app(repo, fake_cfg(), scenario)


def test_direct_diff_review_resumes_row_and_clamps_after_changes(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    (repo / "a.txt").write_text("changed\n")
    (repo / "c.txt").write_text("new\n")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app._refresh_timer.pause()

        async def open_review() -> DiffReviewScreen:
            app.marked = ["main", "@"]
            await pilot.press("equals_sign")
            return await wait_for_screen(app, pilot, DiffReviewScreen)

        async def close_review() -> None:
            await pilot.press("q")
            await wait_until(pilot, lambda: app.screen is app.main)

        screen = await open_review()
        await pilot.press("G")
        assert screen.current_row == 1
        await close_review()
        screen = await open_review()
        assert screen.current_row == 1
        await close_review()

        # The same revisions now have fewer files; clamp to a valid row.
        (repo / "c.txt").unlink()
        screen = await open_review()
        assert len(screen.files) == 1 and screen.current_row == 0
        await close_review()

    run_app(repo, fake_cfg(), scenario)


def test_commit_review_groups_persists_and_invalidates_rewritten_commits(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    from project_sandbox_tui.review import SeenFiles

    jj(repo, "new", "feature/x", "-m", "remove b, add c")
    (repo / "b.txt").unlink()
    (repo / "c.txt").write_text("c\n" * 3)
    jj(repo, "bookmark", "move", "feature/x", "--to", "@")
    backend = detect(repo)
    original = backend.review_commits("main", "feature/x")
    changes = [backend.changed_files_for_commit(commit) for commit in original]
    viewed: list[list[str]] = []

    async def open_review(app: SandboxTUI, pilot: Any) -> CommitReviewScreen:
        await pilot.press("equals_sign")
        return await wait_for_screen(app, pilot, CommitReviewScreen)

    def rows(screen: CommitReviewScreen) -> list[list[str]]:
        table = screen.query_one(DataTable)
        return [[str(cell) for cell in table.get_row_at(i)] for i in range(table.row_count)]

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app._refresh_timer.pause()
        app.marked = ["feature/x"]
        monkeypatch.setattr(app, "_show_diff", lambda title, argv: viewed.append(argv))
        # The reverted b.txt remains reviewable in both commits.
        assert str(app.table.get_cell("feature/x", "reviewed")) == "0/3"
        screen = await open_review(app, pilot)
        assert rows(screen) == [["●", "A", "b.txt", "+1", "2 B"]]
        header = str(screen.query_one("#review-commit", Static).render())
        assert "commit 1/2" in header and "add b" in header
        await pilot.press("enter", "space", "right_square_bracket")
        assert viewed[-1] == app.backend.diff_commit_files_command(original[0], ["b.txt"])
        assert rows(screen) == [
            ["●", "D", "b.txt", "−1", "deleted"],
            ["●", "A", "c.txt", "+3", "6 B"],
        ]
        header = str(screen.query_one("#review-commit", Static).render())
        assert "commit 2/2" in header and "remove b, add c" in header
        await pilot.press("space", "l", "equals_sign")
        assert (
            viewed[-1]
            == viewed[-2]
            == app.backend.diff_commit_files_command(original[1], ["c.txt"])
        )
        await pilot.press("A", "equals_sign")
        assert viewed[-1] == app.backend.diff_commit_files_command(original[1], ["b.txt", "c.txt"])
        await pilot.press("left_square_bracket")
        assert rows(screen)[0][0] == "✓"
        await pilot.press("q")
        await wait_until(pilot, lambda: str(app.table.get_cell("feature/x", "reviewed")) == "3/3")
        keys = {f.key for change in changes for f in change.files}
        assert keys <= SeenFiles.for_repo(repo).seen()
        screen = await open_review(app, pilot)
        assert screen.seen == {changes[0].files[0].key}
        # An open review keeps its pinned files and patches after a rewrite.
        (repo / "d.txt").write_text("later edit\n")
        jj(repo, "status")
        await pilot.press("right_square_bracket", "equals_sign")
        assert rows(screen) == [
            ["✓", "D", "b.txt", "−1", "deleted"],
            ["✓", "A", "c.txt", "+3", "6 B"],
        ]
        assert viewed[-1] == app.backend.diff_commit_files_command(original[1], ["b.txt", "c.txt"])
        await pilot.press("q")
        await wait_until(pilot, lambda: str(app.table.get_cell("feature/x", "reviewed")) == "1/4")
        screen = await open_review(app, pilot)
        await pilot.press("right_square_bracket")
        assert not screen.seen and len(screen.files) == 3
        await pilot.press("q")
        await wait_until(pilot, lambda: app.screen is app.main)
        # Advancing main drops the ancestor's files from the path and count.
        jj(repo, "bookmark", "move", "main", "--to", original[0].commit)
        await app.refresh_data()
        assert str(app.table.get_cell("feature/x", "reviewed")) == "0/3"
        screen = await open_review(app, pilot)
        assert len(screen.commits) == 1 and screen.commits[0].subject == "remove b, add c"
        await pilot.press("A", "q")
        await wait_until(pilot, lambda: str(app.table.get_cell("feature/x", "reviewed")) == "3/3")
        # Appending a commit preserves marks in unchanged ancestors.
        jj(repo, "new", "feature/x", "-m", "follow-up")
        (repo / "c.txt").write_text("follow-up\n")
        jj(repo, "bookmark", "move", "feature/x", "--to", "@")
        await app.refresh_data()
        assert str(app.table.get_cell("feature/x", "reviewed")) == "3/4"
        screen = await open_review(app, pilot)
        assert len(screen.seen) == 3 and len(screen.commits) == 2
        await pilot.press("right_square_bracket")
        assert rows(screen) == [["●", "M", "c.txt", "+1 −3", "10 B"]]
        await pilot.press("q")

    run_app(repo, fake_cfg(), scenario)


@pytest.mark.parametrize("marked", [["@"], ["feature/x"], ["main", "feature/x"]])
def test_review_waits_for_mount(
    repo: Path,
    fake_cfg: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
    marked: list[str],
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app._refresh_timer.pause()
        mount_started = asyncio.Event()
        release_mount = asyncio.Event()
        original_mount = DiffReviewScreen.on_mount

        async def delayed_mount(screen: DiffReviewScreen) -> None:
            # Reproduce the interval where auto-focus precedes table/header setup.
            screen.set_focus(screen.query_one(DataTable))
            mount_started.set()
            await release_mount.wait()
            original_mount(screen)

        monkeypatch.setattr(DiffReviewScreen, "on_mount", delayed_mount)
        app.marked = marked
        app.action_diff()
        screen_type = CommitReviewScreen if len(marked) == 1 else DiffReviewScreen
        try:
            async with asyncio.timeout(10):
                await mount_started.wait()
                assert isinstance(app.focused, DataTable)
                assert app.focused.row_count == 0
                assert not str(app.screen.query_one("#review-title", Static).render())
                waiter = asyncio.create_task(wait_for_screen(app, pilot, screen_type))
                await asyncio.sleep(0)  # let the waiter check the unfinished mount
                returned_early = waiter.done()
                release_mount.set()
                screen = await waiter
                assert not returned_early
        finally:
            release_mount.set()
        assert screen.query_one(DataTable).row_count == len(screen.files)
        assert str(screen.query_one("#review-title", Static).render())
        if isinstance(screen, CommitReviewScreen):
            assert "commit 1/1" in str(screen.query_one("#review-commit", Static).render())
        await pilot.press("q")

    run_app(repo, fake_cfg(), scenario)


def test_commit_review_empty_commits_and_no_path(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app._refresh_timer.pause()
        viewed = []
        monkeypatch.setattr(app, "_show_diff", lambda title, argv: viewed.append(argv))
        await pilot.press("equals_sign")  # empty @ has no files to review
        screen = await wait_for_screen(app, pilot, CommitReviewScreen)
        assert screen.query_one(DataTable).row_count == 0
        assert "(no description set)" in str(screen.query_one("#review-commit", Static).render())
        await pilot.press("space", "A", "equals_sign", "enter", "right_square_bracket")
        assert not viewed and not screen.seen
        await pilot.press("q")
        await wait_until(pilot, lambda: app.screen is app.main)
        assert str(app.table.get_cell("@", "reviewed")) == ""
        app.marked = ["main"]
        await pilot.press("equals_sign")
        await wait_until(
            pilot, lambda: any("no commits to review" in str(n.message) for n in app._notifications)
        )
        assert app.screen is app.main

    run_app(repo, fake_cfg(), scenario)
