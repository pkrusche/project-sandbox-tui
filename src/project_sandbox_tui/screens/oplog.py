"""L: the operations history, one line per bookmark creation, merge, rebase and run.

Drawn like ``jj log`` over the nodes of the bookmark history: each prompt or
session continues its bookmark's line, merges and rebases join their inputs.
"""

from __future__ import annotations

import time
from typing import ClassVar, cast

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.widgets import DataTable, Static

from project_sandbox_tui.jobs import JobManager, JobRecord, Status
from project_sandbox_tui.opgraph import NODE, Node, OpGraph
from project_sandbox_tui.sandbox import LaunchSpec
from project_sandbox_tui.screens.dialogs import MessageScreen
from project_sandbox_tui.screens.log import LogScreen
from project_sandbox_tui.widgets import GLYPHS, KeyHints, PrefixKeys

UNKNOWN = ("?", "dim")  # a run whose outcome was never seen
HINT = "q back · j/k ^d/^u gg/G move · enter/P details · l log"


def _clock(timestamp: float, seconds: bool = False) -> str:
    return time.strftime(f"%Y-%m-%d %H:%M{':%S' if seconds else ''}", time.localtime(timestamp))


class OpLogScreen(Screen[None]):
    DEFAULT_CSS = """
    OpLogScreen #oplog-title { height: 1; background: $panel; padding: 0 1; }
    OpLogScreen DataTable { height: 1fr; border: none; scrollbar-size-vertical: 1; }
    OpLogScreen DataTable > .datatable--header { background: $background; color: $text-muted; }
    OpLogScreen #oplog-hint { height: auto; color: $text-disabled; padding: 0 1; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q,escape,h,L", "app.pop_screen", show=False),
        Binding("j", "cursor(1)", show=False),
        Binding("k", "cursor(-1)", show=False),
        Binding("ctrl+d", "page(1)", show=False),
        Binding("ctrl+u", "page(-1)", show=False),
        Binding("G", "bottom", show=False),
        Binding("g", "g_prefix", show=False),
        Binding("P", "details", show=False),
        Binding("l", "open_log", show=False),
    ]

    def __init__(self, graph: OpGraph, jobs: JobManager, select: str | None = None) -> None:
        """``select``: the node to start on, else the newest."""
        super().__init__()
        self.graph = graph
        self.jobs = jobs
        self._select = select
        self._prefix = PrefixKeys()
        self._shown: list[tuple[str, tuple[Text, ...]]] = []

    def compose(self) -> ComposeResult:
        yield Static("", id="oplog-title")
        table: DataTable[Text] = DataTable(cursor_type="row", zebra_stripes=False)
        yield table
        yield Static(KeyHints(HINT), id="oplog-hint")

    @property
    def table(self) -> DataTable[Text]:
        return self.query_one(DataTable)

    def on_mount(self) -> None:
        for label in ("", "when", "bookmark", "commit", "event"):
            self.table.add_column(label, key=label or "graph")
        self.table.focus()
        self._render_rows()
        # The app reloads the history on each refresh; pick up what it saw.
        self.set_interval(1.0, self._render_rows)

    def on_key(self, event: events.Key) -> None:
        self._prefix.other_key(event.key)

    # ------------------------------------------------------------ rendering

    def status(self, node: Node) -> str:
        """A run's outcome, or the live status of its job until one is recorded."""
        if node.kind != "run" or node.status:
            return node.status
        job = self.jobs.jobs.get(node.bookmark)
        if job is not None and job.started_at == node.job and self.graph.state.is_current(node):
            return job.status
        return ""

    def _cells(self, node: Node, line: tuple[str, int]) -> tuple[Text, ...]:
        state = self.graph.state
        status = self.status(node)
        glyph, colour = (NODE, "") if node.kind != "run" else GLYPHS.get(status, UNKNOWN)
        text, at = line
        graph = Text(text[:at])
        graph.append(glyph, style=colour)
        graph.append(text[at + 1 :])
        current = state.is_current(node)
        bookmark = Text(node.bookmark, style="" if current else "dim")
        if not current:
            bookmark.append(" (deleted)", style="dim italic")
        event = Text(state.describe(node, status), no_wrap=True)
        if node.kind in ("merge", "rebase"):
            event.stylize("bold", 0, len(node.kind))
        return (
            graph,
            Text(_clock(node.at), style="dim"),
            bookmark,
            Text(node.commit[:8], style="dim"),
            event,
        )

    def _render_rows(self) -> None:
        rows = [(node.id, self._cells(node, line)) for node, line in self.graph.state.op_log()]
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
        self._title()

    def _title(self) -> None:
        nodes = list(self.graph.state.nodes.values())
        runs = sum(n.kind == "run" for n in nodes)
        merges = sum(n.kind in ("merge", "rebase") for n in nodes)
        title = Text()
        title.append(" op history ", style="bold reverse")
        stats = f"{len(nodes)} entries · {runs} runs · {merges} merges and rebases"
        title.append(f"   {stats}", style="dim")
        self.query_one("#oplog-title", Static).update(title)

    def selected_id(self) -> str | None:
        table = self.table
        if not table.row_count:
            return None
        key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(key.value) if key.value is not None else None

    def selected_node(self) -> Node | None:
        node_id = self.selected_id()
        return self.graph.state.nodes.get(node_id) if node_id else None

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
        self.action_details()

    def action_details(self) -> None:
        node = self.selected_node()
        if node is None:
            return
        state = self.graph.state
        status = self.status(node)
        lines = [
            f"event       {state.describe(node, status)}",
            f"time        {_clock(node.at, seconds=True)}",
        ]
        if not state.is_current(node):
            lines.append("bookmark    deleted since (its history is kept)")
        parents = [state.nodes[p] for p in node.parents if p in state.nodes]
        label = "after" if node.kind == "run" else "from"
        for i, parent in enumerate(parents):
            what = state.describe(parent, self.status(parent))
            lines.append(f"{label if not i else '':<12}{parent.bookmark}: {what}")
        if node.commit:
            lines.append(f"commit      {node.commit}")
        if node.kind == "run":
            if node.agent:
                lines.append(f"agent       {node.agent} · {node.mode}")
            if node.prompt:
                lines.append(f"prompt      {node.prompt}")
            if node.log:
                lines.append(f"log         {node.log}")
        self.app.push_screen(MessageScreen(node.bookmark, "\n".join(lines)))

    def action_open_log(self) -> None:
        node = self.selected_node()
        if node is None or node.kind != "run":
            self.notify("select a run to view its log", severity="warning")
            return
        job = self.jobs.jobs.get(node.bookmark)
        if job is None or job.started_at != node.job:
            if not node.log:
                self.notify(
                    "no log: interactive sessions log to their terminal", severity="warning"
                )
                return
            # An earlier run: its files stay in the logs directory. The status
            # is only shown, so an unseen outcome may read "unknown".
            job = JobRecord(
                node.bookmark,
                LaunchSpec(node.bookmark, node.agent),
                0,
                node.job,
                log_path=node.log,
                stdout_path=node.stdout,
                status=cast("Status", self.status(node) or "unknown"),
            )
        self.app.push_screen(LogScreen(job))
