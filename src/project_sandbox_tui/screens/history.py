"""ctrl+r: search the prompt history (fzf-style: type to filter, enter to pick)."""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from project_sandbox_tui.history import PromptHistory
from project_sandbox_tui.widgets import KeyHints

PREVIEW_CHARS = 110


def _preview(prompt: str) -> Text:
    lines = prompt.splitlines() or [""]
    first = lines[0]
    if len(first) > PREVIEW_CHARS:
        first = first[: PREVIEW_CHARS - 1] + "…"
    text = Text(first)
    if len(lines) > 1:
        text.append(f"  (+{len(lines) - 1} lines)", style="dim")
    return text


class HistorySearchScreen(ModalScreen[str | None]):
    DEFAULT_CSS = """
    HistorySearchScreen { align: center middle; }
    HistorySearchScreen > Vertical {
        width: 100; max-width: 100%; height: auto; max-height: 80%;
        background: $panel; padding: 1 2;
    }
    HistorySearchScreen .title { text-style: bold; margin-bottom: 1; }
    HistorySearchScreen Input, HistorySearchScreen Input:focus {
        border: none; height: 1; padding: 0 1; background: $boost;
    }
    HistorySearchScreen OptionList { border: none; height: auto; max-height: 16; margin-top: 1; }
    HistorySearchScreen .hint { color: $text-disabled; margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", show=False),
        Binding("down,ctrl+n,ctrl+j", "move(1)", show=False, priority=True),
        Binding("up,ctrl+p,ctrl+k", "move(-1)", show=False, priority=True),
    ]

    def __init__(self, history: PromptHistory, query: str = "") -> None:
        super().__init__()
        self.history = history
        self.initial_query = query
        self.matches: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("prompt history", classes="title")
            yield Input(
                value=self.initial_query, placeholder="search", id="query", select_on_focus=False
            )
            yield OptionList(id="matches")
            yield Static("", id="count", classes="hint")
            yield Static(
                KeyHints("type to filter · ↑/↓ choose · enter use · esc cancel"), classes="hint"
            )

    def on_mount(self) -> None:
        self.query_one("#query", Input).focus()
        self._refresh(self.initial_query)

    def _refresh(self, query: str) -> None:
        self.matches = self.history.search(query)
        options = self.query_one("#matches", OptionList)
        options.clear_options()
        options.add_options([Option(_preview(p)) for p in self.matches])
        if self.matches:
            options.highlighted = 0
        total = len(self.history.entries)
        self.query_one("#count", Static).update(
            f"{len(self.matches)} of {total} prompts" if total else "no prompts yet"
        )

    @on(Input.Changed, "#query")
    def _query_changed(self, event: Input.Changed) -> None:
        self._refresh(event.value)

    def action_move(self, step: int) -> None:
        options = self.query_one("#matches", OptionList)
        if not self.matches:
            return
        current = options.highlighted if options.highlighted is not None else -1
        options.highlighted = min(max(current + step, 0), len(self.matches) - 1)

    @on(Input.Submitted, "#query")
    def _submitted(self) -> None:
        highlighted = self.query_one("#matches", OptionList).highlighted
        if highlighted is not None and 0 <= highlighted < len(self.matches):
            self.dismiss(self.matches[highlighted])

    @on(OptionList.OptionSelected, "#matches")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.matches[event.option_index])

    def action_cancel(self) -> None:
        self.dismiss(None)
