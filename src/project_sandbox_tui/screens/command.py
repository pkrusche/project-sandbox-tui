"""The ``:`` command line at the bottom of the bookmark list, as in vim."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import Input, Static

from project_sandbox_tui import commands
from project_sandbox_tui.history import PromptHistory


class CommandScreen(ModalScreen[str | None]):
    """Read one command line; enter returns it, esc (or backspace when empty) cancels.

    - ``tab`` / ``shift+tab`` complete like vim's wildmenu: the first press
      inserts the first match and lists them all, further presses cycle, and
      cycling past the last match brings back what was typed.
    - ``↑`` / ``↓`` (``ctrl+p`` / ``ctrl+n``) walk the history of lines that
      start with what was typed; ``→`` takes the newest one as ghost text.
    """

    DEFAULT_CSS = """
    CommandScreen { background: $surface; }
    CommandScreen #command-help {
        height: 1fr; padding: 1 2; scrollbar-size-vertical: 1;
    }
    CommandScreen #matches {
        height: auto; max-height: 6; padding: 0 1; display: none; background: $panel;
    }
    CommandScreen #matches.visible { display: block; }
    CommandScreen Horizontal { height: 1; dock: bottom; background: $panel; }
    CommandScreen #colon { width: 2; padding: 0 0 0 1; }
    CommandScreen Input, CommandScreen Input:focus {
        border: none; height: 1; width: 1fr; padding: 0; background: $panel;
    }
    """
    BINDINGS = [
        Binding("escape", "cancel", show=False),
        Binding("backspace", "backspace", show=False, priority=True),
        Binding("tab", "complete(1)", show=False, priority=True),
        Binding("shift+tab", "complete(-1)", show=False, priority=True),
        Binding("up,ctrl+p", "history(1)", show=False),
        Binding("down,ctrl+n", "history(-1)", show=False),
        Binding("pageup", "scroll_help(-1)", show=False),
        Binding("pagedown", "scroll_help(1)", show=False),
    ]

    def __init__(
        self,
        history: PromptHistory,
        cwd: Path,
        *,
        command_names: Iterable[str],
        action_names: Iterable[str],
    ) -> None:
        super().__init__()
        self.command_names = sorted(command_names)
        self.action_names = sorted(action_names)
        self.names = [*commands.BUILTINS, *self.command_names, *self.action_names]
        self.history = history
        self.cwd = cwd
        self._expected: str | None = None  # the value last set here, not typed
        self._cycle: list[str] = []  # completed lines, then the line as typed
        self._words: list[str] = []  # the completions shown in the list
        self._cycle_index = 0
        self._browse: list[str] | None = None  # history lines while walking them
        self._browse_index = -1
        self._browse_origin = ""

    def compose(self) -> ComposeResult:
        with Vertical():
            with VerticalScroll(id="command-help", can_focus=False):
                yield Static(self._help_text(), id="command-help-text")
            yield Static("", id="matches")
            with Horizontal():
                yield Static(":", id="colon")
                yield Input(
                    id="command",
                    suggester=SuggestFromList(self.history.newest_first(), case_sensitive=True),
                )

    def _help_text(self) -> Text:
        help_text = Text.assemble(
            ("Command line\n", "bold"),
            "Enter runs · Esc cancels · Backspace cancels on an empty line\n",
            "Tab / Shift+Tab completes commands, programs, and paths, then cycles\n",
            "↑ / ↓ or Ctrl+P / Ctrl+N browses matching history\n",
            "→ at the end accepts the history suggestion\n",
            "PageUp / PageDown scrolls this help\n",
            ("\nCommand syntax\n", "bold"),
        )
        for syntax, description in (
            (":!CMD", "Run a foreground shell command in the repository"),
            (":NAME [ARGS]", "Run a configured command or start an agent action"),
            (":&!CMD / :&NAME", "Run once in the repository background"),
            (":*!CMD / :*NAME", "Run in the background in each target workspace"),
        ):
            help_text.append(syntax, style="bold").append(f"  {description}\n")
        help_text.append("Targets are the marked bookmarks or the highlighted bookmark.\n")
        if self.command_names or self.action_names:
            help_text.append("\n")
        for title, names in (
            ("Configured commands", self.command_names),
            ("Agent actions (background on each target)", self.action_names),
        ):
            if names:
                help_text.append(f"{title}  ", style="bold")
                help_text.append("  ".join(f":{name}" for name in names) + "\n")
        help_text.append("\nBuilt-in commands\n", style="bold")
        for name, description in commands.BUILTINS.items():
            help_text.append(f":{name}", style="bold").append(f"  {description}\n")
        aliases = "  ".join(f":{alias} → :{name}" for alias, name in commands.ALIASES.items())
        help_text.append(f"Aliases  {aliases}")
        return help_text

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @property
    def input(self) -> Input:
        return self.query_one(Input)

    def _set(self, value: str) -> None:
        self._expected = value
        self.input.value = value
        self.input.cursor_position = len(value)

    @on(Input.Changed)
    def _typed(self, event: Input.Changed) -> None:
        if event.value == self._expected:
            return
        self._expected = None  # typing ends completion and a history walk
        self._cycle, self._words, self._browse = [], [], None
        self._show_matches()

    @on(Input.Submitted)
    def _submit(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_scroll_help(self, direction: int) -> None:
        help_panel = self.query_one("#command-help", VerticalScroll)
        help_panel.scroll_relative(
            y=direction * help_panel.scrollable_content_region.height, animate=False
        )

    def action_backspace(self) -> None:
        if self.input.value:
            self.input.action_delete_left()
        else:
            self.dismiss(None)  # like vim: backspace over the colon

    # ------------------------------------------------------------ completion

    def action_complete(self, step: int) -> None:
        self._browse = None
        if not self._cycle:
            value = self.input.value
            start, words = commands.complete(value, self.names, self.cwd)
            if not words:
                self.app.bell()
                return
            if len(words) == 1:
                self._set(value[:start] + words[0])
                return
            self._words = words
            self._cycle = [value[:start] + word for word in words] + [value]
            self._cycle_index = len(self._cycle) - 1
        self._cycle_index = (self._cycle_index + step) % len(self._cycle)
        self._set(self._cycle[self._cycle_index])
        self._show_matches()

    def _show_matches(self) -> None:
        matches = self.query_one("#matches", Static)
        text = Text()
        for i, word in enumerate(self._words):
            if i:
                text.append("  ")
            text.append(word, style="reverse" if i == self._cycle_index else "")
        matches.update(text)
        matches.set_class(bool(self._words), "visible")

    # ------------------------------------------------------------ history

    def action_history(self, step: int) -> None:
        if self._browse is None:
            if step < 0:
                return
            origin = self._browse_origin = self.input.value
            self._browse = [e for e in self.history.with_prefix(origin) if e != origin]
            self._browse_index = -1
            self._cycle, self._words = [], []
            self._show_matches()
        index = self._browse_index + step
        if not self._browse:
            self._browse = None
            self.app.bell()
            return
        if index >= len(self._browse):
            self.app.bell()  # no (older) match
            return
        if index < 0:  # past the newest: back to what was typed
            self._browse = None
            self._set(self._browse_origin)
            return
        self._browse_index = index
        self._set(self._browse[index])
