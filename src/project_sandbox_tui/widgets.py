"""Small custom widgets."""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual.binding import Binding
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget


class CycleField(Widget, can_focus=True):
    """A one-line choice field: ``label  ‹ value ›`` cycled with h/l.

    j/k move focus to the next/previous field so forms stay keyboard-first.
    """

    DEFAULT_CSS = """
    CycleField { height: 1; width: 1fr; padding: 0 1; }
    CycleField:focus { background: $boost; }
    """

    BINDINGS = [
        Binding("h,left", "cycle(-1)", "prev", show=False),
        Binding("l,right,space", "cycle(1)", "next", show=False),
        Binding("j,down", "app.focus_next", "next field", show=False),
        Binding("k,up", "app.focus_previous", "prev field", show=False),
    ]

    index: reactive[int] = reactive(0)

    class Changed(Message):
        def __init__(self, field: CycleField, value: str) -> None:
            super().__init__()
            self.field = field
            self.value = value

        @property
        def control(self) -> CycleField:
            return self.field

    def __init__(
        self,
        label: str,
        options: Sequence[str],
        value: str = "",
        *,
        empty_label: str = "default",
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.label = label
        self.options = list(options)
        self.empty_label = empty_label
        initial = self.options.index(value) if value in self.options else 0
        self.set_reactive(CycleField.index, initial)  # type: ignore[arg-type]

    @property
    def value(self) -> str:
        return self.options[self.index] if self.options else ""

    @value.setter
    def value(self, new: str) -> None:
        if new in self.options:
            self.index = self.options.index(new)

    def action_cycle(self, step: int) -> None:
        if self.options:
            self.index = (self.index + step) % len(self.options)

    def watch_index(self, _old: int, _new: int) -> None:
        self.post_message(self.Changed(self, self.value))

    def render(self) -> Text:
        shown = self.value or self.empty_label
        text = Text()
        text.append(f"{self.label:<9}", style="dim")
        text.append("‹ ", style="dim")
        text.append(shown, style="bold" if self.value else "italic")
        text.append(" ›", style="dim")
        return text
