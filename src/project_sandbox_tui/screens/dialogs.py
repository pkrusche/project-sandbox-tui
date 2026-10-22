"""Small modal dialogs: new branch, help, message/preview."""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static

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


@dataclass(frozen=True)
class ForceRemove:
    """CleanupScreen result: force-delete this kept workspace (after confirmation)."""

    workspace: Workspace


class CleanupScreen(ModalScreen[list[Workspace] | ForceRemove | None]):
    """List every workspace and merged branch; removable ones are preselected, the rest say why."""

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
        branches = sum(ws.path is None for ws in self.workspaces)
        kept = len(self.workspaces) - removable
        summary = f"{len(self.workspaces) - branches} workspaces"
        if branches:
            summary += f" · {branches} merged branches"
        summary += f" · {removable} can be removed"
        if kept:
            summary += f" · {kept} kept (reason shown)"
        with Vertical():
            yield Static("clean up workspaces", classes="title")
            yield Static(summary)
            # Not a SelectionList: its options are one line each, which cut long reasons off.
            yield OptionList(*(self._label(i) for i in range(len(self.workspaces))))
            yield Static(
                "space toggle · a all/none · enter remove · D force-delete kept · esc cancel",
                classes="hint",
            )

    def _label(self, index: int) -> Text:
        ws = self.workspaces[index]
        label = Text("[x] " if index in self.selected else "[ ] " if ws.removable else "    ")
        label.append(ws.name, style="bold" if ws.removable else "bold dim")
        label.append(f"  {ws.path}" if ws.path else "  branch", style="dim")
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


HELP_TEXT = """\
navigation
  j / k  gg / G  ctrl+d / u   down / up · top / bottom · half page
  /      filter (esc clears)  ?  this help   q  quit (runs continue)
sessions (one sandbox per branch)
  i          interactive (inline, or tmux / zellij / screen: ui.terminal)
  r          unsupervised run with a prompt (background)
  x          stop a run; again: SIGINT → SIGTERM → SIGKILL + container
  l / enter  log / transcript     c / C  clear status / forget stuck run
prompt box (unsupervised runs)
  ctrl+enter  launch (enter = newline)  → / ctrl+f  take the suggestion
  ctrl+p / n  older / newer prompt      ctrl+r      search prompt history
branches
  n          new branch / bookmark      W  clean up workspaces
  v / space  select a range / mark branches (esc clears), then r or i
             merges or rebases them into a new branch
  o  open the worktree in your editor   P  show the project-sandbox command
  R  refresh   ctrl+r  reload configuration   H  show / hide cheat-sheet
"""


class HelpScreen(MessageScreen):
    def __init__(self) -> None:
        super().__init__("sbx-tui keys", HELP_TEXT)
