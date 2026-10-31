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


def test_cleanup_fits_a_narrow_terminal() -> None:
    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test(size=(60, 16)) as pilot:
            app.push_screen(CleanupScreen(_cleanup_rows(12)))
            await pilot.pause()
            options = app.screen.query_one(OptionList)
            # Focus must not add a border that the layout leaves no room for.
            assert app.focused is options and options.outer_size == options.size
            hint = app.screen.query_one(".hint")
            assert hint.size.height == 2  # the hint wraps at this width
            assert hint.region.bottom < app.screen.region.bottom
            assert "esc cancel" in app.export_screenshot().replace("&#160;", " ")

    asyncio.run(scenario())


def test_cleanup_list_layout_survives_resizing() -> None:
    """A scrollbar from a short terminal narrows the list and wraps more of it;
    that must not keep the scrollbar once the terminal grows back."""
    rows = [
        Workspace(f"feature/bookmark-number-{i}-with-a-long-name", None, "merged into main")
        for i in range(8)
    ]

    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test(size=(60, 30)) as pilot:
            app.push_screen(CleanupScreen(rows))
            await pilot.pause()
            options = app.screen.query_one(OptionList)

            def layout() -> tuple[object, ...]:
                return (options.size, options.show_vertical_scrollbar, options.max_scroll_y)

            fresh = layout()
            await pilot.resize_terminal(60, 12)
            await pilot.pause()
            await pilot.resize_terminal(60, 30)
            await pilot.pause()
            await pilot.pause()
            assert layout() == fresh

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
            assert "q/esc/? close" in shot

    asyncio.run(scenario())


def test_help_resize_after_children_are_removed() -> None:
    from textual import events

    from project_sandbox_tui.screens.dialogs import HelpScreen

    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test(size=(80, 24)) as pilot:
            screen = HelpScreen()
            app.push_screen(screen)
            await pilot.pause()
            await screen.remove_children()
            # A queued resize can arrive while the screen is being torn down.
            screen.on_resize(events.Resize(screen.size, screen.size))
            await pilot.pause()

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
            await pilot.press("x")  # other keys neither close nor fall through
            assert isinstance(app.screen, HelpScreen)
            await pilot.press("G")
            assert body.scroll_y == body.max_scroll_y
            await pilot.press("g", "j", "g")  # another key in between cancels gg
            assert body.scroll_y == body.max_scroll_y
            await pilot.press("g", "g")
            assert body.scroll_y == 0
            await pilot.press("question_mark")
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


def test_message_scrolls_with_vim_keys_and_closes_on_q() -> None:
    from project_sandbox_tui.screens.dialogs import MessageScreen

    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test(size=(80, 24)) as pilot:
            screen = MessageScreen("details", "\n".join(f"line-{i}" for i in range(100)))
            app.push_screen(screen)
            await pilot.pause()
            scroll = screen.query_one(VerticalScroll)
            await pilot.press("j", "j")
            assert app.screen is screen and scroll.scroll_y == 2
            await pilot.press("ctrl+d")
            assert scroll.scroll_y == 2 + scroll.size.height // 2
            await pilot.press("ctrl+u", "k", "x", "P")  # other keys do not close
            assert app.screen is screen and scroll.scroll_y == 1
            await pilot.press("q")
            assert app.screen is not screen
            for key in ("escape", "enter"):
                screen = MessageScreen("details", "short")
                app.push_screen(screen)
                await pilot.pause()
                await pilot.press(key)
                assert app.screen is not screen

    asyncio.run(scenario())


@pytest.mark.parametrize("key", ["j", "k", "g", "G", "ctrl+d", "pagedown"])
def test_confirmation_scroll_keys_neither_confirm_nor_cancel(key: str) -> None:
    from project_sandbox_tui.screens.dialogs import ConfirmScreen

    results: list[bool | None] = []

    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test(size=(80, 24)) as pilot:
            screen = ConfirmScreen("delete?", "\n".join(f"file-{i}" for i in range(100)), "x")
            app.push_screen(screen, results.append)
            await pilot.pause()
            await pilot.press(key)
            assert app.screen is screen and results == []
            await pilot.press("y")
            assert results == [True]

    asyncio.run(scenario())


def test_cleanup_enter_asks_before_removing() -> None:
    from project_sandbox_tui.screens.dialogs import ConfirmScreen

    rows = [
        Workspace("done", Path("/tmp/done"), "clean", removable=True, branch="done"),
        Workspace("old", None, "merged into main", removable=True, branch="old"),
    ]
    results: list[object] = []

    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test(size=(100, 30)) as pilot:
            screen = CleanupScreen(rows)
            app.push_screen(screen, results.append)
            await pilot.pause()
            await pilot.press("enter")
            confirm = app.screen
            assert isinstance(confirm, ConfirmScreen)
            assert confirm.title_text == "remove 1 workspace and 2 bookmarks?"
            assert "remove done  /tmp/done" in confirm.body
            assert "delete bookmark old" in confirm.body
            await pilot.press("escape")  # back to the list, nothing removed
            assert app.screen is screen and results == []
            await pilot.press("enter", "y")
            assert results == [rows]

    asyncio.run(scenario())
