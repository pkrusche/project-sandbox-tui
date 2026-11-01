"""T: every background task across the workspaces, running ones first."""

from __future__ import annotations

import time
from typing import ClassVar

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.widgets import DataTable, Static

from project_sandbox_tui.jobs import _parse_ts
from project_sandbox_tui.screens.dialogs import MessageScreen
from project_sandbox_tui.screens.log import LogScreen
from project_sandbox_tui.tasks import TaskManager, TaskRecord
from project_sandbox_tui.widgets import GLYPHS, KeyHints, PrefixKeys, human_age

HINT = "q back · j/k ^d/^u gg/G move · l/enter output · P details · x cancel (finished: forget)"


def _clock(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def status_text(task: TaskRecord) -> str:
    status = task.status
    if task.exit_code is not None and status != "completed":
        status += f" ({task.exit_code})"
    return status


def order(tasks: list[TaskRecord]) -> list[TaskRecord]:
    """Running tasks first, then the rest; newest first within each."""
    newest = sorted(tasks, key=lambda t: t.started_at, reverse=True)
    return [t for t in newest if t.running] + [t for t in newest if not t.running]


class TasksScreen(Screen[None]):
    DEFAULT_CSS = """
    TasksScreen #tasks-title { height: 1; background: $panel; padding: 0 1; }
    TasksScreen DataTable { height: 1fr; border: none; scrollbar-size-vertical: 1; }
    TasksScreen DataTable > .datatable--header { background: $background; color: $text-muted; }
    TasksScreen #tasks-empty { height: auto; padding: 1 2; color: $text-muted; }
    TasksScreen #tasks-hint { height: auto; color: $text-disabled; padding: 0 1; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q,escape,h,T", "app.pop_screen", show=False),
        Binding("j", "cursor(1)", show=False),
        Binding("k", "cursor(-1)", show=False),
        Binding("ctrl+d", "page(1)", show=False),
        Binding("ctrl+u", "page(-1)", show=False),
        Binding("G", "bottom", show=False),
        Binding("g", "g_prefix", show=False),
        Binding("l", "open_log", show=False),
        Binding("P", "details", show=False),
        Binding("x", "cancel", show=False),
    ]

    def __init__(self, tasks: TaskManager, bookmark: str | None = None) -> None:
        """``bookmark``: start on its newest task, if it has one."""
        super().__init__()
        self.tasks = tasks
        latest = tasks.latest(bookmark) if bookmark else None
        self._select = latest.id if latest else None
        self._prefix = PrefixKeys()
        self._shown: list[tuple[str, tuple[Text, ...]]] | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="tasks-title")
        table: DataTable[Text] = DataTable(cursor_type="row", zebra_stripes=False)
        yield table
        yield Static(
            "No tasks yet. :&!cmd runs in the repository; :*!cmd runs in each selected workspace.",
            id="tasks-empty",
        )
        yield Static(KeyHints(HINT), id="tasks-hint")

    @property
    def table(self) -> DataTable[Text]:
        return self.query_one(DataTable)

    def on_mount(self) -> None:
        for label in ("", "age", "bookmark", "status", "command"):
            self.table.add_column(label, key=label or "glyph")
        self.table.focus()
        self._render_rows()
        # The app refreshes the task records on its timer; show what it saw.
        self.set_interval(1.0, self._render_rows)

    def on_key(self, event: events.Key) -> None:
        self._prefix.other_key(event.key)

    # ------------------------------------------------------------ rendering

    def _cells(self, task: TaskRecord) -> tuple[Text, ...]:
        glyph, colour = GLYPHS.get(task.status, ("?", "dim"))
        end = _parse_ts(task.ended_at) if task.ended_at else 0.0
        age = human_age(time.time() - (end or task.started()))
        status = status_text(task)
        if task.running and self.tasks.cancel_requested(task.id):
            status += " · cancelling"
        return (
            Text(glyph, style=colour),
            Text(age, style="dim"),
            Text(task.bookmark),
            Text(status, style=colour),
            Text(f":{task.line}", no_wrap=True, overflow="ellipsis"),
        )

    def _render_rows(self) -> None:
        rows = [(t.id, self._cells(t)) for t in order(list(self.tasks.tasks.values()))]
        self._title()
        self.query_one("#tasks-empty", Static).display = not rows
        if rows == self._shown:
            return
        table = self.table
        selected = self._select or self.selected_id()
        self._select = None
        table.clear()
        for key, cells in rows:
            table.add_row(*cells, key=key)
        self._shown = rows
        keys = [key for key, _cells in rows]
        if selected in keys:
            table.move_cursor(row=keys.index(selected), animate=False)

    def _title(self) -> None:
        tasks = list(self.tasks.tasks.values())
        running = sum(t.running for t in tasks)
        workspaces = len({t.bookmark for t in tasks if t.running})
        title = Text()
        title.append(" tasks ", style="bold reverse")
        stats = f"{running} running"
        if running:
            stats += f" in {workspaces} workspace{'s' * (workspaces != 1)}"
        stats += f" · {len(tasks) - running} finished"
        title.append(f"   {stats}", style="dim")
        self.query_one("#tasks-title", Static).update(title)

    def selected_id(self) -> str | None:
        table = self.table
        if not table.row_count:
            return None
        key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(key.value) if key.value is not None else None

    def selected_task(self) -> TaskRecord | None:
        task_id = self.selected_id()
        return self.tasks.tasks.get(task_id) if task_id else None

    # ------------------------------------------------------------ navigation

    def action_cursor(self, step: int) -> None:
        table = self.table
        if table.row_count:
            row = min(max(table.cursor_row + step, 0), table.row_count - 1)
            table.move_cursor(row=row, animate=False)

    def action_page(self, direction: int) -> None:
        self.action_cursor(direction * max(1, self.table.size.height // 2))

    def action_bottom(self) -> None:
        self.action_cursor(self.table.row_count)

    def action_g_prefix(self) -> None:
        if self._prefix.press("g"):
            self.action_cursor(-self.table.row_count)

    # ------------------------------------------------------------ actions

    @on(DataTable.RowSelected)
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()  # not the bookmark list's enter
        self.action_open_log()

    def action_open_log(self) -> None:
        task = self.selected_task()
        if task is not None:
            self.app.push_screen(LogScreen(task))

    def action_details(self) -> None:
        task = self.selected_task()
        if task is None:
            return
        lines = [
            f"bookmark    {task.bookmark}",
            f"command     :{task.line}",
            f"runs        sh -c {task.script!r}",
            f"directory   {task.cwd}",
            f"status      {status_text(task)}",
            f"started     {_clock(task.started())}",
        ]
        if task.ended_at:
            lines.append(f"ended       {_clock(_parse_ts(task.ended_at))}")
        lines += [f"pid         {task.pid}", f"output      {task.out_path}"]
        self.app.push_screen(MessageScreen(f"{task.bookmark} :{task.name}", "\n".join(lines)))

    def action_cancel(self) -> None:
        task = self.selected_task()
        if task is None:
            return
        self.notify(self.tasks.cancel_task(task.id), markup=False)
        self._shown = None  # redraw now: the status or the row changed
        self._render_rows()
