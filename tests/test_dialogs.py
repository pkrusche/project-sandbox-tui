"""Rendering regressions for modal dialogs."""

from __future__ import annotations

import asyncio

import pytest
from textual.app import App
from textual.widgets import Input

from project_sandbox_tui.screens.dialogs import NewBranchScreen


@pytest.mark.parametrize("kind, start", [("jj", "@-"), ("git", "main")])
def test_new_branch_inputs_remain_visible_when_focused(kind: str, start: str) -> None:
    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(NewBranchScreen(start, kind))
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
