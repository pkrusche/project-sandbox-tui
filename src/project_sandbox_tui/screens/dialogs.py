"""Small modal dialogs: new branch, help, message/preview."""

from __future__ import annotations

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from project_sandbox_tui.vcs import validate_branch_name

_MODAL_CSS = """
{name} {{ align: center middle; }}
{name} > Vertical {{
    width: 80; max-width: 100%; height: auto; max-height: 100%;
    background: $panel; padding: 1 2;
}}
{name} .title {{ text-style: bold; margin-bottom: 1; }}
{name} .row-label {{ color: $text-muted; height: 1; padding: 0 1; }}
{name} .hint {{ color: $text-disabled; margin-top: 1; }}
{name} Input {{ border: none; height: 1; padding: 0 1; }}
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
        self.dismiss(None)


HELP_TEXT = """\
navigation
  j / k          down / up            gg / G      top / bottom
  ctrl+d / u     half page            /           filter (esc clears)

sessions (one sandbox per branch)
  i              interactive session in this terminal
  r              unsupervised run with a prompt (background)
  x              stop background run (again: force kill)
  l / enter      log / transcript of the last run
  c              clear a finished status

branches
  n              new branch / bookmark
  o              open the branch worktree in your editor
  P              preview the project-sandbox command
  R              refresh now

  ?              this help            q           quit (background runs continue)
"""


class HelpScreen(MessageScreen):
    def __init__(self) -> None:
        super().__init__("sbx-tui keys", HELP_TEXT)
