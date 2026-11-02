"""Small modal dialogs: new branch, help, message/preview, confirmation, cleanup."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import ClassVar

from rich.cells import cell_len
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.table import Table
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, OptionList, Static, TextArea

from project_sandbox_tui.cleanup import Workspace
from project_sandbox_tui.vcs import validate_branch_name
from project_sandbox_tui.widgets import KeyHints, PrefixKeys, modifier_key

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


def fit_body(body: Widget) -> None:
    """Cap a dialog's scrolling ``body`` so the rows around it stay on screen.

    Call after layout: titles and hints wrap onto more lines in narrow terminals.
    """
    box = body.parent
    assert isinstance(box, Widget)
    others = sum(
        child.outer_size.height + child.styles.margin.height
        for child in box.children
        if child is not body and child.display
    )
    body.styles.max_height = max(3, body.screen.size.height - box.styles.gutter.height - others)


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
            yield Static("bookmark name", classes="row-label")
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


class DescriptionScreen(ModalScreen[bool]):
    """Edit a full commit description; keep the draft visible if saving fails."""

    DEFAULT_CSS = (
        _MODAL_CSS.format(name="DescriptionScreen")
        + """
    DescriptionScreen TextArea { height: 12; max-height: 60vh; }
    DescriptionScreen TextArea:focus { background: $boost; }
    """
    )
    BINDINGS = [
        Binding("escape", "cancel", show=False),
        Binding("ctrl+s", "save", show=False, priority=True),
    ]

    def __init__(
        self, name: str, description: str, save: Callable[[str], Awaitable[str | None]]
    ) -> None:
        super().__init__()
        self.name_text = name
        self.description = description
        self.save = save
        self._saving = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text(f"edit description · {self.name_text}"), classes="title")
            # Compact: no border, which TextArea:focus would add over "border: none".
            yield TextArea(self.description, compact=True, id="description")
            yield Static("", id="error")
            yield Static(KeyHints("ctrl+s save · enter newline · esc cancel"), classes="hint")

    def on_mount(self) -> None:
        self.query_one(TextArea).focus()

    async def action_save(self) -> None:
        if self._saving:
            return
        self._saving = True
        try:
            error = await self.save(self.query_one(TextArea).text)
            if error:
                self.query_one("#error", Static).update(Text(error, style="red"))
            else:
                self.dismiss(True)
        finally:
            self._saving = False

    def action_cancel(self) -> None:
        if not self._saving:
            self.dismiss(False)


class ScrollModal[ResultT](ModalScreen[ResultT]):
    """A pop-up with a scrolling body and less/vim keys; subclasses handle other keys.

    j/k and arrows scroll a line, ctrl+d/u half a page, ctrl+f/b and pgdn/pgup
    a page, gg/G and home/end to either end. Every key is consumed so it cannot
    also trigger a binding on the screen below.
    """

    def __init__(self) -> None:
        super().__init__()
        self._prefix = PrefixKeys()

    def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        if not modifier_key(event.key) and not self._scroll(event.key):
            self.other_key(event.key)

    def _scroll(self, key: str) -> bool:
        """Scroll for a navigation key; return whether ``key`` was one."""
        self._prefix.other_key(key)
        scroll = self.query_one(VerticalScroll)
        half = max(1, scroll.size.height // 2)
        steps = {"j": 1, "down": 1, "k": -1, "up": -1, "ctrl+d": half, "ctrl+u": -half}
        pages = {"ctrl+f": 1, "pagedown": 1, "ctrl+b": -1, "pageup": -1}
        if key in steps:
            scroll.scroll_relative(y=steps[key], animate=False)
        elif key in pages:
            scroll.scroll_relative(y=pages[key] * max(1, scroll.size.height), animate=False)
        elif key in ("G", "end"):
            scroll.scroll_end(animate=False)
        elif key == "home" or (key == "g" and self._prefix.press(key)):
            scroll.scroll_home(animate=False)
        elif key != "g":
            return False
        return True

    def other_key(self, key: str) -> None:
        """A key that does not scroll."""


SCROLL_HINT = "j/k ^d/^u gg/G scroll"


class MessageScreen(ScrollModal[None]):
    """Show scrollable plain text; q, esc or enter closes."""

    DEFAULT_CSS = (
        _MODAL_CSS.format(name="MessageScreen")
        + """
    MessageScreen .message-body { height: auto; max-height: 60vh; }
    """
    )
    CLOSE_KEYS: ClassVar[frozenset[str]] = frozenset({"q", "escape", "enter"})

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self.title_text = title
        self.body = body

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.title_text, classes="title")
            with VerticalScroll(classes="message-body"):
                yield Static(Text(self.body))
            yield Static(KeyHints(f"{SCROLL_HINT} · q/esc close"), classes="hint")

    def other_key(self, key: str) -> None:
        if key in self.CLOSE_KEYS:
            self.dismiss(None)


class ConfirmScreen(ScrollModal[bool]):
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
                KeyHints(f"y {self.action_text} · {SCROLL_HINT} · any other key cancels"),
                classes="hint",
            )

    def other_key(self, key: str) -> None:
        self.dismiss(key == "y")


class StaleWorkspaceScreen(ScrollModal[bool]):
    """The jj working copy is stale: u updates it (True), q exits (False).

    Other keys are ignored: there is nothing useful to return to until it is updated.
    """

    DEFAULT_CSS = (
        _MODAL_CSS.format(name="StaleWorkspaceScreen")
        + """
    StaleWorkspaceScreen VerticalScroll { height: auto; max-height: 60vh; }
    """
    )

    def __init__(self, repo: str) -> None:
        super().__init__()
        self.repo = repo

    def compose(self) -> ComposeResult:
        body = (
            f"{self.repo}\n\n"
            "Another workspace or operation rewrote this workspace's working-copy "
            "commit, so jj will not read or snapshot it until it is updated.\n\n"
            "Updating runs `jj workspace update-stale`: files on disk are replaced "
            "with the current working-copy commit. Edits jj had not snapshotted yet "
            "are recorded first and can be recovered via `jj op log`."
        )
        with Vertical():
            yield Static("the jj working copy is stale", classes="title")
            with VerticalScroll():
                yield Static(Text(body))
            yield Static(KeyHints(f"u update · q exit · {SCROLL_HINT}"), classes="hint")

    def other_key(self, key: str) -> None:
        if key in ("u", "q"):
            self.dismiss(key == "u")


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
    CleanupScreen OptionList, CleanupScreen OptionList:focus {
        border: none; height: auto; padding: 0;
        scrollbar-gutter: stable; scrollbar-size-vertical: 1;  /* see LaunchScreen */
    }
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
                    "space toggle · a all/none · enter remove… · D force-delete kept · esc cancel"
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
        self.call_after_refresh(fit_body, self.query_one(OptionList))

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
        """enter: list what goes and ask; workspace removal cannot be undone."""
        chosen = [self.workspaces[i] for i in sorted(self.selected)]
        if not chosen:
            self.dismiss([])
            return
        spaces = [ws for ws in chosen if ws.path is not None]
        bookmarks = [ws.branch for ws in chosen if ws.branch]
        what = []
        if spaces:
            what.append(f"{len(spaces)} workspace{'' if len(spaces) == 1 else 's'}")
        if bookmarks:
            what.append(f"{len(bookmarks)} bookmark{'' if len(bookmarks) == 1 else 's'}")
        lines = [f"remove {ws.name}  {ws.path}" for ws in spaces]
        lines += [f"delete bookmark {name}" for name in bookmarks]
        lines.append("")
        if spaces:
            lines.append(
                "Workspace directories are deleted with their ignored files; u cannot undo it."
            )
        if bookmarks:
            lines.append("u restores the deleted bookmarks.")

        def decided(confirmed: bool | None) -> None:
            if confirmed:
                self.dismiss(chosen)

        title = f"remove {' and '.join(what)}?"
        self.app.push_screen(ConfirmScreen(title, "\n".join(lines), "remove"), decided)

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
                (":", "command line, :! shell"),
                (":&!cmd", "background command"),
                ("\\ keys", "[leader] key mappings"),
                ("R / ctrl+l", "refresh / reload config"),
                ("H / ?", "cheat-sheet / this help"),
                ("q", "quit (runs continue)"),
            ),
        ),
        (
            "prompt box (unsupervised runs)",
            (
                ("ctrl+s", "launch (enter = newline)"),
                ("→ / ctrl+f", "take the suggestion"),
                ("ctrl+p / n", "older / newer prompt"),
                ("ctrl+r", "search prompt history"),
            ),
        ),
    ),
    (
        (
            "sessions (one sandbox per bookmark)",
            (
                ("i / I", "interactive / host agent"),
                (":host", "host agent with CLI args"),
                ("r", "unsupervised run"),
                ("x", "cancel · 3×: forget"),
                ("l / enter", "log, else details"),
                ("P / L / T", "details / op log / tasks"),
            ),
        ),
        (
            "bookmarks",
            (
                ("n / e", "new / describe"),
                ("dd", "delete (history kept)"),
                ("u / ctrl+r", "undo / redo n, dd, f"),
                (
                    "v V space",
                    "select a range / mark; r/i merge, esc clears",
                ),
                ("=", "diff vs main or 2 revs"),
                ("f / @", "advance main / move @"),
                ("o", "open selected in editor"),
                ("W", "clean up workspaces"),
            ),
        ),
    ),
)


class HelpBody:
    """The key reference: two columns when they fit, else one; long descriptions wrap in place."""

    GAP = 4
    MIN_DESCRIPTION = 10  # half the narrowest description column before the columns stack

    def __init__(self, extra: tuple[HelpSection, ...] = ()) -> None:
        self.extra = extra  # appended to the right column, e.g. configured commands

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        left, right = HELP[0], HELP[1] + self.extra
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
                table.add_row(Text(key), Text(what))  # not markup: [commands], shell code
            parts.append(table)
        return Group(*parts)


class HelpScreen(MessageScreen):
    """The full key reference; scrolls when the terminal is too short for it."""

    DEFAULT_CSS = """
    HelpScreen > Vertical { width: 84; }
    HelpScreen VerticalScroll { height: auto; scrollbar-size-vertical: 1; }
    """
    CLOSE_KEYS = MessageScreen.CLOSE_KEYS | {"question_mark"}

    def __init__(self, extra: tuple[HelpSection, ...] = ()) -> None:
        super().__init__("sbx-tui keys", "")
        self.extra = extra

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.title_text, classes="title")
            with VerticalScroll():
                yield Static(HelpBody(self.extra))
            yield Static(KeyHints(f"{SCROLL_HINT} · q/esc/? close"), classes="hint")

    def on_resize(self, event: events.Resize) -> None:
        self.call_after_refresh(self._fit_body)

    def _fit_body(self) -> None:
        # Queued resize events may outlive the dialog's children during teardown.
        for body in self.query(VerticalScroll):
            fit_body(body)
