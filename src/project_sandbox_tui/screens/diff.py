"""Scrollable comparison of two branch tips."""

from __future__ import annotations

import asyncio

from rich.syntax import Syntax
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import RichLog, Static

from project_sandbox_tui.vcs import Backend, VcsError
from project_sandbox_tui.widgets import KeyHints


class DiffScreen(Screen[None]):
    DEFAULT_CSS = """
    DiffScreen #diff-title { height: 1; background: $panel; padding: 0 1; }
    DiffScreen RichLog { height: 1fr; border: none; }
    DiffScreen #diff-hint { height: auto; color: $text-disabled; padding: 0 1; }
    """

    BINDINGS = [
        Binding("q,escape", "app.pop_screen", show=False),
        Binding("j", "scroll(1)", show=False),
        Binding("k", "scroll(-1)", show=False),
        Binding("ctrl+d", "page(1)", show=False),
        Binding("ctrl+u", "page(-1)", show=False),
        Binding("G", "bottom", show=False),
        Binding("g", "g_prefix", show=False),
    ]

    def __init__(self, backend: Backend, base: str, target: str) -> None:
        super().__init__()
        self.backend = backend
        self.base = base
        self.target = target
        self._g_pending = False

    def compose(self) -> ComposeResult:
        yield Static(Text(f"Diff: {self.base} → {self.target}"), id="diff-title")
        yield RichLog(auto_scroll=False, wrap=False)
        yield Static(
            KeyHints("q/esc back · j/k scroll · ctrl+d/u half page · gg/G top/bottom"),
            id="diff-hint",
        )

    def on_mount(self) -> None:
        self.query_one(RichLog).write("Loading diff…")
        self.load_diff()

    @work(exclusive=True)
    async def load_diff(self) -> None:
        try:
            diff = await asyncio.to_thread(self.backend.diff, self.base, self.target)
        except VcsError as exc:
            content = Text(f"Unable to load diff: {exc}", style="red")
        else:
            content = Syntax(diff, "diff", word_wrap=False) if diff else Text("No differences.")
        log = self.query_one(RichLog)
        log.clear()
        log.write(content)
        log.scroll_home(animate=False)

    def action_scroll(self, lines: int) -> None:
        self.query_one(RichLog).scroll_relative(y=lines, animate=False)

    def action_page(self, direction: int) -> None:
        self.action_scroll(direction * max(1, self.query_one(RichLog).size.height // 2))

    def action_bottom(self) -> None:
        self.query_one(RichLog).scroll_end(animate=False)

    def action_g_prefix(self) -> None:
        if self._g_pending:
            self.query_one(RichLog).scroll_home(animate=False)
            self._g_pending = False
        else:
            self._g_pending = True
            self.set_timer(0.8, self._reset_g)

    def _reset_g(self) -> None:
        self._g_pending = False
