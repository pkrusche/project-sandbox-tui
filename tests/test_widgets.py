import asyncio
from pathlib import Path

from textual.app import App
from textual.widgets import Input, OptionList

from project_sandbox_tui.history import PromptHistory
from project_sandbox_tui.screens.history import HistorySearchScreen
from project_sandbox_tui.widgets import Fitted, KeyHints, PromptArea


def test_fitted_draws_the_longest_variant_that_fits() -> None:
    from rich.console import Console
    from rich.text import Text

    fitted = Fitted(Text("a long variant"), KeyHints("short · s"), Text("tiny"))
    assert str(fitted) == "a long variant"
    for width, expected in ((20, "a long variant"), (10, "short · s"), (5, "tiny"), (3, "tin")):
        console = Console(width=width, color_system=None, record=True)
        console.print(fitted, no_wrap=True, crop=True, end="")
        assert console.export_text().rstrip("\n") == expected


def test_prompt_history_boundaries_and_editing_end_browse(tmp_path: Path) -> None:
    history = PromptHistory(tmp_path / "history.json")
    history.add("fix old")
    history.add("fix new")

    class PromptApp(App[None]):
        ENABLE_COMMAND_PALETTE = False  # like SandboxTUI: ctrl+p belongs to prompt history

    async def scenario() -> None:
        app = PromptApp()
        async with app.run_test() as pilot:
            area = PromptArea(prompt_history=history)
            await app.mount(area)
            area.focus()
            area.replace_text("fix")
            await pilot.pause()
            await pilot.press("ctrl+n")  # newer before starting a walk keeps the draft
            assert area.text == "fix" and area.browse_position is None
            await pilot.press("ctrl+p", "ctrl+p", "ctrl+p")
            assert app.focused is area
            assert area.text == "fix old" and area.browse_position == (2, 2)
            await pilot.press("!")
            assert area.text == "fix old!" and area.browse_position is None
            await pilot.press("ctrl+p")  # no matches must leave the edited draft alone
            assert area.text == "fix old!" and area.browse_position is None

    asyncio.run(scenario())


def test_history_search_empty_results_and_cancel(tmp_path: Path) -> None:
    history = PromptHistory(tmp_path / "history.json")
    history.add("first\nfull text")
    history.add("second")
    results: list[str | None] = []

    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test() as pilot:
            screen = HistorySearchScreen(history, "missing")
            app.push_screen(screen, results.append)
            await pilot.pause()
            await pilot.press("down", "up", "enter")
            assert app.screen is screen and results == []
            screen.query_one(Input).value = ""
            await pilot.pause()
            await pilot.press("down", "down")  # stop at the oldest entry
            assert screen.query_one(OptionList).highlighted == 1
            await pilot.press("enter")
            assert results == ["first\nfull text"]
            app.push_screen(HistorySearchScreen(history), results.append)
            await pilot.pause()
            await pilot.press("escape")
            assert results == ["first\nfull text", None]

    asyncio.run(scenario())
