"""Small modal dialogs: new branch, help, message/preview."""

from __future__ import annotations

from dataclasses import dataclass

from rich.cells import cell_len
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.table import Table
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static

from project_sandbox_tui.cleanup import Workspace
from project_sandbox_tui.vcs import validate_branch_name
from project_sandbox_tui.widgets import KeyHints, modifier_key

_MODAL_CSS = """
{name} {{ align: center middle; }}
{name} > Vertical {{
    width: 80; max-width: 100%; height: auto; max-height: 100%;
    background: $panel; padding: 1 2;
}}
{name} .title {{ text-style: bold; margin-bottom: 1; }}
{name} .row-label {{ color: $text-muted; height: 1; padding: 0 1; }}
{name} .hint {{ color: $text-disabled; margin-top: 1; }}
{name} Input, {name} Input:focus {{ border: none; height: 1; padding: 0 1; }}
{name} Input:focus {{ background: $boost; }}
"""


class NewBranchScreen(ModalScreen[tuple[str, str] | None]):
    """Ask for a bookmark name and start revision."""

    DEFAULT_CSS = _MODAL_CSS.format(name="NewBranchScreen")
    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, default_start: str) -> None:
        super().__init__()
        self.default_start = default_start

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("new bookmark", classes="title")
            yield Static("name", classes="row-label")
            yield Input(placeholder="feature/my-change", id="name")
            yield Static("start at", classes="row-label")
            yield Input(value=self.default_start, id="start")
            yield Static("", id="error")
            yield Static(KeyHints("tab fields · enter create · esc cancel"), classes="hint")

    def on_mount(self) -> None:
        self.query_one("#name").focus()

    @on(Input.Submitted)
    def _submit(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        start = self.query_one("#start", Input).value.strip() or self.default_start
        error = validate_branch_name(name)
        if error:
            self.query_one("#error", Static).update(Text(error, style="red"))
            self.query_one("#name").focus()
            return
        self.dismiss((name, start))

    def action_cancel(self) -> None:
        self.dismiss(None)


class MessageScreen(ModalScreen[None]):
    """Show scrollable plain text; navigation scrolls, other keys close."""

    DEFAULT_CSS = (
        _MODAL_CSS.format(name="MessageScreen")
        + """
    MessageScreen .message-body { height: auto; max-height: 60vh; }
    """
    )

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self.title_text = title
        self.body = body

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.title_text, classes="title")
            with VerticalScroll(classes="message-body"):
                yield Static(Text(self.body))
            yield Static("↑/↓, pgup/pgdn scroll · any other key to close", classes="hint")

    def on_key(self, event: events.Key) -> None:
        # Consume the key so it cannot also trigger a binding on the screen below.
        event.stop()
        event.prevent_default()
        scroll = self.query_one(VerticalScroll)
        actions = {
            "up": scroll.scroll_up,
            "down": scroll.scroll_down,
            "pageup": scroll.scroll_page_up,
            "pagedown": scroll.scroll_page_down,
            "home": scroll.scroll_home,
            "end": scroll.scroll_end,
        }
        if event.key in actions:
            actions[event.key]()
            return
        if not modifier_key(event.key):
            self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Show scrollable plain text; y confirms, navigation scrolls, other keys cancel."""

    DEFAULT_CSS = (
        _MODAL_CSS.format(name="ConfirmScreen")
        + """
    ConfirmScreen VerticalScroll { height: auto; max-height: 60vh; }
    """
    )

    def __init__(self, title: str, body: str, action: str) -> None:
        super().__init__()
        self.title_text = title
        self.body = body
        self.action_text = action

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.title_text, classes="title")
            with VerticalScroll():
                yield Static(Text(self.body))
            yield Static(
                KeyHints(f"y {self.action_text} · arrows/pgup/pgdn scroll · any other key cancels"),
                classes="hint",
            )

    def on_key(self, event: events.Key) -> None:
        # Consume the key so it cannot also trigger a binding on the screen below.
        event.stop()
        event.prevent_default()
        scroll = self.query_one(VerticalScroll)
        actions = {
            "up": scroll.scroll_up,
            "down": scroll.scroll_down,
            "pageup": scroll.scroll_page_up,
            "pagedown": scroll.scroll_page_down,
            "home": scroll.scroll_home,
            "end": scroll.scroll_end,
        }
        if event.key in actions:
            actions[event.key]()
            return
        if not modifier_key(event.key):
            self.dismiss(event.key == "y")


@dataclass(frozen=True)
class ForceRemove:
    """CleanupScreen result: force-delete this kept workspace (after confirmation)."""

    workspace: Workspace


class CleanupScreen(ModalScreen[list[Workspace] | ForceRemove | None]):
    """List every workspace and merged bookmark; removable ones are preselected, others say why."""

    DEFAULT_CSS = (
        _MODAL_CSS.format(name="CleanupScreen")
        + """
    CleanupScreen > Vertical { width: 110; }
    CleanupScreen OptionList { border: none; height: auto; padding: 0; }
    """
    )
    BINDINGS = [
        Binding("escape,q", "cancel", show=False),
        Binding("enter", "confirm", show=False, priority=True),
        Binding("space", "toggle_one", show=False),
        Binding("j", "move(1)", show=False),
        Binding("k", "move(-1)", show=False),
        Binding("a", "toggle_all", show=False),
        Binding("D", "force_remove", show=False),
    ]

    def __init__(self, workspaces: list[Workspace]) -> None:
        super().__init__()
        self.workspaces = workspaces
        self.selected = {i for i, ws in enumerate(workspaces) if ws.removable}

    def compose(self) -> ComposeResult:
        removable = sum(ws.removable for ws in self.workspaces)
        bookmarks = sum(ws.path is None for ws in self.workspaces)
        kept = len(self.workspaces) - removable
        summary = f"{len(self.workspaces) - bookmarks} workspaces"
        if bookmarks:
            summary += f" · {bookmarks} merged bookmarks"
        summary += f" · {removable} can be removed"
        if kept:
            summary += f" · {kept} kept (reason shown)"
        with Vertical():
            yield Static("clean up workspaces", classes="title")
            yield Static(summary)
            # Not a SelectionList: its options are one line each, which cut long reasons off.
            yield OptionList(*(self._label(i) for i in range(len(self.workspaces))))
            yield Static(
                KeyHints(
                    "space toggle · a all/none · enter remove · D force-delete kept · esc cancel"
                ),
                classes="hint",
            )

    def _label(self, index: int) -> Text:
        ws = self.workspaces[index]
        label = Text("[x] " if index in self.selected else "[ ] " if ws.removable else "    ")
        label.append(ws.name, style="bold" if ws.removable else "bold dim")
        label.append(f"  {ws.path}" if ws.path else "  bookmark", style="dim")
        # The reason gets its own line(s): long ones wrap instead of being cut off.
        label.append(f"\n    {ws.status}", style="green" if ws.removable else "yellow")
        return label

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_resize(self, event: events.Resize) -> None:
        # Long lists scroll inside the dialog; title, summary and hint stay visible.
        chrome = 7  # padding, title + margin, summary, hint + margin
        self.query_one(OptionList).styles.max_height = max(3, event.size.height - chrome)

    def action_move(self, step: int) -> None:
        options = self.query_one(OptionList)
        options.action_cursor_down() if step > 0 else options.action_cursor_up()

    @on(OptionList.OptionSelected)
    def _clicked(self, event: OptionList.OptionSelected) -> None:
        self._toggle(event.option_index)

    def action_toggle_one(self) -> None:
        highlighted = self.query_one(OptionList).highlighted
        if highlighted is not None:
            self._toggle(highlighted)

    def _toggle(self, index: int) -> None:
        if self.workspaces[index].removable:
            self.selected ^= {index}
            self.query_one(OptionList).replace_option_prompt_at_index(index, self._label(index))

    def action_toggle_all(self) -> None:
        if self.selected:
            self.selected.clear()
        else:
            self.selected = {i for i, ws in enumerate(self.workspaces) if ws.removable}
        options = self.query_one(OptionList)
        for i in range(len(self.workspaces)):
            options.replace_option_prompt_at_index(i, self._label(i))

    def action_force_remove(self) -> None:
        highlighted = self.query_one(OptionList).highlighted
        if highlighted is None:
            return
        ws = self.workspaces[highlighted]
        if ws.removable:
            self.notify("removable without force: select it and press enter")
        elif not ws.forceable or ws.path is None:
            self.notify(f"cannot force-delete {ws.name}: {ws.status}", severity="warning")
        else:
            self.dismiss(ForceRemove(ws))

    def action_confirm(self) -> None:
        self.dismiss([self.workspaces[i] for i in sorted(self.selected)])

    def action_cancel(self) -> None:
        self.dismiss(None)


HelpSection = tuple[str, tuple[tuple[str, str], ...]]

# Two columns of sections, side by side when the terminal is wide enough.
HELP: tuple[tuple[HelpSection, ...], tuple[HelpSection, ...]] = (
    (
        (
            "navigation",
            (
                ("j / k", "down / up"),
                ("gg / G", "top / bottom"),
                ("ctrl+d / u", "half page down / up"),
                ("/", "filter (esc clears)"),
                ("R", "refresh"),
                ("ctrl+r", "reload configuration"),
                ("H", "show / hide cheat-sheet"),
                ("?", "this help"),
                ("q", "quit (runs continue)"),
            ),
        ),
        (
            "prompt box (unsupervised runs)",
            (
                ("ctrl+enter", "launch (enter = newline)"),
                ("→ / ctrl+f", "take the suggestion"),
                ("ctrl+p / n", "older / newer prompt"),
                ("ctrl+r", "search prompt history"),
            ),
        ),
    ),
    (
        (
            "sessions (one sandbox per branch)",
            (
                ("i", "interactive (ui.terminal)"),
                ("r", "unsupervised run"),
                ("x", "stop selected; repeat"),
                ("l / enter", "log (one branch)"),
                ("c / C", "clear / forget selected"),
            ),
        ),
        (
            "branches",
            (
                ("n", "new bookmark"),
                (
                    "v V space",
                    "select a range / mark; r/i merge or rebase; esc clears",
                ),
                ("d", "diff main / two branches"),
                ("o", "open selected in editor"),
                ("P", "show selected commands"),
                ("W", "clean up workspaces"),
            ),
        ),
    ),
)


class HelpBody:
    """The key reference: two columns when they fit, else one; long descriptions wrap in place."""

    GAP = 4
    MIN_DESCRIPTION = 10  # half the narrowest description column before the columns stack

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        left, right = HELP
        side_by_side = (
            self._width(left) + self.GAP + self._width(right) + 4 * self.MIN_DESCRIPTION
        ) <= options.max_width
        if side_by_side:
            # Fixed equal widths: ratios would favour the column with longer words.
            width = (options.max_width - self.GAP) // 2
            grid = Table.grid(padding=(0, self.GAP))
            grid.add_column(width=width)
            grid.add_column(width=width)
            grid.add_row(self._column(left), self._column(right))
            yield grid
        else:
            yield self._column(left + right)

    @staticmethod
    def _width(sections: tuple[HelpSection, ...]) -> int:
        """Width of the key column plus its gap."""
        return max(cell_len(key) for _title, keys in sections for key, _what in keys) + 2

    @classmethod
    def _column(cls, sections: tuple[HelpSection, ...]) -> Group:
        parts: list[RenderableType] = []
        for title, keys in sections:
            if parts:
                parts.append(Text())
            parts.append(Text(title, style="bold"))
            table = Table.grid(padding=(0, 2))
            table.add_column(width=cls._width(sections) - 2, no_wrap=True, style="bold")
            table.add_column(style="dim")
            for key, what in keys:
                table.add_row(key, what)
            parts.append(table)
        return Group(*parts)


class HelpScreen(MessageScreen):
    """The full key reference; scrolls when the terminal is too short for it."""

    DEFAULT_CSS = """
    HelpScreen > Vertical { width: 84; }
    HelpScreen VerticalScroll { height: auto; scrollbar-size-vertical: 1; }
    """

    SCROLL_KEYS = {"j": 1, "down": 1, "k": -1, "up": -1}

    def __init__(self) -> None:
        super().__init__("sbx-tui keys", "")

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.title_text, classes="title")
            with VerticalScroll():
                yield Static(HelpBody())
            yield Static("any key to close", classes="hint")

    def on_resize(self, event: events.Resize) -> None:
        chrome = 6  # padding, title + margin, hint + margin
        self.query_one(VerticalScroll).styles.max_height = max(3, event.size.height - chrome)

    def on_key(self, event: events.Key) -> None:
        body = self.query_one(VerticalScroll)
        step = self.SCROLL_KEYS.get(event.key)
        if step and body.max_scroll_y:
            event.stop()
            event.prevent_default()
            body.scroll_relative(y=step, animate=False)
            return
        super().on_key(event)
