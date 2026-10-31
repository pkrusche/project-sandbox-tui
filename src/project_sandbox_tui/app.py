"""The Textual application: branch list + session control."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import shlex
import subprocess
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, cast

from rich.cells import cell_len
from rich.console import Console, ConsoleOptions, RenderResult
from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.css.query import NoMatches
from textual.notifications import SeverityLevel
from textual.screen import Screen
from textual.widgets import DataTable, Input, Static
from textual.widgets.data_table import ColumnKey

from project_sandbox_tui import launchers, notify, octopus, terminal
from project_sandbox_tui.cleanup import (
    Workspace,
    find_workspaces,
    force_removal_details,
    force_remove_workspace,
    remove_workspaces,
)
from project_sandbox_tui.config import (
    Config,
    ConfigError,
    LastUsed,
    ProjectTrust,
    load,
    load_candidate,
    remember,
    resolve_launch_defaults,
    trusted_config_path,
)
from project_sandbox_tui.history import PromptHistory
from project_sandbox_tui.jobs import BusyError, JobManager, JobRecord, _parse_ts
from project_sandbox_tui.opgraph import NODE, OpGraph, UndoEntry, UndoItem, draw, toposort
from project_sandbox_tui.sandbox import (
    LaunchSpec,
    Mode,
    SessionRecord,
    build_argv,
    read_sessions,
    running_by_branch,
)
from project_sandbox_tui.screens.dialogs import (
    CleanupScreen,
    ConfirmScreen,
    ForceRemove,
    HelpScreen,
    MessageScreen,
    NewBranchScreen,
)
from project_sandbox_tui.screens.launch import LaunchScreen, OctopusSetup
from project_sandbox_tui.screens.log import LogScreen
from project_sandbox_tui.vcs import (
    WORKING_COPY,
    Branch,
    JjBackend,
    VcsError,
    main_branch,
    sort_branches,
    workspace_path,
)
from project_sandbox_tui.widgets import KeyHints, PrefixKeys, modifier_key

STATE_HEADING = "## Current state"  # octopus state report appended to prompts

GLYPHS = {
    "running": ("▶", "green"),
    "completed": ("✓", "green"),
    "failed": ("✗", "red"),
    "timeout": ("⏱", "yellow"),
    "rate_limited": ("⚠", "yellow"),
    "interrupted": ("⚠", "yellow"),
    "orphaned": ("⚠", "yellow"),
    "external": ("◆", "cyan"),
}


def human_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def count_label(count: int) -> str:
    """'1 bookmark', '3 bookmarks', 'no bookmarks' (the working-copy row is not counted)."""
    if count == 0:
        return "no bookmarks"
    return f"{count} bookmark{'' if count == 1 else 's'}"


def _ts(value: str) -> float:
    return _parse_ts(value)


def _clock(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


HISTORY_LINES = 12  # history entries in the P pop-up
EDITOR_BATCH = 3  # o asks before opening more workspaces than this at once


@dataclass
class Row:
    branch: Branch
    job: JobRecord | None
    session: SessionRecord | None
    graph: tuple[str, int] | None = None  # history graph line and its node position

    def name_cell(self, selected: bool = False) -> Text:
        b = self.branch
        if selected:
            return Text(f"+ {b.label}", style="bold magenta")
        name = Text(b.label, style="bold" if b.is_current else "")
        if b.is_current:
            name.append(" ●", style="green")
        return name

    def status(self) -> tuple[str, str, str]:
        """(glyph, colour, status text); the P pop-up has the details."""
        job, sess = self.job, self.session
        glyph, colour, status = "", "", ""
        if job is not None:
            glyph, colour = GLYPHS.get(job.status, ("?", ""))
            if job.running:
                status = "interactive" if job.mode == "interactive" else "running"
                if job.external:
                    status = "interactive (ext)"
                if job.spec.operation:  # an integration of several branches
                    verb = "merging" if job.spec.operation == "merge" else "rebasing"
                    status = verb if job.mode == "background" else f"{verb} ({status})"
            else:
                status = job.status.replace("_", " ")
                if job.exit_code is not None and job.status != "completed":
                    status += f" ({job.exit_code})"
                last_run = _ts(job.ended_at) or _ts(job.started_at)
                if last_run:
                    status += f" · {human_age(time.time() - last_run)} ago"
        if sess is not None and (job is None or not job.running):
            glyph, colour = GLYPHS["external"]
            status = "busy (other)"
        return glyph, colour, status

    def graph_cell(self) -> Text:
        """The history graph line, its node showing the run status."""
        glyph, colour, _status = self.status()
        if self.graph is None:  # the working copy is not part of the graph
            return Text(glyph or " ", style=colour)
        line, at = self.graph
        cell = Text(line[:at])
        cell.append(glyph or NODE, style=colour)
        cell.append(line[at + 1 :])
        return cell

    def cells(self, selected: bool = False) -> tuple[Text, ...]:
        b = self.branch
        _glyph, colour, status = self.status()
        return (
            self.graph_cell(),
            self.name_cell(selected),
            self.comparison_cell(),
            Text(b.head, style="dim"),
            Text(b.subject, overflow="ellipsis", no_wrap=True),
            Text(status, style=colour),
        )

    def comparison_cell(self) -> Text:
        comparison = self.branch.comparison
        if comparison is None:
            return Text("?", style="dim")
        if not (comparison.ahead or comparison.behind or comparison.changed_files):
            return Text("=", style="dim")
        marker = Text()
        for symbol, count, style in (
            ("↑", comparison.ahead, "green"),
            ("↓", comparison.behind, "yellow"),
            ("Δ", comparison.changed_files, "cyan"),
        ):
            if count:
                if marker:
                    marker.append(" ")
                marker.append(f"{symbol}{count}", style=style)
        return marker


MAIN_BINDINGS: list[BindingType] = [
    Binding("j", "app.cursor(1)", show=False),
    Binding("k", "app.cursor(-1)", show=False),
    Binding("ctrl+d", "app.page(1)", show=False),
    Binding("ctrl+u", "app.page(-1)", show=False),
    Binding("G", "app.bottom", show=False),
    Binding("g", "app.g_prefix", show=False),
    Binding("slash", "app.filter", show=False),
    Binding("v,V", "app.visual", show=False),
    Binding("space", "app.toggle_mark", show=False),
    Binding("escape", "app.clear_filter", show=False),
    Binding("i", "app.launch('interactive')", show=False),
    Binding("r", "app.launch('background')", show=False),
    Binding("n", "app.new_branch", show=False),
    Binding("l", "app.open_log", show=False),
    Binding("o", "app.open_editor", show=False),
    Binding("d", "app.d_prefix", show=False),
    Binding("equals_sign", "app.diff", show=False),
    Binding("u", "app.undo", show=False),
    Binding("ctrl+r", "app.redo", show=False),
    Binding("f", "app.fast_forward_main", show=False),
    Binding("at", "app.move_working_copy", show=False),
    Binding("P", "app.preview", show=False),
    Binding("W", "app.cleanup_workspaces", show=False),
    Binding("x", "app.cancel", show=False),
    Binding("R", "app.refresh", show=False),
    Binding("ctrl+l", "app.reload_config", show=False),
    Binding("question_mark", "app.help", show=False),
    Binding("H", "app.toggle_cheatsheet", show=False),
    Binding("q", "app.quit", show=False),
]


CHEATSHEET: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "general",
        (
            ("j k", "down / up"),
            ("gg G", "top / bottom"),
            ("^d ^u", "half page"),
            ("/", "filter"),
            ("?", "full help"),
            ("H", "hide sheet"),
            ("q", "quit"),
        ),
    ),
    (
        "sessions",
        (
            ("i", "interactive"),
            ("r", "unsupervised run"),
            ("x", "cancel · 3×: forget"),
            ("l ⏎", "log / transcript"),
            ("P", "details / command"),
        ),
    ),
    (
        "bookmarks",
        (
            ("n", "new bookmark"),
            ("dd", "delete bookmark"),
            ("u ^r", "undo / redo"),
            ("v V ␣", "select bookmarks"),
            ("=", "diff revisions"),
            ("f", "fast-forward main"),
            ("@", "working copy to bookmark"),
            ("o", "open in editor"),
            ("W", "clean workspaces"),
            ("R ^l", "refresh / reload config"),
        ),
    ),
)

FOOTER = "? full help · H keys · j/k move · i interactive · r run · q quit"


def _cheatsheet_section(title: str, keys: tuple[tuple[str, str], ...]) -> Text:
    key_width = max(cell_len(key) for key, _what in keys) + 2
    section = Text(title, style="bold")
    for key, what in keys:
        section.append("\n")
        section.append(key + " " * (key_width - cell_len(key)), style="bold")
        section.append(what, style="dim")
    return section


class Cheatsheet:
    """The key cheat-sheet under the branch list.

    Sections sit side by side and flow onto more rows when the terminal is too
    narrow for all of them, instead of cutting their descriptions off.
    """

    GAP = 3

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        sections = [_cheatsheet_section(title, keys) for title, keys in CHEATSHEET]
        widths = [max(cell_len(line) for line in s.plain.splitlines()) for s in sections]
        per_row = len(sections)
        while per_row > 1 and sum(widths[:per_row]) + self.GAP * (per_row - 1) > options.max_width:
            per_row -= 1
        grid = Table.grid(padding=(0, self.GAP))
        for _ in range(per_row):
            grid.add_column(no_wrap=True)
        for start in range(0, len(sections), per_row):
            if start:
                grid.add_row()  # blank line between rows of sections
            grid.add_row(*sections[start : start + per_row])
        yield grid


class MainScreen(Screen[None]):
    """Branch list. Bindings live here so they are inactive under modal forms."""

    BINDINGS: ClassVar[list[BindingType]] = MAIN_BINDINGS

    def compose(self) -> ComposeResult:
        yield Static("", id="header")
        table: DataTable[Text] = DataTable(cursor_type="row", zebra_stripes=False)
        yield table
        yield Static(Cheatsheet(), id="cheatsheet")
        yield Input(placeholder="filter", id="filter")
        yield Static(KeyHints(FOOTER), id="footer")

    def on_resize(self, event: events.Resize) -> None:
        self.update_help_layout()

    def update_help_layout(self) -> None:
        """Collapse the sheet before it crowds out the working copy and a bookmark."""
        sheet = self.query_one("#cheatsheet", Static)
        gutter = sheet.styles.gutter
        width = max(1, self.size.width - gutter.width)
        console = self.app.console
        options = console.options.update(width=width, height=None)
        sheet_height = len(console.render_lines(Cheatsheet(), options)) + gutter.height
        # Header, table header + working copy + bookmark + horizontal scrollbar,
        # and room for the filter even before it opens.
        app = cast("SandboxTUI", self.app)
        expanded = app.cheatsheet_visible and sheet_height + 6 <= self.size.height
        sheet.set_class(not expanded, "hidden")
        self.set_class(expanded, "with-cheatsheet")

    def on_key(self, event: events.Key) -> None:
        modifier = modifier_key(event.key)
        if not modifier:  # gg / dd only when typed in a row
            app = cast("SandboxTUI", self.app)
            if app._prefix.pending:
                app._prefix.other_key(event.key)
                app._show_pending()
        if modifier:
            hints = {
                "ctrl": "ctrl+d/u half page down/up · ctrl+r redo · ctrl+l reload configuration",
                "shift": "G bottom · V select · P details · R refresh · W clean up · H sheet",
            }
            hint = hints.get(modifier, f"{modifier}: no shortcuts on the bookmark list")
            self.query_one("#cheatsheet", Static).update(KeyHints(hint))
            self.query_one("#footer", Static).update(KeyHints(f"? full help · {hint}"))
            event.stop()
        else:
            self.query_one("#cheatsheet", Static).update(Cheatsheet())
            self.query_one("#footer", Static).update(KeyHints(FOOTER))


class SandboxTUI(App[None]):
    TITLE = "sbx-tui"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: $background; }
    #header { height: 1; background: $panel; padding: 0 1; }
    MainScreen DataTable { height: 1fr; border: none; scrollbar-size-vertical: 1; }
    MainScreen DataTable > .datatable--header { background: $background; color: $text-muted; }
    #filter { height: 1; border: none; padding: 0 1; display: none; }
    #filter.visible { display: block; }
    #footer { height: 1; color: $text-disabled; padding: 0 1; }
    #cheatsheet { height: auto; padding: 1 2; border-top: solid $panel; }
    #cheatsheet.hidden { display: none; }
    MainScreen.with-cheatsheet #footer { display: none; }
    """

    def __init__(
        self,
        repo: Path,
        backend: JjBackend,
        cfg: Config,
        jobs: JobManager | None = None,
        sessions_dir: Path | None = None,
        *,
        config_path: Path | None = None,
        dry_run: bool = False,
    ) -> None:
        super().__init__()
        self.repo = repo
        self.backend = backend
        self.cfg = cfg
        self.config_path = config_path
        self.dry_run = dry_run
        self.jobs = jobs or JobManager(repo, cfg)
        self.sessions_dir = sessions_dir
        self.branches: list[Branch] = []
        self.sessions: dict[str, SessionRecord] = {}
        self.filter_text = ""
        self._prefix = PrefixKeys()  # gg and dd
        self._refreshing = False
        self._refresh_again = False
        self._refreshed = asyncio.Event()  # set when the current refresh pass ends
        self._pending_select: str | None = None
        self.default_start = cfg.project.default_branch_start
        self.main = MainScreen()
        self.cheatsheet_visible = cfg.ui.cheatsheet
        self.prompt_history = PromptHistory.for_repo(repo)
        self.graph = OpGraph(self.jobs.state_dir / "opgraph.json")
        self._graph_error = ""
        # Creating, deleting and undoing run in order: u right after n undoes n.
        self._history_lock = asyncio.Lock()
        # Branch selection for operations on several branches: space marks
        # rows (kept in marking order), v starts a visual range at the cursor.
        self.marked: list[str] = []
        self._visual_anchor: str | None = None
        self._confirming_quit = False
        self._drafts: dict[tuple[str, ...], str] = {}  # unsent prompts, by launch target

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = False,
    ) -> None:
        """Toasts are plain text: they quote branch names, paths and jj/tmux
        output, whose ``[`` would be read as markup. Screens' ``notify`` passes
        ``markup=True`` by default, so it is ignored here."""
        super().notify(message, title=title, severity=severity, timeout=timeout, markup=False)

    # ------------------------------------------------------------ layout

    @property
    def table(self) -> DataTable[Text]:
        return self.main.query_one(DataTable)

    async def on_mount(self) -> None:
        await self.push_screen(self.main)
        self.show_cheatsheet(self.cheatsheet_visible)
        table = self.table
        for label, width in (
            ("", 1),
            ("bookmark", None),
            ("vs main", None),
            ("head", 8),
            ("subject", 32),  # Ellipsize long messages to leave room for session status.
            ("status", None),
        ):
            table.add_column(label, width=width, key=label or "graph")
        table.focus()
        await self.refresh_data()
        self._refresh_timer = self.set_interval(self.cfg.ui.refresh_seconds, self.refresh_data)
        self._review_project_config()

    # ------------------------------------------------------------ data

    @property
    def worktree_dir(self) -> Path | None:
        wd = self.cfg.project.worktree_dir
        return self.cfg.resolve_path(wd, self.repo) if wd else None

    async def refresh_data(self) -> None:
        if self._refreshing:
            # Coalesce: rerun once the current pass ends, and wait for that rerun
            # so callers see data at least as fresh as their own changes.
            self._refresh_again = True
            await self._refreshed.wait()
            return
        self._refreshing = True
        self._refreshed = asyncio.Event()
        try:
            await self._refresh_once()
            while self._refresh_again:
                self._refresh_again = False
                await self._refresh_once()
        finally:
            self._refreshing = False
            self._refreshed.set()

    async def _refresh_once(self) -> None:
        listed_at = time.time()
        listed = True
        try:
            branches = await asyncio.to_thread(
                self.backend.list_branches, compare_to_main=self.cfg.project.main_branch
            )
        except VcsError as exc:
            self.notify(str(exc), severity="error", timeout=8)
            branches = self.branches
            listed = False
        self.branches = sort_branches(branches)
        if listed:  # first: sessions and jobs are read back to back, then redrawn
            await self._sync_graph(listed_at)
        records = await asyncio.to_thread(read_sessions, self.sessions_dir)
        self.sessions = running_by_branch(
            records, self.repo, [b.name for b in self.branches], self.worktree_dir
        )
        for job in self.jobs.refresh(self.sessions):
            self._announce(job)
        # NoMatches: the app is shutting down (e.g. a worker finishing after quit)
        with contextlib.suppress(NoMatches):
            self._render_table()

    async def _sync_graph(self, listed_at: float) -> None:
        """Adopt bookmarks created elsewhere and record finished runs."""
        try:
            await asyncio.to_thread(
                self.graph.sync,
                self.branches,
                dict(self.jobs.jobs),  # a snapshot: load() refills the dict in place
                listed_at,
                self.backend.bookmarked_ancestors,
                self.main_name() or "",
            )
        except OSError as exc:
            error = f"could not save the bookmark history: {exc}"
            if error != self._graph_error:  # once, not on every refresh
                self.notify(error, severity="error", timeout=8)
            self._graph_error = error
        else:
            self._graph_error = ""

    def _announce(self, job: JobRecord) -> None:
        if not self.jobs.mark_notified(job):
            return
        title = f"sbx-tui: {job.branch} {job.status.replace('_', ' ')}"
        detail = job.spec.label()
        if job.exit_code is not None:
            detail += f" · exit {job.exit_code}"
        severity: SeverityLevel = "information" if job.status == "completed" else "warning"
        self.notify(
            f"{job.branch}: {job.status.replace('_', ' ')} — {detail}",
            severity=severity,
            timeout=10,
        )
        notify.send(title, detail, self.cfg.ui.notify_command)
        if self.cfg.ui.bell:
            self.bell()

    def main_name(self) -> str | None:
        return main_branch(
            {b.name for b in self.branches if not b.working_copy}, self.cfg.project.main_branch
        )

    def visible_rows(self) -> list[Row]:
        """The working copy, then bookmarks in history order: children above parents."""
        needle = self.filter_text.lower()
        # Selected rows stay visible: an action must never hit a row the filter hides.
        pinned = {*self.marked, self._visual_anchor}
        shown = {
            b.name: b
            for b in self.branches
            if not needle or needle in b.name.lower() or b.name in pinned
        }
        state = self.graph.state
        bookmarks = [name for name, b in shown.items() if not b.working_copy]
        parents = state.bookmark_parents(bookmarks)
        activity = state.activity()
        recency = {n: max(float(shown[n].timestamp), activity.get(n, 0.0)) for n in bookmarks}
        order = toposort(bookmarks, parents, recency)
        lines = draw(order, parents)
        names = [name for name, b in shown.items() if b.working_copy] + order
        return [
            Row(shown[n], self.jobs.jobs.get(n), self.sessions.get(n), lines.get(n)) for n in names
        ]

    def _render_table(self) -> None:
        table = self.table
        scroll_x = table.scroll_target_x
        selected = self._pending_select or self.selected_branch_name()
        rows = self.visible_rows()
        baseline = self.main_name()
        comparison_col = table.columns[ColumnKey("vs main")]
        comparison_col.label = Text(f"vs {baseline or self.cfg.project.main_branch or 'main'}")
        comparison_col.content_width = cell_len(comparison_col.label.plain)
        chosen = set(self.selection())  # before clear(): the visual range follows the cursor
        # Fixed width with room for the "+ " / " ●" markers, so marking a row in
        # place (update_cell) never has to widen the column.
        name_col = table.columns.get(ColumnKey("bookmark"))
        if name_col is not None:
            name_col.auto_width = False
            name_col.width = max([len("bookmark"), *(len(r.branch.label) + 2 for r in rows)])
        graph_col = table.columns.get(ColumnKey("graph"))
        if graph_col is not None:  # shrinks again once wide parts of the graph are gone
            graph_col.auto_width = False
            graph_col.width = max([1, *(row.graph_cell().cell_len for row in rows)])
        table.clear()
        for row in rows:
            table.add_row(*row.cells(row.branch.name in chosen), key=row.branch.name)
        if selected is not None:
            names = [r.branch.name for r in rows]
            if selected in names:
                table.move_cursor(row=names.index(selected), animate=False)
                if selected == self._pending_select:
                    self._pending_select = None
        # clear() resets scrolling. Restore now so consecutive redraws retain the
        # offset, then clamp it after layout/cursor scrolling settles.
        table.scroll_to(x=scroll_x, animate=False)
        table.call_after_refresh(table.scroll_to, x=scroll_x, animate=False)
        self._render_header(chosen)

    def _render_header(self, chosen: set[str]) -> None:
        running = self.jobs.running_count()
        busy_other = sum(
            1 for n in self.sessions if not (n in self.jobs.jobs and self.jobs.jobs[n].running)
        )
        header = Text()
        header.append(" sbx-tui ", style="bold reverse")
        header.append(f"  {self.repo}", style="bold")
        count = sum(1 for b in self.branches if not b.working_copy)
        stats = f"{count_label(count)} · {running} running"
        if busy_other:
            stats += f" · {busy_other} external"
        if self.filter_text:
            stats += f" · filter: {self.filter_text}"
        if self._visual_anchor is not None:
            stats += " · VISUAL"
        if chosen:
            stats += f" · {len(chosen)} selected"
            needle = self.filter_text.lower()
            if hidden := sum(needle not in name.lower() for name in chosen):
                stats += f" ({hidden} outside the filter)"
        header.append(f"   {stats}", style="dim")
        if pending := self._prefix.pending:  # like vim's showcmd
            header.append(f"   {pending}…", style="bold yellow")
        self.main.query_one("#header", Static).update(header)

    def branch(self, name: str) -> Branch | None:
        return next((b for b in self.branches if b.name == name), None)

    def selected_branch_name(self) -> str | None:
        table = self.table
        if table.row_count == 0:
            return None
        try:
            key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        return str(key.value) if key.value is not None else None

    # ------------------------------------------------------------ selection

    def selection(self) -> list[str]:
        """Selected branches in merge / stacking order: marks in the order they
        were made, then the visual range in list order. The jj working-copy row
        cannot take part."""
        names = [r.branch.name for r in self.visible_rows()]
        chosen = [n for n in self.marked]
        cursor = self.selected_branch_name()
        if self._visual_anchor in names and cursor in names:
            i, j = sorted((names.index(self._visual_anchor), names.index(cursor)))
            chosen += [n for n in names[i : j + 1] if n not in chosen]
        excluded = {b.name for b in self.branches if b.working_copy}
        known = {b.name for b in self.branches}
        return [n for n in chosen if n not in excluded and n in known]

    def _refresh_marks(self) -> None:
        """Redraw the name cells and header after the selection changed."""
        table = self.table
        chosen = set(self.selection())
        for row in self.visible_rows():
            try:
                table.update_cell(
                    row.branch.name, "bookmark", row.name_cell(row.branch.name in chosen)
                )
            except Exception:
                return  # table being rebuilt; the next render shows the marks
        self._render_header(chosen)

    def action_visual(self) -> None:
        """v / V: start a range at the cursor; press again to keep it as marks."""
        if self._visual_anchor is None:
            self._visual_anchor = self.selected_branch_name()
        else:
            self.marked = self.selection()
            self._visual_anchor = None
        self._refresh_marks()

    def action_toggle_mark(self) -> None:
        name = self.selected_branch_name()
        if name is None:
            return
        branch = self.branch(name)
        if branch is not None and branch.working_copy:
            self.notify("the working copy cannot be merged; select bookmarks", severity="warning")
            return
        if name in self.marked:
            self.marked.remove(name)
        else:
            self.marked.append(name)
        self.action_cursor(1)  # like ranger: mark and move on
        self._refresh_marks()

    def _end_visual(self) -> None:
        """An action ends visual mode, as in vim; the range stays marked until esc."""
        if self._visual_anchor is not None:
            self.marked = self.selection()
            self._visual_anchor = None
            self._refresh_marks()

    def clear_branch_selection(self) -> bool:
        had = bool(self.marked) or self._visual_anchor is not None
        self.marked = []
        self._visual_anchor = None
        if had:
            self._refresh_marks()
        return had

    @on(DataTable.RowHighlighted)
    def _cursor_moved(self) -> None:
        if self._visual_anchor is not None:
            self._refresh_marks()

    # ------------------------------------------------------------ navigation

    def action_cursor(self, step: int) -> None:
        table = self.table
        if table.row_count:
            row = min(max(table.cursor_row + step, 0), table.row_count - 1)
            table.move_cursor(row=row, animate=False)

    def action_page(self, direction: int) -> None:
        self.action_cursor(direction * max(1, self.table.size.height // 2))

    def action_bottom(self) -> None:
        table = self.table
        if table.row_count:
            table.move_cursor(row=table.row_count - 1, animate=False)

    def action_g_prefix(self) -> None:
        if self._prefix.press("g"):
            self.table.move_cursor(row=0, animate=False)
        self._show_pending()

    def action_d_prefix(self) -> None:
        if self._prefix.press("d"):
            self.action_delete_bookmarks()
        self._show_pending()

    def _show_pending(self) -> None:
        """Show a half-typed gg / dd in the header until it completes or times out."""
        with contextlib.suppress(NoMatches):
            self._render_header(set(self.selection()))
        if self._prefix.pending:
            self.set_timer(PrefixKeys.TIMEOUT + 0.05, self._show_pending)

    def action_filter(self) -> None:
        box = self.main.query_one("#filter", Input)
        box.add_class("visible")
        box.value = self.filter_text
        box.focus()

    @on(Input.Changed, "#filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        self.filter_text = event.value.strip()
        self._render_table()

    @on(Input.Submitted, "#filter")
    def _filter_done(self) -> None:
        self.main.query_one("#filter", Input).remove_class("visible")
        self.table.focus()

    def action_clear_filter(self) -> None:
        box = self.main.query_one("#filter", Input)
        box.remove_class("visible")
        self.table.focus()
        if self.filter_text:
            self.filter_text = ""
            box.value = ""
            self._render_table()
        else:
            self.clear_branch_selection()  # esc also drops marks / a visual range

    @on(DataTable.RowSelected)
    def _row_selected(self) -> None:
        self.action_open_log()

    # ------------------------------------------------------------ actions

    def _spec_preview(self, spec: LaunchSpec) -> str:
        paths = self.jobs.new_run_paths(spec.branch) if spec.mode == "background" else None
        if spec.mode == "background" and not spec.prompt_text.strip():
            spec = dataclasses.replace(spec, prompt_text="<prompt>")
        return shlex.join(build_argv(self.cfg, self.repo, spec, paths))

    def _form_preview(self, spec: LaunchSpec) -> str:
        """Shorter preview for the launch form: state-dir paths abbreviated."""
        return self._spec_preview(spec).replace(str(self.jobs.logs_dir), "<logs>")

    def _require_branch(self) -> str | None:
        name = self.selected_branch_name()
        if name is None:
            self.notify("no bookmark or working copy selected", severity="warning")
        return name

    def _action_branches(self) -> list[str]:
        """Use the selection when present, otherwise the highlighted row."""
        self._end_visual()
        chosen = self.selection()
        if chosen:
            return chosen
        name = self._require_branch()
        return [name] if name is not None else []

    def action_fast_forward_main(self) -> None:
        chosen = self._action_branches()
        if not chosen:
            return
        if len(chosen) != 1:
            self.notify("select one target to fast-forward main", severity="warning")
            return
        target = chosen[0]
        branch = self.branch(target)
        if branch is None:
            self.notify("select a target to fast-forward main", severity="warning")
            return
        main = self.main_name()
        if main is None:
            self.notify("no main bookmark found; configure project.main_branch", severity="warning")
            return
        self._fast_forward_main(main, target)

    def action_move_working_copy(self) -> None:
        chosen = self._action_branches()
        if not chosen:
            return
        if len(chosen) != 1:
            self.notify("select one bookmark for the working copy", severity="warning")
            return
        target = chosen[0]
        branch = self.branch(target)
        if branch is None or branch.working_copy:
            self.notify("select a bookmark for the working copy", severity="warning")
            return
        self._move_working_copy(target)

    @work(exclusive=True, group="vcs")
    async def _move_working_copy(self, target: str) -> None:
        def move_if_idle() -> str | None:
            # Share the launch lock so another TUI cannot start a session between
            # the busy check and checkout. Read records again, not the UI cache.
            with self.jobs._locked():
                self.jobs.load()
                sessions = running_by_branch(
                    read_sessions(self.sessions_dir), self.repo, [WORKING_COPY], self.worktree_dir
                )
                reason = self.jobs._busy_reason(WORKING_COPY, sessions)
                if reason:
                    raise BusyError(reason)
                return self.backend.move_working_copy_to(target)

        try:
            saved = await asyncio.to_thread(move_if_idle)
        except (BusyError, VcsError) as exc:
            self.notify(str(exc), severity="error", timeout=8)
        else:
            message = f"working copy moved onto {target}"
            if saved:
                message += f"; previous change saved as {saved}"
            self.notify(message)
        await self.refresh_data()

    @work(exclusive=True, group="vcs")
    async def _fast_forward_main(self, main: str, target: str) -> None:
        try:
            async with self._history_lock:
                moved = await asyncio.to_thread(self._fast_forward_recorded, main, target)
        except VcsError as exc:
            self.notify(str(exc), severity="error", timeout=8)
        except OSError as exc:
            self.notify(f"could not update the bookmark history: {exc}", severity="error")
        else:
            self.notify(
                f"fast-forwarded {main} to {target} (u undoes)"
                if moved
                else f"{main} already points to {target}"
            )
        await self.refresh_data()

    def _fast_forward_recorded(self, main: str, target: str) -> bool:
        """Fast-forward ``main`` and make the move undoable."""
        with self.graph.transaction() as state:
            old = self.backend._bookmark_commit(main)
            if not self.backend.fast_forward(main, target):
                return False
            new = self.backend._bookmark_commit(main)
            lineage = state.current.get(main, "")
            state.push_undo(UndoEntry("move", [UndoItem(main, lineage, old, new)]))
        return True

    def action_diff(self) -> None:
        chosen = self._action_branches()
        if not chosen:
            return
        if len(chosen) > 2:
            self.notify("select one or two revisions to diff", severity="warning")
            return
        if len(chosen) == 1:
            base = main_branch({b.name for b in self.branches}, self.cfg.project.main_branch)
            if base is None:
                self.notify(
                    "no main bookmark found; configure project.main_branch", severity="warning"
                )
                return
            target = chosen[0]
        else:
            base, target = chosen
        self._show_diff(base, target)

    def _show_diff(self, base: str, target: str) -> None:
        try:
            with self._suspended():
                print(f"\n── sbx-tui: diff {base} → {target} ──\n", flush=True)
                with terminal.preserved():
                    try:
                        code = subprocess.call(
                            self.backend.diff_command(base, target), cwd=self.backend.root
                        )
                    except KeyboardInterrupt:
                        code = 130
                # Pagers can exit immediately for short diffs. Keep the output
                # visible until the user is ready to resume the TUI.
                with contextlib.suppress(EOFError, KeyboardInterrupt, OSError):
                    input("\n── press Enter to return to sbx-tui ──")
        except OSError as exc:
            self.notify(f"could not start jj diff: {exc}", severity="error")
            return
        if code not in (0, 130):
            self.notify(f"jj diff exited with {code}", severity="error")

    def action_launch(self, mode: Mode) -> None:
        self._end_visual()
        selected = self.selection()
        if len(selected) > 1:
            self._launch_octopus(mode, selected)
            return
        name = selected[0] if selected else self._require_branch()  # one mark wins, as for f
        if name is None:
            return
        reason = self.jobs.busy_reason(name, self.sessions)
        if reason:
            self.notify(reason, severity="warning")
            return
        last = LastUsed.load(self.repo)
        defaults = resolve_launch_defaults(self.cfg, last)
        previous = self.jobs.jobs.get(name)
        prompt = previous.spec.prompt_text if previous is not None else ""
        prompt = prompt.split(STATE_HEADING, 1)[0].rstrip()  # drop an old state report
        branch = self.branch(name)
        screen = LaunchScreen(
            name,
            mode,
            self.cfg,
            last,
            defaults,
            self._form_preview,
            prompt,
            branch_label=branch.label if branch is not None else name,
            opens_in=launchers.describe(self.cfg.ui.terminal_mode())
            if mode == "interactive"
            else "",
            prompt_history=self.prompt_history,
            branches=[b.name for b in self.branches],
            draft=self._drafts.get((name,), ""),
        )
        known_branches = {b.name for b in self.branches}

        def chosen(spec: LaunchSpec | None) -> None:
            self._keep_draft((name,), screen.draft, spec)
            start = name if spec is not None and spec.branch not in known_branches else None
            self._launch_chosen(spec, new_branch_start=start, draft_key=(name,))

        self.push_screen(screen, chosen)

    def _launch_octopus(self, mode: Mode, chosen: list[str]) -> None:
        """r / i with bookmarks selected: merge or rebase into a new bookmark."""
        try:
            default_base = self.default_start or self.backend.default_start()
        except VcsError:
            default_base = ""
        overrides = {
            "merge": self.cfg.prompts.octopus_merge,
            "rebase": self.cfg.prompts.octopus_rebase,
        }
        setup = OctopusSetup(
            inputs=chosen,
            existing={b.name for b in self.branches if not b.working_copy},
            default_base=default_base,
            templates={
                op: overrides[op] or octopus.DEFAULT_TEMPLATES[op] for op in octopus.OPERATIONS
            },
        )
        last = LastUsed.load(self.repo)
        screen = LaunchScreen(
            "",
            mode,
            self.cfg,
            last,
            resolve_launch_defaults(self.cfg, last),
            self._form_preview,
            opens_in=launchers.describe(self.cfg.ui.terminal_mode())
            if mode == "interactive"
            else "",
            prompt_history=self.prompt_history,
            octopus=setup,
            draft=self._drafts.get(tuple(chosen), ""),
        )

        def launched(spec: LaunchSpec | None) -> None:
            self._keep_draft(tuple(chosen), screen.draft, spec)
            self._launch_chosen(spec, draft_key=tuple(chosen))

        self.push_screen(screen, launched)

    def _keep_draft(self, key: tuple[str, ...], draft: str | None, spec: LaunchSpec | None) -> None:
        """Keep an edited prompt until its launch starts, so esc never loses it."""
        if draft is None:  # not an unsupervised run
            return
        if not draft:
            self._drafts.pop(key, None)
            return
        self._drafts[key] = draft
        if spec is None:
            self.notify("prompt kept as a draft: r brings it back")

    def _launch_chosen(
        self,
        spec: LaunchSpec | None,
        *,
        new_branch_start: str | None = None,
        draft_key: tuple[str, ...] = (),
    ) -> None:
        if spec is None:
            return
        # Start only once the form is gone and its key event is fully processed:
        # dismiss() runs this callback before the screen has been popped, and an
        # interactive session blocks the event loop until the agent exits.
        if spec.operation:
            self.clear_branch_selection()
            self.call_after_refresh(self._integrate, spec, draft_key)
            return
        self.call_after_refresh(
            self._start_launch, spec, new_branch_start=new_branch_start, draft_key=draft_key
        )

    @work(exclusive=True, group="octopus")
    async def _integrate(self, spec: LaunchSpec, draft_key: tuple[str, ...] = ()) -> None:
        """Merge / rebase the inputs into the result branch on the host, then start
        the session that resolves what is left and reviews the integration."""
        operation: octopus.Operation = "rebase" if spec.operation == "rebase" else "merge"
        plan = octopus.OctopusPlan(operation, list(spec.inputs), spec.branch, spec.base)
        try:
            branches = await asyncio.to_thread(self.backend.list_branches)
            octopus.validate(plan, {b.name for b in branches if not b.working_copy})
            reason = self.jobs.busy_reason(spec.branch, self.sessions)
            if reason:
                raise BusyError(reason)
            moving = [i for i in spec.inputs if self.jobs.busy_reason(i, self.sessions)]
            if moving:
                self.notify(
                    f"{', '.join(moving)} still has a session running; the {operation} "
                    "uses its current state",
                    severity="warning",
                    timeout=8,
                )
            self.notify(f"{spec.branch}: preparing {operation} of {len(spec.inputs)} bookmarks…")
            state = await asyncio.to_thread(octopus.prepare, self.repo, plan)
        except (octopus.OctopusError, BusyError, VcsError) as exc:
            self.notify(f"{spec.branch}: {exc}", severity="error", timeout=10)
            await self.refresh_data()
            return
        warning = await asyncio.to_thread(
            self._record_creation, spec.branch, operation, list(spec.inputs)
        )
        if warning:
            self.notify(warning, severity="warning")
        self.notify(
            f"{spec.branch}: {state.summary()}",
            severity="information" if state.clean else "warning",
            timeout=8,
        )
        typed = spec.prompt_text
        if spec.mode == "background":
            if spec.agent == "bash":
                spec.prompt_text = f"printf '%s\\n' {shlex.quote(state.report())}\n{typed}"
            else:
                spec.prompt_text = f"{typed.rstrip()}\n\n{state.report()}\n"
        await self.refresh_data()  # the result branch exists now
        self._start_launch(spec, history_text=typed, banner=state.report(), draft_key=draft_key)

    def _start_launch(
        self,
        spec: LaunchSpec,
        *,
        new_branch_start: str | None = None,
        history_text: str | None = None,
        banner: str = "",
        draft_key: tuple[str, ...] = (),
    ) -> None:
        try:
            # Bookmarks and external sessions may have changed while the
            # launch form was open. Recheck immediately before starting.
            self.branches = sort_branches(self.backend.list_branches())
            if new_branch_start is not None:
                known = {b.name for b in self.branches if not b.working_copy}
                self.backend.create_branch(spec.branch, new_branch_start)
                if warning := self._record_creation(
                    spec.branch, "create", [new_branch_start] if new_branch_start in known else None
                ):
                    self.notify(warning, severity="warning")
                self.branches = sort_branches(self.backend.list_branches())
                self._pending_select = spec.branch
            if self.branch(spec.branch) is None:
                raise VcsError(f"{spec.branch} no longer exists")
            self.sessions = running_by_branch(
                read_sessions(self.sessions_dir),
                self.repo,
                [b.name for b in self.branches],
                self.worktree_dir,
            )
            if spec.mode == "background":
                job = self.jobs.start_background(spec, self.sessions)
                self._record_run(job, history_text)
                self.notify(f"{spec.branch}: started {spec.label()} (pid {job.pid})")
            elif launchers.effective_mode(self.cfg.ui.terminal_mode()) != "inline":
                self._launch_external(spec)
            else:
                self._launch_interactive(spec, banner)
        except (BusyError, ConfigError, VcsError, launchers.LaunchError) as exc:
            self.notify(str(exc), severity="error", timeout=8)
        except OSError as exc:
            self.notify(f"could not start project-sandbox: {exc}", severity="error", timeout=8)
        else:
            self._drafts.pop(draft_key, None)
            if spec.mode == "background":
                self.prompt_history.add(spec.prompt_text if history_text is None else history_text)
            # Only a launch that actually started becomes the new default.
            remember(
                self.repo,
                spec.agent,
                spec.model,
                spec.effort,
                spec.timeout if spec.mode == "background" else None,
            )
        self.call_later(self.refresh_data)

    @contextlib.contextmanager
    def _suspended(self) -> Generator[None, None, None]:
        """``App.suspend()`` that always resumes.

        Textual's ``suspend()`` has no try/finally around its ``yield``: an
        exception in the block (e.g. project-sandbox not on PATH) skips
        ``resume_application_mode()`` and leaves the TUI deaf to input.
        """
        error: BaseException | None = None
        with self.suspend():
            try:
                yield
            except BaseException as exc:  # re-raised once the app has resumed
                error = exc
        if error is not None:
            raise error

    def _launch_interactive(self, spec: LaunchSpec, banner: str = "") -> None:
        with self.jobs.interactive(spec, self.sessions) as job:
            self._record_run(job)
            with self._suspended():
                print(
                    f"\n── sbx-tui: {spec.label()} on {spec.branch} ──\n{shlex.join(job.argv)}\n",
                    flush=True,
                )
                if banner:  # e.g. what an octopus merge left to resolve; read it first
                    print(banner, flush=True)
                    with contextlib.suppress(EOFError, KeyboardInterrupt, OSError):
                        input("\n── press Enter to start the session ──")
                # The agent's `container run -it` may die with the terminal still
                # raw or non-blocking; put it back before prompting or resuming.
                with terminal.preserved():
                    try:
                        proc = subprocess.Popen(job.argv, cwd=self.repo)
                    except OSError:
                        job.exit_code = 127  # like a shell: command could not be run
                        raise
                    while True:
                        try:
                            job.exit_code = proc.wait()
                            break
                        except KeyboardInterrupt:
                            continue  # the child got the SIGINT too; wait for it to wind down
                if job.exit_code:
                    with contextlib.suppress(EOFError, KeyboardInterrupt):
                        input(
                            f"\n── project-sandbox exited with {job.exit_code}; "
                            "press Enter to return to sbx-tui ──"
                        )

    def _launch_external(self, spec: LaunchSpec) -> None:
        """Hand the session to tmux / zellij / screen / a custom terminal command."""
        argv = build_argv(self.cfg, self.repo, spec)
        launch = launchers.external_launch(
            self.cfg.ui.terminal_mode(), argv, spec.branch, self.repo, self.cfg.ui.terminal_command
        )
        job = self.jobs.record_external(spec, argv, self.sessions)
        try:
            launchers.start(launch, cwd=self.repo)
        except (OSError, launchers.LaunchError):
            self.jobs.discard(job)
            raise
        self._record_run(job)
        self.notify(f"{spec.branch}: {launch.message}", timeout=8)

    # ------------------------------------------------------------ history

    def _record_creation(self, name: str, kind: str, parents: list[str] | None) -> str | None:
        """Start the new bookmark's lineage and make its creation undoable.

        ``parents`` defaults to the bookmarks on the nearest ancestors. Returns
        a warning instead of raising: the bookmark exists either way.
        """
        try:
            branches = self.backend.list_branches()
            commit = next((b.commit for b in branches if b.name == name), "")
            names = {b.name for b in branches if not b.working_copy}
            main = main_branch(names, self.cfg.project.main_branch) or ""
            with self.graph.transaction() as state:
                if parents is None:
                    parents = state.infer_parents(
                        name, commit, branches, self.backend.bookmarked_ancestors, main
                    )
                node = state.add_lineage(name, kind, parents, commit)
                state.push_undo(UndoEntry("create", [UndoItem(name, node.id, commit)]))
        except (OSError, VcsError) as exc:
            return f"{name}: not recorded in the bookmark history: {exc}"
        return None

    def _record_run(self, job: JobRecord, prompt: str | None = None) -> None:
        """``prompt``: what was typed, if the run's prompt has generated parts."""
        if job.branch == WORKING_COPY:  # not a bookmark
            return
        try:
            with self.graph.transaction() as state:
                state.add_run(job, prompt)
        except OSError as exc:
            self.notify(f"{job.branch}: run not recorded in the history: {exc}", severity="warning")

    def _sessions_now(self, names: list[str]) -> dict[str, SessionRecord]:
        """Sessions read again, not the UI cache, for busy checks under the jobs lock."""
        return running_by_branch(
            read_sessions(self.sessions_dir), self.repo, names, self.worktree_dir
        )

    def action_delete_bookmarks(self) -> None:
        """dd: delete the selected bookmarks, keeping their history; u restores them."""
        main = self.main_name()
        names = []
        for name in self._action_branches():
            branch = self.branch(name)
            if branch is None:
                continue
            if branch.working_copy:
                self.notify("the working copy is not a bookmark", severity="warning")
            elif name == main:
                self.notify(f"{name} is the main bookmark; not deleted", severity="warning")
            else:
                names.append(name)
        if names:
            self._delete_bookmarks(names)

    @work(group="history")  # not exclusive: cancelling would drop what jj already did
    async def _delete_bookmarks(self, names: list[str]) -> None:
        try:
            async with self._history_lock:
                deleted, problems = await asyncio.to_thread(self._delete_idle, names)
        except OSError as exc:
            deleted, problems = [], [f"could not update the bookmark history: {exc}"]
        for problem in problems:
            self.notify(problem, severity="warning", timeout=8)
        if deleted:
            self.clear_branch_selection()
            self.notify(f"deleted {', '.join(deleted)} (u restores)")
        await self.refresh_data()

    def _delete_idle(self, names: list[str]) -> tuple[list[str], list[str]]:
        problems: list[str] = []
        items: list[UndoItem] = []
        # The jobs lock keeps other instances from starting a session meanwhile.
        with self.graph.transaction() as state, self.jobs._locked():
            self.jobs.load()
            sessions = self._sessions_now(names)
            for name in names:
                reason = self.jobs._busy_reason(name, sessions)
                if reason:
                    problems.append(f"not deleted: {reason}")
                    continue
                try:
                    commit = self.backend.delete_bookmark(name)
                except VcsError as exc:
                    problems.append(f"could not delete {name}: {exc}")
                    continue
                items.append(UndoItem(name, state.current.pop(name, ""), commit))
            if items:
                state.push_undo(UndoEntry("delete", items))
        return [item.bookmark for item in items], problems

    def action_undo(self) -> None:
        self._undo(redo=False)

    def action_redo(self) -> None:
        self._undo(redo=True)

    @work(group="history")
    async def _undo(self, redo: bool) -> None:
        try:
            async with self._history_lock:
                message, restored = await asyncio.to_thread(self._undo_step, redo)
        except (BusyError, OSError, VcsError) as exc:
            self.notify(f"{'redo' if redo else 'undo'} failed: {exc}", severity="error", timeout=8)
        else:
            self.notify(message)
            if restored:
                self._pending_select = restored
        await self.refresh_data()

    def _undo_step(self, redo: bool) -> tuple[str, str | None]:
        """Undo or redo the latest bookmark creation or deletion.

        Returns a message and a restored bookmark to select. Steps interrupted
        by a jj failure are not recorded, and complete when retried.
        """
        verb = "redo" if redo else "undo"
        with self.graph.transaction() as state, self.jobs._locked():
            entry = state.redo_entry() if redo else state.undo_entry()
            if entry is None:
                return f"nothing to {verb}", None
            if entry.op == "move":  # f: put the bookmark back, unless it moved again
                for item in entry.items:
                    before, after = item.commit, item.moved_to
                    if redo:
                        self.backend.move_bookmark(item.bookmark, before, after)
                    else:
                        self.backend.move_bookmark(item.bookmark, after, before)
                state.position += 1 if redo else -1
                moved = entry.items[0].bookmark
                return f"{verb} fast-forward: moved {moved} {'forward' if redo else 'back'}", moved
            existing = {b.name: b.commit for b in self.backend.list_branches()}
            names = [item.bookmark for item in entry.items]
            if (entry.op == "create") != redo:  # remove the bookmarks again
                self.jobs.load()
                sessions = self._sessions_now(names)
                for name in names:
                    if reason := self.jobs._busy_reason(name, sessions):
                        raise BusyError(reason)
                for item in entry.items:
                    if item.bookmark in existing:
                        item.commit = self.backend.delete_bookmark(item.bookmark)
                    if state.current.get(item.bookmark) == item.lineage:
                        del state.current[item.bookmark]
                done, restored = "deleted", None
            else:
                taken = [
                    i.bookmark
                    for i in entry.items
                    if existing.get(i.bookmark, i.commit) != i.commit
                ]
                if taken:
                    raise VcsError(f"{', '.join(taken)} exists again; delete it first")
                for item in entry.items:
                    if item.bookmark not in existing:
                        self.backend.restore_bookmark(item.bookmark, item.commit)
                    if item.lineage in state.nodes:
                        state.current[item.bookmark] = item.lineage
                done, restored = "restored", names[0]
            state.position += 1 if redo else -1
        what = "creation" if entry.op == "create" else "deletion"
        return f"{verb} {what}: {done} {entry.names()}", restored

    def _history_lines(self, name: str) -> list[str]:
        state = self.graph.state
        verbs = {"create": "created", "merge": "merge of", "rebase": "rebase of"}
        lines = []
        for node in state.history(name)[:HISTORY_LINES]:
            if node.kind == "run":
                what = f"{node.mode or 'run'} {node.agent}".rstrip()
                if node.status:
                    what += f" → {node.status.replace('_', ' ')}"
                if node.prompt:
                    what += f": {node.prompt}"
            else:
                parents = [state.nodes[p].bookmark for p in node.parents if p in state.nodes]
                what = verbs.get(node.kind, "existing bookmark")
                if parents:
                    joined = ", ".join(parents)
                    what += f" {joined}" if node.kind in ("merge", "rebase") else f" from {joined}"
            lines.append(f"{_clock(node.at)}  {what}")
        return lines

    def _job_details(self, job: JobRecord) -> list[str]:
        now = time.time()
        status = job.status.replace("_", " ")
        if job.exit_code is not None:
            status += f" (exit {job.exit_code})"
        lines = [f"last run    {status}", f"agent       {job.spec.label()} · {job.mode}"]
        if job.external:
            lines[-1] += " · external terminal"
        started = _ts(job.started_at)
        if started:
            lines.append(f"started     {_clock(started)} ({human_age(now - started)} ago)")
        if job.ended_at:
            ended = _ts(job.ended_at)
            lines.append(f"ended       {_clock(ended)} (after {human_age(ended - started)})")
        ids = [
            f"pid {job.pid}" if job.pid > 0 else "",
            f"session {job.session_id}" if job.session_id else "",
            f"container {job.container_name}" if job.container_name else "",
        ]
        if any(ids):
            lines.append(f"ids         {' · '.join(i for i in ids if i)}")
        prompt = job.spec.prompt_text.strip().splitlines()
        if prompt:
            lines.append(f"prompt      {prompt[0]}" + (" …" if len(prompt) > 1 else ""))
        if job.log_path:
            lines.append(f"log         {job.log_path}")
        if job.argv:
            lines.append(f"command     {shlex.join(job.argv)}")
        return lines

    def action_new_branch(self) -> None:
        start = self.default_start
        if not start:
            try:
                start = self.backend.default_start()
            except VcsError:
                start = ""
        self.push_screen(NewBranchScreen(start), self._new_branch_chosen)

    def _new_branch_chosen(self, result: tuple[str, str] | None) -> None:
        self._create_branch(result)

    @work(exclusive=True, group="vcs")
    async def _create_branch(self, result: tuple[str, str] | None) -> None:
        if result is None:
            return
        name, start = result
        known = {b.name for b in self.branches if not b.working_copy}
        async with self._history_lock:
            try:
                await asyncio.to_thread(self.backend.create_branch, name, start)
            except VcsError as exc:
                self.notify(f"could not create {name}: {exc}", severity="error", timeout=8)
                return
            warning = await asyncio.to_thread(
                self._record_creation, name, "create", [start] if start in known else None
            )
        if warning:
            self.notify(warning, severity="warning")
        self.notify(f"created {name} at {start} (u undoes)")
        self._pending_select = name  # applied by whichever refresh sees it first
        await self.refresh_data()

    def action_open_log(self) -> None:
        chosen = self._action_branches()
        if not chosen:
            return
        if len(chosen) > 1:
            self.notify(
                "select one bookmark or working copy to view its log (esc clears the selection)",
                severity="warning",
            )
            return
        name = chosen[0]
        job = self.jobs.jobs.get(name)
        if job is None:  # no log yet: the history and command are the next best thing
            self.action_preview()
            return
        self.push_screen(LogScreen(job))

    def action_open_editor(self) -> None:
        chosen = self._action_branches()
        if len(chosen) <= EDITOR_BATCH:
            for name in chosen:
                self._open_editor(name)
            return

        def decided(confirmed: bool | None) -> None:
            if confirmed:
                for name in chosen:
                    self._open_editor(name)

        body = "\n".join(chosen) + "\n\nEach one opens in its own editor."
        self.push_screen(ConfirmScreen(f"open {len(chosen)} workspaces?", body, "open"), decided)

    def _open_editor(self, name: str) -> None:
        branch = self.branch(name)
        path = (
            self.repo
            if branch is not None and branch.working_copy
            else workspace_path(self.repo, name, self.worktree_dir)
        )
        if not path.exists():
            self.notify(
                f"{name}: no workspace yet (run a session with keep_workspace)",
                severity="warning",
            )
            return
        if self.cfg.ui.editor_command:
            try:
                tokens = launchers.fill_command(self.cfg.ui.editor_command, {"path": str(path)})
                if "{path}" not in self.cfg.ui.editor_command:
                    tokens.append(str(path))
                subprocess.Popen(
                    tokens,
                    cwd=path,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except (OSError, ValueError) as exc:
                self.notify(f"editor failed: {exc}", severity="error")
            return
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        try:
            with self._suspended(), terminal.preserved():
                subprocess.call([*shlex.split(editor), str(path)], cwd=path)
        except (OSError, ValueError) as exc:
            self.notify(f"editor failed: {exc}", severity="error")

    def action_preview(self) -> None:
        """P: last run details, history and the interactive command for each selected row."""
        chosen = self._action_branches()
        if not chosen:
            return
        d = resolve_launch_defaults(self.cfg, LastUsed.load(self.repo))
        sections = []
        for name in chosen:
            parts = []
            job = self.jobs.jobs.get(name)
            if job is not None:
                parts.append("\n".join(self._job_details(job)))
            session = self.sessions.get(name)
            if session is not None and (job is None or not job.running):
                started = _ts(session.started_at)
                parts.append(
                    f"busy (other): {session.agent} session {session.session_id}"
                    + (f" since {_clock(started)}" if started else "")
                )
            if history := self._history_lines(name):
                parts.append("history\n" + "\n".join(history))
            try:
                spec = LaunchSpec(branch=name, agent=d.agent, model=d.model, effort=d.effort)
                command = self._spec_preview(spec)
            except ConfigError as exc:
                command = f"! {exc}"
            parts.append(f"interactive command\n{command}")
            body = "\n\n".join(parts)
            sections.append(f"── {name} ──\n{body}" if len(chosen) > 1 else body)
        branch = self.branch(chosen[0])
        title = (
            (branch.label if branch is not None else chosen[0])
            if len(chosen) == 1
            else f"{len(chosen)} bookmarks"
        )
        self.push_screen(MessageScreen(title, "\n\n".join(sections)))

    def action_cleanup_workspaces(self) -> None:
        self._cleanup_workspaces()

    @work(exclusive=True, group="cleanup")
    async def _cleanup_workspaces(self) -> None:
        """List every workspace, let the user pick, then remove; removal re-checks each one."""
        try:
            found = await asyncio.to_thread(
                find_workspaces,
                self.backend,
                self.cfg,
                state_dir=self.jobs.state_dir,
                sessions_dir=self.sessions_dir,
            )
        except (VcsError, OSError) as exc:
            self.notify(f"cleanup failed: {exc}", severity="error", timeout=8)
            return
        if not found:
            body = f"No workspaces found besides {self.repo}."
            self.push_screen(MessageScreen("nothing to clean up", body))
            return
        chosen = await self.push_screen_wait(CleanupScreen(found))
        if isinstance(chosen, ForceRemove):
            await self._force_remove_workspace(chosen.workspace)
            return
        if not chosen:
            return
        try:
            async with self._history_lock:
                results, deleted = await asyncio.to_thread(self._remove_workspaces, chosen)
        except (VcsError, OSError) as exc:
            self.notify(f"cleanup failed: {exc}", severity="error", timeout=8)
            return
        removed = sum(line.startswith("removed") for line in results)
        done = [f"removed {removed} workspace{'' if removed == 1 else 's'}"] if removed else []
        if deleted:
            done.append(f"deleted {', '.join(deleted)} (u restores)")
        self.notify("; ".join(done) or "nothing removed")
        if any(line.startswith(("skipped", "kept")) for line in results):
            self.push_screen(MessageScreen("workspace cleanup", "\n".join(results)))
        await self.refresh_data()

    def _remove_workspaces(self, chosen: list[Workspace]) -> tuple[list[str], list[str]]:
        """Remove the chosen rows; the bookmarks they delete can be restored with u."""
        with self.graph.transaction() as state:
            commits: dict[str, str] = {}
            for ws in chosen:
                if ws.branch:
                    with contextlib.suppress(VcsError):  # removal rechecks and reports it
                        commits[ws.branch] = self.backend._bookmark_commit(ws.branch)
            results = remove_workspaces(
                self.backend,
                self.cfg,
                chosen,
                state_dir=self.jobs.state_dir,
                sessions_dir=self.sessions_dir,
            )
            existing = {b.name for b in self.backend.list_branches()} if commits else set()
            items = [
                UndoItem(name, state.current.pop(name, ""), commit)
                for name, commit in commits.items()
                if name not in existing
            ]
            if items:
                state.push_undo(UndoEntry("delete", items))
        return results, [item.bookmark for item in items]

    async def _force_remove_workspace(self, ws: Workspace) -> None:
        try:
            body = await asyncio.to_thread(force_removal_details, ws)
        except OSError as exc:
            self.notify(f"cannot list files for deletion: {exc}", severity="error", timeout=8)
            return
        if not await self.push_screen_wait(
            ConfirmScreen(f"force-delete {ws.name}?", body, "delete")
        ):
            return
        try:
            result = await asyncio.to_thread(
                force_remove_workspace,
                self.backend,
                self.cfg,
                ws,
                state_dir=self.jobs.state_dir,
                sessions_dir=self.sessions_dir,
            )
        except (VcsError, OSError) as exc:
            result = f"skipped {ws.name}: {exc}"
        removed = result.startswith("force-removed")
        self.notify(result, severity="information" if removed else "error", timeout=8)
        await self.refresh_data()

    def action_cancel(self) -> None:
        """x: cancel a run, harder on the second press, forget it on the third.

        Asks first before the first signal reaches several runs at once."""
        chosen = self._action_branches()
        fresh = [
            name
            for name in chosen
            if (job := self.jobs.jobs.get(name)) is not None
            and job.running
            and not self.jobs.cancel_started(name)
        ]
        if len(fresh) < 2:
            self._cancel(chosen)
            return

        def decided(confirmed: bool | None) -> None:
            if confirmed:
                self._cancel(chosen)

        body = (
            "\n".join(f"{name}  {self.jobs.jobs[name].spec.label()}" for name in fresh)
            + "\n\nEach run gets SIGINT, so project-sandbox can commit its work and close "
            "the session. Pressing x again escalates without asking."
        )
        self.push_screen(ConfirmScreen(f"cancel {len(fresh)} runs?", body, "cancel"), decided)

    def _cancel(self, names: list[str]) -> None:
        for name in names:
            self.notify(self.jobs.cancel(name, self.sessions))
        self._render_table()

    async def action_refresh(self) -> None:
        await self.refresh_data()

    def _review_project_config(self) -> bool:
        """Show a new or changed project config for review; reload once decided.

        Returns whether a review was opened.
        """
        try:
            trust = ProjectTrust.check(self.repo)
            if not trust.needs_review:
                return False
            load_candidate(trust, self.config_path)  # never offer what cannot load
        except ConfigError as exc:
            self.notify(f"Project config not trusted: {exc}", severity="error")
            return False

        def decided(trusted: bool | None) -> None:
            if not trusted:
                self.notify(trust.describe(), severity="warning", timeout=10)
            else:
                try:
                    trust.trust(self.repo)
                except ConfigError as exc:
                    self.notify(str(exc), severity="error")
            self.call_later(self._reload_config)

        body = (
            "This file comes with the repository. It can set commands that run on this "
            "machine (sandbox.command, ui.*_command) and loosen the sandbox (network, "
            f"credentials, extra_args). Trusting it copies it to {trusted_config_path(self.repo)}; "
            "later changes are shown here again before they take effect.\n\n" + trust.diff()
        )
        title = "trust the project config?" if trust.trusted is None else "project config changed"
        self.push_screen(ConfirmScreen(title, body, "trust"), decided)
        return True

    async def action_reload_config(self) -> None:
        """Reload both config files, keeping the current settings on error."""
        if not self._review_project_config():
            await self._reload_config()

    async def _reload_config(self) -> None:
        try:
            cfg = load(self.repo, self.config_path)
        except ConfigError as exc:
            self.notify(f"Config reload failed: {exc}", severity="error")
            return
        if self.dry_run:
            cfg.sandbox.extra_args.append("--dry-run")
        self.cfg = cfg
        self.jobs.cfg = cfg
        self.default_start = cfg.project.default_branch_start
        self._refresh_timer.stop()
        self._refresh_timer = self.set_interval(cfg.ui.refresh_seconds, self.refresh_data)
        self.show_cheatsheet(cfg.ui.cheatsheet)
        await self.refresh_data()
        self.notify("Configuration reloaded")

    def action_toggle_cheatsheet(self) -> None:
        self.show_cheatsheet(not self.cheatsheet_visible)

    def show_cheatsheet(self, visible: bool) -> None:
        """Show the sheet when it fits, otherwise keep the one-line help hint."""
        self.cheatsheet_visible = visible
        self.main.update_help_layout()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    async def action_quit(self) -> None:
        """Quit, asking first while runs or sessions are still going (q and ctrl+q)."""
        running = sorted(name for name, job in self.jobs.jobs.items() if job.running)
        if not running or self._confirming_quit:  # ctrl+q again over the dialog quits
            self.exit()
            return

        def decided(confirmed: bool | None) -> None:
            self._confirming_quit = False
            if confirmed:
                self.exit()

        body = (
            "\n".join(f"{name}  {self.jobs.jobs[name].spec.label()}" for name in running)
            + "\n\nThey keep running after sbx-tui exits. Completion notifications wait "
            "until sbx-tui is started again in this repository."
        )
        title = f"quit with {len(running)} still running?"
        self._confirming_quit = True
        self.push_screen(ConfirmScreen(title, body, "quit"), decided)
