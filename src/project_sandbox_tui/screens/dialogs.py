"""Small modal dialogs: new branch, help, message/preview."""

from __future__ import annotations

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, SelectionList, Static
from textual.widgets.selection_list import Selection

from project_sandbox_tui.cleanup import Workspace
from project_sandbox_tui.vcs import validate_branch_name
from project_sandbox_tui.widgets import modifier_key

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
    """Ask for a branch name and start revision."""

    DEFAULT_CSS = _MODAL_CSS.format(name="NewBranchScreen")
    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, default_start: str, kind: str) -> None:
        super().__init__()
        self.default_start = default_start
        self.kind = kind

    def compose(self) -> ComposeResult:
        noun = "bookmark" if self.kind == "jj" else "branch"
        with Vertical():
            yield Static(f"new {noun}", classes="title")
            yield Static("name", classes="row-label")
            yield Input(placeholder="feature/my-change", id="name")
            yield Static("start at", classes="row-label")
            yield Input(value=self.default_start, id="start")
            yield Static("", id="error")
            yield Static("tab fields · enter create · esc cancel", classes="hint")

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
    """Show a block of plain text; any key closes."""

    DEFAULT_CSS = _MODAL_CSS.format(name="MessageScreen")

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self.title_text = title
        self.body = body

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.title_text, classes="title")
            yield Static(Text(self.body))
            yield Static("any key to close", classes="hint")

    def on_key(self, event: events.Key) -> None:
        # Consume the key so it cannot also trigger a binding on the screen below.
        event.stop()
        event.prevent_default()
        if not modifier_key(event.key):
            self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Show a block of plain text; y confirms, any other key cancels."""

    DEFAULT_CSS = _MODAL_CSS.format(name="ConfirmScreen")

    def __init__(self, title: str, body: str, action: str) -> None:
        super().__init__()
        self.title_text = title
        self.body = body
        self.action_text = action

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.title_text, classes="title")
            yield Static(Text(self.body))
            yield Static(f"y {self.action_text} · any other key cancels", classes="hint")

    def on_key(self, event: events.Key) -> None:
        # Consume the key so it cannot also trigger a binding on the screen below.
        event.stop()
        event.prevent_default()
        if not modifier_key(event.key):
            self.dismiss(event.key == "y")


class CleanupScreen(ModalScreen[list[Workspace] | None]):
    """List every workspace found; clean ones are preselected, the rest say why they stay."""

    DEFAULT_CSS = (
        _MODAL_CSS.format(name="CleanupScreen")
        + """
    CleanupScreen > Vertical { width: 110; }
    CleanupScreen SelectionList { border: none; height: auto; max-height: 20; padding: 0; }
    """
    )
    BINDINGS = [
        Binding("escape,q", "cancel", show=False),
        Binding("enter", "confirm", show=False, priority=True),
        Binding("j", "move(1)", show=False),
        Binding("k", "move(-1)", show=False),
        Binding("a", "toggle_all", show=False),
    ]

    def __init__(self, workspaces: list[Workspace]) -> None:
        super().__init__()
        self.workspaces = workspaces

    def compose(self) -> ComposeResult:
        removable = sum(ws.removable for ws in self.workspaces)
        kept = len(self.workspaces) - removable
        summary = f"{len(self.workspaces)} found · {removable} can be removed"
        if kept:
            summary += f" · {kept} kept (reason shown)"
        with Vertical():
            yield Static("clean up workspaces", classes="title")
            yield Static(summary)
            yield SelectionList[int](
                *(
                    Selection(self._label(ws), i, ws.removable, disabled=not ws.removable)
                    for i, ws in enumerate(self.workspaces)
                )
            )
            yield Static(
                "space toggle · a all/none · enter remove selected · esc cancel", classes="hint"
            )

    @staticmethod
    def _label(ws: Workspace) -> Text:
        label = Text(ws.name, style="bold")
        label.append(f"  {ws.path}", style="dim")
        label.append(f"  {ws.status}", style="green" if ws.removable else "yellow")
        return label

    def on_mount(self) -> None:
        self.query_one(SelectionList).focus()

    def action_move(self, step: int) -> None:
        options = self.query_one(SelectionList)
        options.action_cursor_down() if step > 0 else options.action_cursor_up()

    def action_toggle_all(self) -> None:
        options = self.query_one(SelectionList)
        if options.selected:
            options.deselect_all()
        else:
            options.select_all()

    def action_confirm(self) -> None:
        selected = self.query_one(SelectionList).selected
        self.dismiss([self.workspaces[i] for i in sorted(selected)])

    def action_cancel(self) -> None:
        self.dismiss(None)


HELP_TEXT = """\
navigation
  j / k          down / up            gg / G      top / bottom
  ctrl+d / u     half page            /           filter (esc clears)

sessions (one sandbox per branch)
  i              interactive session (inline, or tmux/zellij/screen per ui.terminal)
  r              unsupervised run with a prompt (background)
  x              stop background run
  x again        escalate: SIGINT → SIGTERM → SIGKILL + container stop
  l / enter      log / transcript of the last run
  c / C          clear a finished status / force-forget a stuck record

prompt box (unsupervised runs)
  ctrl+enter     submit prompt and launch (enter inserts a newline)
  → / ctrl+f     take the history suggestion shown as ghost text
  ctrl+p / n     older / newer prompt starting with what you typed
  ctrl+r         search the prompt history

branches
  n              new branch/bookmark  W           clean up workspaces
  o              open the branch worktree in your editor
  P              preview the project-sandbox command
  R              refresh now          ctrl+r      reload configuration
  H              show / hide the cheat-sheet
  ?              this help            q           quit (background runs continue)
"""


class HelpScreen(MessageScreen):
    def __init__(self) -> None:
        super().__init__("sbx-tui keys", HELP_TEXT)
