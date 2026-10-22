"""Small custom widgets."""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual.binding import Binding
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import TextArea

from project_sandbox_tui.history import PromptHistory


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
        self.set_reactive(CycleField.index, initial)

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


class PromptArea(TextArea):
    """Prompt editor with shell-style history.

    - fish-style autosuggestion: the rest of the newest history entry that
      extends what you typed is shown as ghost text; ``→`` (Textual's own
      TextArea suggestion) or ``ctrl+f`` takes it.
    - ``ctrl+p`` / ``ctrl+n`` walk back / forward through entries that start
      with the text typed before browsing began (zsh history-beginning-search).
    - ``ctrl+r`` asks the screen to open the history search.
    - ``ctrl+l`` clears the prompt (e.g. one pre-filled from the last run);
      ``ctrl+z`` brings it back.
    """

    BINDINGS = [
        Binding("ctrl+p", "history(1)", "older prompt", show=False),
        Binding("ctrl+n", "history(-1)", "newer prompt", show=False),
        Binding("ctrl+f", "accept_suggestion", "accept suggestion", show=False),
        Binding("ctrl+r", "search_history", "search history", show=False),
        Binding("ctrl+l", "clear_prompt", "clear prompt", show=False),
    ]

    class SearchRequested(Message):
        def __init__(self, area: PromptArea) -> None:
            super().__init__()
            self.area = area

    def __init__(
        self,
        text: str = "",
        *,
        prompt_history: PromptHistory | None = None,
        id: str | None = None,
    ) -> None:
        # Set before TextArea.__init__, which may already call update_suggestion().
        # (Not "history": TextArea uses that name for its undo stack.)
        self.prompt_history = prompt_history
        self._browse: list[str] | None = None  # matches while walking history
        self._browse_index = -1
        self._browse_origin = ""  # text before browsing, restored past the newest
        self._programmatic = False
        super().__init__(text, id=id, soft_wrap=True)

    # ------------------------------------------------------------ state

    def update_suggestion(self) -> None:
        """Offer the remainder of the newest history entry extending the text."""
        history = getattr(self, "prompt_history", None)
        entry = None
        if history is not None and getattr(self, "_browse", None) is None:
            entry = history.suggest(self.text)
        self.suggestion = entry[len(self.text) :] if entry else ""

    @property
    def browse_position(self) -> tuple[int, int] | None:
        """(1-based position, count) while walking history, else None."""
        if self._browse is None or self._browse_index < 0:
            return None
        return self._browse_index + 1, len(self._browse)

    def replace_text(self, text: str) -> None:
        """Set the prompt (e.g. from history) and put the cursor at the end."""
        self._programmatic = True
        self.text = text
        self.move_cursor(self.document.end)

    def _on_text_area_changed(self, _event: TextArea.Changed) -> None:
        if self._programmatic:
            self._programmatic = False
        elif self._browse is not None:
            self._browse = None  # typing ends a history walk
            self._browse_index = -1
            self.update_suggestion()

    # ------------------------------------------------------------ actions

    def action_history(self, step: int) -> None:
        if self.prompt_history is None:
            return
        if self._browse is None:
            self._browse_origin = self.text
            self._browse = [e for e in self.prompt_history.with_prefix(self.text) if e != self.text]
            self._browse_index = -1
            self.suggestion = ""
        if not self._browse:
            self._browse = None
            self.update_suggestion()
            self.app.bell()
            return
        index = self._browse_index + step
        if index >= len(self._browse):
            self.app.bell()  # already at the oldest match
            return
        if index < 0:
            # walked past the newest match: back to what was typed, walk over
            self._browse = None
            self.replace_text(self._browse_origin)
            return
        self._browse_index = index
        self.replace_text(self._browse[index])

    def action_accept_suggestion(self) -> None:
        if self.suggestion:
            self.insert(self.suggestion)

    def action_clear_prompt(self) -> None:
        self._browse = None  # also ends a history walk
        self._browse_index = -1
        self.clear()  # an edit, so ctrl+z undoes it

    def action_search_history(self) -> None:
        self.post_message(self.SearchRequested(self))


def modifier_key(key: str) -> str | None:
    """Normalize standalone modifier events reported by extended-key terminals."""
    key = key.removeprefix("left_").removeprefix("right_")
    key = {"control": "ctrl"}.get(key, key)
    return key if key in {"ctrl", "shift", "alt", "super", "meta", "hyper"} else None
