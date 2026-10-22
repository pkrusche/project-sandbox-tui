"""Rendering regressions for modal dialogs."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.app import App
from textual.containers import Vertical, VerticalScroll
from textual.geometry import Region
from textual.widgets import Input, OptionList, Static

from project_sandbox_tui.cleanup import Workspace
from project_sandbox_tui.screens.dialogs import CleanupScreen, NewBranchScreen


@pytest.mark.parametrize("start", ["@-", "main"])
def test_new_branch_inputs_remain_visible_when_focused(start: str) -> None:
    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(NewBranchScreen(start))
            await pilot.pause()
            name = app.screen.query_one("#name", Input)
            revision = app.screen.query_one("#start", Input)
            assert app.focused is name
            await pilot.press(*"topic/new")
            assert name.value == "topic/new"
            assert name.content_size.height == 1

            await pilot.press("tab", "end", "x")
            assert app.focused is revision
            assert revision.value == start + "x"
            assert revision.content_size.height == 1
            assert name.content_size.height == 1

            await pilot.press("shift+tab")
            assert app.focused is name
            assert name.content_size.height == 1

    asyncio.run(scenario())


def _cleanup_rows(count: int) -> list[Workspace]:
    reason = "directory not found (expected at /a/very/long/path/" + "deep/" * 20 + "end-marker)"
    return [Workspace(f"ws-{i}", Path(f"/tmp/ws-{i}"), reason) for i in range(count)]


def test_cleanup_reasons_wrap_and_list_scrolls() -> None:
    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test(size=(80, 20)) as pilot:
            app.push_screen(CleanupScreen(_cleanup_rows(12)))
            await pilot.pause()
            options = app.screen.query_one(OptionList)
            hint = app.screen.query_one(".hint")
            # The whole reason is shown (wrapped), not cut off at the dialog edge.
            rendered = "\n".join(
                line.text for line in options.render_lines(Region(0, 0, *options.size))
            )
            assert "end-marker)" in rendered.replace("\n", "")
            # The list scrolls inside the dialog; the key hint stays on screen.
            assert options.max_scroll_y > 0
            assert hint.region.bottom <= app.screen.region.bottom
            assert app.screen.query_one(".title").region.y >= 0
            await pilot.press("end")
            await pilot.pause()
            assert options.scroll_y > 0

            app.pop_screen()
            app.push_screen(CleanupScreen(_cleanup_rows(1)))
            await pilot.pause()
            dialog = app.screen.query_one(Vertical)
            assert dialog.region.height < 14  # a short list keeps the dialog compact

    asyncio.run(scenario())


def test_cleanup_toggles_only_removable_rows() -> None:
    rows = [
        Workspace("done", Path("/tmp/done"), "clean", removable=True),
        Workspace("old", None, "merged into main", removable=True, branch="old"),
        Workspace("busy", Path("/tmp/busy"), "running session"),
    ]

    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test(size=(80, 24)) as pilot:
            screen = CleanupScreen(rows)
            app.push_screen(screen)
            await pilot.pause()
            assert screen.selected == {0, 1}
            await pilot.press("space")  # first row
            assert screen.selected == {1}
            await pilot.press("j", "j", "space")  # kept rows cannot be selected
            assert screen.selected == {1}
            await pilot.press("a")
            assert screen.selected == set()
            await pilot.press("a")
            assert screen.selected == {0, 1}

    asyncio.run(scenario())


def test_help_fits_a_small_terminal() -> None:
    """Every help entry must be visible on 80x24; the last one used to be cut off."""
    from project_sandbox_tui.screens.dialogs import HELP, HelpScreen

    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(HelpScreen())
            await pilot.pause()
            shot = app.export_screenshot().replace("&#160;", " ")
            for column in HELP:
                for title, keys in column:
                    assert title in shot, title
                    for _key, what in keys:
                        # Long descriptions wrap; their first word must still be there.
                        assert what.split()[0] in shot, what
            assert "any key to close" in shot

    asyncio.run(scenario())


def test_help_wraps_under_its_own_column_when_narrow() -> None:
    """Narrow terminals stack the columns and scroll; wrapped text keeps its indent."""
    from project_sandbox_tui.screens.dialogs import HelpScreen

    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test(size=(40, 20)) as pilot:
            app.push_screen(HelpScreen())
            await pilot.pause()
            body = app.screen.query_one(VerticalScroll)
            assert body.max_scroll_y > 0
            hint = app.screen.query_one(".hint")
            assert hint.region.bottom <= app.screen.region.bottom
            text = body.query_one(Static)
            lines = [line.text for line in text.render_lines(Region(0, 0, *text.size))]
            # The v / space description wraps: the continuation starts in the
            # description column, not back at the left edge.
            index = next(i for i, line in enumerate(lines) if "select a range" in line)
            continuation = lines[index + 1]
            assert continuation.strip()
            assert continuation.index(continuation.strip()) == lines[index].index("select")

            await pilot.press("j")
            assert isinstance(app.screen, HelpScreen)  # scrolls instead of closing
            assert body.scroll_y == 1
            await pilot.press("x")
            assert not isinstance(app.screen, HelpScreen)

    asyncio.run(scenario())


def test_confirmation_contents_scroll_without_dismissing() -> None:
    from textual.containers import VerticalScroll

    from project_sandbox_tui.screens.dialogs import ConfirmScreen

    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test(size=(80, 24)) as pilot:
            screen = ConfirmScreen("delete?", "\n".join(f"file-{i}" for i in range(100)), "delete")
            app.push_screen(screen)
            await pilot.pause()
            scroll = screen.query_one(VerticalScroll)
            assert scroll.max_scroll_y > 0
            assert screen.query_one(".hint").region.bottom <= screen.region.bottom
            await pilot.press("end")
            await pilot.pause()
            assert app.screen is screen and scroll.scroll_y > 0
            await pilot.press("n")
            assert app.screen is not screen

    asyncio.run(scenario())
