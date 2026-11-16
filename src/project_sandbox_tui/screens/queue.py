"""Per-bookmark unsupervised queue, with Vim list editing and persistent marks."""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.widgets import DataTable, Input, Static

from project_sandbox_tui.queue import QueuedTask, TaskQueue
from project_sandbox_tui.screens.dialogs import MessageScreen
from project_sandbox_tui.screens.log import LogScreen
from project_sandbox_tui.widgets import KeyHints, PrefixKeys

HINT = (
    "q back · j/k ^d/^u gg/G move · / filter · n add · e edit · l/enter edit/log"
    " · P details · c commits · dd delete"
    " · J/K reorder · v/V/space select · u undo · ^r redo · p pause/resume · ma mark · 'a jump"
)


class QueueScreen(Screen[None]):
    DEFAULT_CSS = """
    QueueScreen #queue-title { height: auto; background: $panel; padding: 0 1; }
    QueueScreen DataTable { height: 1fr; border: none; scrollbar-size-vertical: 1; }
    QueueScreen #queue-filter { height: 1; border: none; padding: 0 1; display: none; }
    QueueScreen #queue-filter.visible { display: block; }
    QueueScreen #queue-empty { height: auto; padding: 1 2; color: $text-muted; }
    QueueScreen #queue-hint { height: auto; color: $text-disabled; padding: 0 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q,h,L", "app.pop_screen", show=False),
        Binding("escape", "escape", show=False),
        Binding("j", "cursor(1)", show=False),
        Binding("k", "cursor(-1)", show=False),
        Binding("ctrl+d", "page(1)", show=False),
        Binding("ctrl+u", "page(-1)", show=False),
        Binding("G", "bottom", show=False),
        Binding("g", "g_prefix", show=False),
        Binding("slash", "filter", show=False),
        Binding("n,r", "add", show=False),
        Binding("e,l", "edit", show=False),
        Binding("P", "details", show=False),
        Binding("c", "commits", show=False),
        Binding("d", "d_prefix", show=False),
        Binding("J", "reorder(1)", show=False),
        Binding("K", "reorder(-1)", show=False),
        Binding("v,V", "visual", show=False),
        Binding("space", "toggle_mark", show=False),
        Binding("u", "undo", show=False),
        Binding("ctrl+r", "redo", show=False),
        Binding("p", "pause", show=False),
        Binding("m", "mark_prefix", show=False),
        Binding("apostrophe,backtick", "jump_prefix", show=False),
    ]

    def __init__(
        self,
        queue: TaskQueue,
        branch: str,
        edit: Callable[[str, QueuedTask | None], None],
        review: Callable[[str, str], object] | None = None,
    ) -> None:
        super().__init__()
        self.queue = queue
        self.branch = branch
        self.edit_task = edit
        self.review = review
        self._prefix = PrefixKeys()
        self._mark_prefix = ""
        self.marked: list[str] = []
        self._visual_anchor: str | None = None
        self.filter_text = ""
        self._shown: list[tuple[str, tuple[Text, ...]]] | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="queue-title")
        yield Input(placeholder="filter prompts", id="queue-filter")
        yield DataTable(cursor_type="row")
        yield Static("No tasks. Press n to add a prompt.", id="queue-empty")
        yield Static(KeyHints(HINT), id="queue-hint")

    @property
    def table(self) -> DataTable[Text]:
        return self.query_one(DataTable)

    def on_mount(self) -> None:
        for label in ("", "#", "status", "agent", "commits", "prompt"):
            self.table.add_column(label)
        self.table.focus()
        self.render_queue()
        self.set_interval(0.5, self.render_queue)

    def selected_id(self) -> str | None:
        if not self.table.row_count:
            return None
        key, _ = self.table.coordinate_to_cell_key(self.table.cursor_coordinate)
        return str(key.value)

    def visible_ids(self) -> list[str]:
        return [key for key, _ in self._shown or []]

    def selection(self) -> list[str]:
        ids = self.visible_ids()
        selected = list(self.marked)
        current = self.selected_id()
        if self._visual_anchor in ids and current in ids:
            start, end = sorted((ids.index(self._visual_anchor), ids.index(current)))
            selected += ids[start : end + 1]
        return list(dict.fromkeys(selected)) or ([current] if current else [])

    def render_queue(self) -> None:
        try:
            self.queue.load()
        except (OSError, ValueError) as exc:
            self.query_one("#queue-title", Static).update(Text(str(exc), style="red"))
            return
        queue = self.queue.get(self.branch)
        live = {t.id for t in queue.entries}
        self.marked = [key for key in self.marked if key in live]
        selected = self.selected_id()
        chosen = set(self.selection()) if self.marked or self._visual_anchor else set()
        rows: list[tuple[str, tuple[Text, ...]]] = [
            (
                task.id,
                (
                    Text("+" if task.id in chosen else ""),
                    Text(str(index) if task.status == "pending" else ""),
                    Text(task.status),
                    Text(task.spec.label()),
                    Text(", ".join(c[:12] for c in task.commits)),
                    Text(
                        " ".join(task.spec.prompt_text.split()), no_wrap=True, overflow="ellipsis"
                    ),
                ),
            )
            for index, task in enumerate(queue.entries, 1)
            if self.filter_text.lower() in task.spec.prompt_text.lower() or task.id in chosen
        ]
        state = "paused" if queue.paused else "active"
        title = f" {self.branch} · {len(queue.tasks)} queued · {state}"
        running = sum(t.status == "running" for t in queue.history)
        title += f" · {running} running · {len(queue.history) - running} finished"
        if queue.error:
            title += f" · {queue.error}"
        self.query_one("#queue-title", Static).update(Text(title))
        self.query_one("#queue-empty", Static).display = not rows
        if rows != self._shown:
            row = self.table.cursor_row
            self.table.clear()
            for key, cells in rows:
                self.table.add_row(*cells, key=key)
            self._shown = rows
            ids = self.visible_ids()
            self.table.move_cursor(
                row=ids.index(selected) if selected in ids else min(row, max(0, len(ids) - 1)),
                animate=False,
            )

    def change(self, action: Callable[[], None], *, pause: bool = True) -> None:
        try:
            if pause:
                self.queue.pause(self.branch, True)
            action()
        except (OSError, ValueError) as exc:
            self.notify(str(exc), severity="error")
        self.render_queue()

    def on_key(self, event: events.Key) -> None:
        if isinstance(self.focused, Input):
            return
        self._prefix.other_key(event.key)
        if self._mark_prefix:
            command, self._mark_prefix = self._mark_prefix, ""
            event.stop()
            event.prevent_default()
            key = event.character or ""
            if len(key) != 1 or not key.isascii() or not key.isalpha():
                return
            if command == "m":
                if selected := self.selected_id():
                    self.change(lambda: self.queue.mark(self.branch, key, selected), pause=False)
            else:
                target = self.queue.get(self.branch).marks.get(key)
                self.filter_text = ""
                self.render_queue()
                if target in self.visible_ids():
                    self.table.move_cursor(row=self.visible_ids().index(target), animate=False)

    def action_mark_prefix(self) -> None:
        self._mark_prefix = "m"

    def action_jump_prefix(self) -> None:
        self._mark_prefix = "'"

    def action_cursor(self, step: int) -> None:
        self.table.move_cursor(
            row=min(max(0, self.table.cursor_row + step), max(0, self.table.row_count - 1)),
            animate=False,
        )
        if self._visual_anchor:
            self.render_queue()

    @on(DataTable.RowHighlighted)
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        event.stop()
        if self._visual_anchor:
            self.render_queue()

    def action_page(self, step: int) -> None:
        self.action_cursor(step * max(1, self.table.size.height // 2))

    def action_bottom(self) -> None:
        self.action_cursor(self.table.row_count)

    def action_g_prefix(self) -> None:
        if self._prefix.press("g"):
            self.action_cursor(-self.table.row_count)

    def action_d_prefix(self) -> None:
        if self._prefix.press("d"):
            ids = self.selection()
            self.change(lambda: self.queue.delete(self.branch, ids))
            self.marked.clear()
            self._visual_anchor = None
            self.render_queue()

    def action_visual(self) -> None:
        if self._visual_anchor:
            self.marked = self.selection()
            self._visual_anchor = None
        else:
            self._visual_anchor = self.selected_id()
        self.render_queue()

    def action_toggle_mark(self) -> None:
        current = self.selected_id()
        if current:
            if current in self.marked:
                self.marked.remove(current)
            else:
                self.marked.append(current)
        self.action_cursor(1)  # same mark-and-advance behavior as the bookmark list
        self.render_queue()

    def action_reorder(self, step: int) -> None:
        ids = self.selection()
        if self._visual_anchor:
            self.marked = ids
            self._visual_anchor = None
        self.change(lambda: self.queue.move(self.branch, ids, step))

    def action_undo(self) -> None:
        self.change(lambda: self.queue.undo(self.branch))

    def action_redo(self) -> None:
        self.change(lambda: self.queue.undo(self.branch, redo=True))

    def action_pause(self) -> None:
        self.change(
            lambda: self.queue.pause(self.branch, not self.queue.get(self.branch).paused),
            pause=False,
        )

    def action_add(self) -> None:
        self.edit_task(self.branch, None)

    def action_edit(self) -> None:
        task = self.selected_task()
        if task:
            if task.job:
                self.app.push_screen(LogScreen(task.job))
            else:
                self.edit_task(self.branch, task)

    def selected_task(self) -> QueuedTask | None:
        return next(
            (t for t in self.queue.get(self.branch).entries if t.id == self.selected_id()), None
        )

    def action_details(self) -> None:
        task = self.selected_task()
        if task is None:
            return
        lines = [
            f"task        {task.id}",
            f"status      {task.status}",
            f"agent       {task.spec.label()}",
            f"base        {task.base_commit or '—'}",
            f"result      {task.result_commit or '—'}",
            "commits     " + ("\n            ".join(task.commits) or "—"),
        ]
        if task.job:
            lines += [
                f"started     {task.job.started_at}",
                f"ended       {task.job.ended_at or '—'}",
                f"exit code   {task.job.exit_code}",
                f"log         {task.job.log_path}",
            ]
        lines += ["", task.spec.prompt_text]
        self.app.push_screen(MessageScreen(f"{self.branch} task", "\n".join(lines)))

    def action_commits(self) -> None:
        task = self.selected_task()
        if task and task.base_commit and task.result_commit and self.review:
            self.review(task.base_commit, task.result_commit)
        else:
            self.notify("no recorded commit range for this task")

    @on(DataTable.RowSelected)
    def _selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.action_edit()

    def action_filter(self) -> None:
        box = self.query_one(Input)
        box.add_class("visible")
        box.value = self.filter_text
        box.focus()

    @on(Input.Changed)
    def _filtered(self, event: Input.Changed) -> None:
        event.stop()
        self.filter_text = event.value
        self.render_queue()

    @on(Input.Submitted)
    def _filter_done(self, event: Input.Submitted) -> None:
        event.stop()
        self.query_one(Input).remove_class("visible")
        self.table.focus()

    def action_escape(self) -> None:
        if self.filter_text or isinstance(self.focused, Input):
            self.filter_text = ""
            self.query_one(Input).value = ""
            self.query_one(Input).remove_class("visible")
            self.table.focus()
        elif self.marked or self._visual_anchor:
            self.marked.clear()
            self._visual_anchor = None
        else:
            self.app.pop_screen()
            return
        self.render_queue()
