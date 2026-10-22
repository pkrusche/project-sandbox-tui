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
from typing import ClassVar

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
    force_remove_workspace,
    remove_workspaces,
)
from project_sandbox_tui.config import (
    Config,
    ConfigError,
    LastUsed,
    load,
    remember,
    resolve_launch_defaults,
)
from project_sandbox_tui.history import PromptHistory
from project_sandbox_tui.jobs import BusyError, JobManager, JobRecord, _parse_ts
from project_sandbox_tui.sandbox import (
    CheckoutConflict,
    LaunchSpec,
    Mode,
    SessionRecord,
    build_argv,
    read_sessions,
    running_by_branch,
    uses_main_checkout,
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
    Backend,
    Branch,
    VcsError,
    sort_branches,
    workspace_path,
)
from project_sandbox_tui.widgets import modifier_key

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


def count_label(count: int, kind: str) -> str:
    """'1 branch', '3 bookmarks', 'no bookmarks' (the working-copy row is not counted)."""
    singular, plural = ("bookmark", "bookmarks") if kind == "jj" else ("branch", "branches")
    if count == 0:
        return f"no {plural}"
    return f"{count} {singular if count == 1 else plural}"


def _ts(value: str) -> float:
    return _parse_ts(value)


@dataclass
class Row:
    branch: Branch
    job: JobRecord | None
    session: SessionRecord | None

    def name_cell(self, selected: bool = False) -> Text:
        b = self.branch
        if selected:
            return Text(f"+ {b.label}", style="bold magenta")
        name = Text(b.label, style="bold" if b.is_current else "")
        if b.is_current:
            name.append(" ●", style="green")
        return name

    def cells(self, now: float, selected: bool = False) -> tuple[Text, ...]:
        b, job, sess = self.branch, self.job, self.session
        glyph, colour, status, agent, when = " ", "", "", "", ""
        if job is not None:
            glyph, colour = GLYPHS.get(job.status, ("?", ""))
            agent = job.spec.label()
            if job.running:
                status = "interactive" if job.mode == "interactive" else "running"
                if job.external:
                    status = "interactive (ext)"
                if job.spec.operation:  # an integration of several branches
                    verb = "merging" if job.spec.operation == "merge" else "rebasing"
                    status = verb if job.mode == "background" else f"{verb} ({status})"
                when = human_age(now - _ts(job.started_at))
            else:
                status = job.status.replace("_", " ")
                if job.exit_code is not None and job.status != "completed":
                    status += f" ({job.exit_code})"
                when = human_age(now - _ts(job.ended_at)) + " ago" if job.ended_at else ""
        if sess is not None and (job is None or not job.running):
            glyph, colour = GLYPHS["external"]
            status, agent = "busy (other)", sess.agent
            when = human_age(now - _ts(sess.started_at))
        return (
            Text(glyph, style=colour),
            self.name_cell(selected),
            Text(b.head, style="dim"),
            Text(b.subject, overflow="ellipsis", no_wrap=True),
            Text(status, style=colour),
            Text(agent, style="dim"),
            Text(when, style="dim"),
        )


MAIN_BINDINGS: list[BindingType] = [
    Binding("j", "app.cursor(1)", show=False),
    Binding("k", "app.cursor(-1)", show=False),
    Binding("ctrl+d", "app.page(1)", show=False),
    Binding("ctrl+u", "app.page(-1)", show=False),
    Binding("G", "app.bottom", show=False),
    Binding("g", "app.g_prefix", show=False),
    Binding("slash", "app.filter", show=False),
    Binding("v", "app.visual", show=False),
    Binding("space", "app.toggle_mark", show=False),
    Binding("escape", "app.clear_filter", show=False),
    Binding("i", "app.launch('interactive')", show=False),
    Binding("r", "app.launch('background')", show=False),
    Binding("n", "app.new_branch", show=False),
    Binding("l", "app.open_log", show=False),
    Binding("o", "app.open_editor", show=False),
    Binding("P", "app.preview", show=False),
    Binding("W", "app.cleanup_workspaces", show=False),
    Binding("x", "app.stop", show=False),
    Binding("c", "app.clear_status", show=False),
    Binding("C", "app.clear_status(True)", show=False),
    Binding("R", "app.refresh", show=False),
    Binding("ctrl+r", "app.reload_config", show=False),
    Binding("question_mark", "app.help", show=False),
    Binding("H", "app.toggle_cheatsheet", show=False),
    Binding("q", "app.quit", show=False),
]


CHEATSHEET: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "move",
        (("j k", "down / up"), ("gg G", "top / bottom"), ("^d ^u", "half page"), ("/", "filter")),
    ),
    (
        "sessions",
        (
            ("i", "interactive"),
            ("r", "unsupervised run"),
            ("x", "stop (again: escalate)"),
            ("l ⏎", "log / transcript"),
        ),
    ),
    (
        "branches",
        (
            ("n", "new branch"),
            ("v ␣", "select → merge/rebase"),
            ("o", "open in editor"),
            ("P", "show command"),
            ("W", "clean workspaces"),
            ("R", "refresh"),
        ),
    ),
    (
        "other",
        (
            ("c C", "clear / forget"),
            ("^r", "reload config"),
            ("H", "hide this sheet"),
            ("?", "full help"),
            ("q", "quit"),
        ),
    ),
)


def cheatsheet() -> Table:
    """The key cheat-sheet shown under the branch list."""
    grid = Table.grid(padding=(0, 3), expand=False)
    for _ in CHEATSHEET:
        grid.add_column(no_wrap=True)
    grid.add_row(*(Text(title, style="bold") for title, _keys in CHEATSHEET))
    for i in range(max(len(keys) for _title, keys in CHEATSHEET)):
        cells = []
        for _title, keys in CHEATSHEET:
            cell = Text()
            if i < len(keys):
                key, what = keys[i]
                cell.append(f"{key:<6}", style="bold")
                cell.append(what, style="dim")
            cells.append(cell)
        grid.add_row(*cells)
    return grid


class MainScreen(Screen[None]):
    """Branch list. Bindings live here so they are inactive under modal forms."""

    BINDINGS: ClassVar[list[BindingType]] = MAIN_BINDINGS

    def compose(self) -> ComposeResult:
        yield Static("", id="header")
        table: DataTable[Text] = DataTable(cursor_type="row", zebra_stripes=False)
        yield table
        yield Static(cheatsheet(), id="cheatsheet")
        yield Input(placeholder="filter", id="filter")
        yield Static(
            "j/k move · i interactive · r run · n new · l log · x stop · o open · ? help · q quit",
            id="footer",
        )

    def on_key(self, event: events.Key) -> None:
        modifier = modifier_key(event.key)
        if modifier:
            hints = {
                "ctrl": "ctrl+d/u half page down/up · ctrl+r reload configuration",
                "shift": "G bottom · P preview · R refresh · C force-forget · H toggle sheet",
            }
            hint = hints.get(modifier, f"{modifier}: no shortcuts on the branch list")
            self.query_one("#cheatsheet", Static).update(hint)
            self.query_one("#footer", Static).update(hint)
            event.stop()
        else:
            self.query_one("#cheatsheet", Static).update(cheatsheet())
            self.query_one("#footer", Static).update(
                "j/k move · i interactive · r run · n new · l log · "
                "x stop · o open · ? help · q quit"
            )


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
        backend: Backend,
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
        self._g_pending = False
        self._refreshing = False
        self._refresh_again = False
        self._pending_select: str | None = None
        self.default_start = cfg.project.default_branch_start
        self.main = MainScreen()
        self.cheatsheet_visible = cfg.ui.cheatsheet
        self.prompt_history = PromptHistory.for_repo(repo)
        # Branch selection for merging / rebasing several branches: space marks
        # rows (kept in marking order), v starts a visual range at the cursor.
        self.marked: list[str] = []
        self._visual_anchor: str | None = None

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
            ("branch", None),
            ("head", 8),
            ("subject", None),
            ("status", None),
            ("agent", None),
            ("when", 8),
        ):
            table.add_column(label, width=width, key=label or "glyph")
        table.focus()
        await self.refresh_data()
        self._refresh_timer = self.set_interval(self.cfg.ui.refresh_seconds, self.refresh_data)

    # ------------------------------------------------------------ data

    @property
    def worktree_dir(self) -> Path | None:
        wd = self.cfg.project.worktree_dir
        return self.cfg.resolve_path(wd, self.repo) if wd else None

    async def refresh_data(self) -> None:
        if self._refreshing:
            self._refresh_again = True  # coalesce: rerun once the current pass ends
            return
        self._refreshing = True
        try:
            await self._refresh_once()
            while self._refresh_again:
                self._refresh_again = False
                await self._refresh_once()
        finally:
            self._refreshing = False

    async def _refresh_once(self) -> None:
        try:
            branches = await asyncio.to_thread(self.backend.list_branches)
        except VcsError as exc:
            self.notify(str(exc), severity="error", timeout=8)
            branches = self.branches
        self.branches = sort_branches(branches)
        records = await asyncio.to_thread(read_sessions, self.sessions_dir)
        self.sessions = running_by_branch(
            records,
            self.repo,
            self.backend.kind,
            [b.name for b in self.branches],
            self.worktree_dir,
            main_branch=self.main_checkout_branch(),
        )
        for job in self.jobs.refresh(self.sessions):
            self._announce(job)
        # NoMatches: the app is shutting down (e.g. a worker finishing after quit)
        with contextlib.suppress(NoMatches):
            self._render_table()

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

    def visible_rows(self) -> list[Row]:
        rows = []
        needle = self.filter_text.lower()
        for b in self.branches:
            if needle and needle not in b.name.lower():
                continue
            rows.append(Row(b, self.jobs.jobs.get(b.name), self.sessions.get(b.name)))
        return rows

    def _render_table(self) -> None:
        table = self.table
        selected = self._pending_select or self.selected_branch_name()
        rows = self.visible_rows()
        now = time.time()
        chosen = set(self.selection())  # before clear(): the visual range follows the cursor
        # Fixed width with room for the "+ " / " ●" markers, so marking a row in
        # place (update_cell) never has to widen the column.
        name_col = table.columns.get(ColumnKey("branch"))
        if name_col is not None:
            name_col.auto_width = False
            name_col.width = max([len("branch"), *(len(r.branch.label) + 2 for r in rows)])
        table.clear()
        for row in rows:
            table.add_row(*row.cells(now, row.branch.name in chosen), key=row.branch.name)
        if selected is not None:
            names = [r.branch.name for r in rows]
            if selected in names:
                table.move_cursor(row=names.index(selected), animate=False)
                if selected == self._pending_select:
                    self._pending_select = None
        self._render_header(chosen)

    def _render_header(self, chosen: set[str]) -> None:
        running = self.jobs.running_count()
        busy_other = sum(
            1 for n in self.sessions if not (n in self.jobs.jobs and self.jobs.jobs[n].running)
        )
        header = Text()
        header.append(" sbx-tui ", style="bold reverse")
        header.append(f"  {self.repo}", style="bold")
        header.append(f"  ({self.backend.kind})", style="dim")
        count = sum(1 for b in self.branches if not b.working_copy)
        stats = f"{count_label(count, self.backend.kind)} · {running} running"
        if busy_other:
            stats += f" · {busy_other} external"
        if self.filter_text:
            stats += f" · filter: {self.filter_text}"
        if self._visual_anchor is not None:
            stats += " · VISUAL"
        if chosen:
            stats += f" · {len(chosen)} selected"
        header.append(f"   {stats}", style="dim")
        self.main.query_one("#header", Static).update(header)

    def main_checkout_branch(self) -> str | None:
        """The branch whose sessions run in the repository itself, if any.

        git: the branch checked out there. jj: the working-copy row.
        """
        for b in self.branches:
            if b.working_copy:
                return b.name
        if self.backend.kind != "git":
            return None
        repo = self.repo.resolve()
        for b in self.branches:
            if b.checkout_path is not None and b.checkout_path.resolve() == repo:
                return b.name
        return None

    def branch(self, name: str) -> Branch | None:
        return next((b for b in self.branches if b.name == name), None)

    def _main_checkout(self, name: str) -> bool:
        """Whether a session on ``name`` runs in the main checkout (raises on conflict)."""
        branch = self.branch(name)
        if branch is None:
            return False
        return uses_main_checkout(self.repo, branch, self.worktree_dir)

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
                    row.branch.name, "branch", row.name_cell(row.branch.name in chosen)
                )
            except Exception:
                return  # table being rebuilt; the next render shows the marks
        self._render_header(chosen)

    def action_visual(self) -> None:
        """v: start a range at the cursor; v again keeps the range as marks."""
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
        if self._g_pending:
            self._g_pending = False
            self.table.move_cursor(row=0, animate=False)
        else:
            self._g_pending = True
            self.set_timer(0.8, self._reset_g)

    def _reset_g(self) -> None:
        self._g_pending = False

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
            self.notify("no branch selected", severity="warning")
        return name

    def action_launch(self, mode: Mode) -> None:
        selected = self.selection()
        if selected:
            self._launch_octopus(mode, selected)
            return
        name = self._require_branch()
        if name is None:
            return
        reason = self.jobs.busy_reason(name, self.sessions)
        if reason:
            self.notify(reason, severity="warning")
            return
        try:
            main_checkout = self._main_checkout(name)
        except CheckoutConflict as exc:
            self.notify(str(exc), severity="error", timeout=8)
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
            main_checkout=main_checkout,
            branch_label=branch.label if branch is not None else name,
            opens_in=launchers.describe(self.cfg.ui.terminal_mode())
            if mode == "interactive"
            else "",
            prompt_history=self.prompt_history,
            branches=[b.name for b in self.branches],
            main_checkout_for=self._main_checkout,
        )
        known_branches = {b.name for b in self.branches}

        def chosen(spec: LaunchSpec | None) -> None:
            start = name if spec is not None and spec.branch not in known_branches else None
            self._launch_chosen(spec, new_branch_start=start)

        self.push_screen(screen, chosen)

    def _launch_octopus(self, mode: Mode, chosen: list[str]) -> None:
        """r / i with branches selected: merge or rebase them into a new branch."""
        if len(chosen) < 2:
            self.notify(
                "select at least two branches to merge or rebase (esc clears the selection)",
                severity="warning",
            )
            return
        kind = self.backend.kind
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
            kind=kind,
            existing={b.name for b in self.branches if not b.working_copy},
            default_base=default_base,
            templates={
                op: overrides[op] or octopus.DEFAULT_TEMPLATES[(op, kind)]
                for op in octopus.OPERATIONS
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
        )
        self.push_screen(screen, self._launch_chosen)

    def _launch_chosen(
        self, spec: LaunchSpec | None, *, new_branch_start: str | None = None
    ) -> None:
        if spec is None:
            return
        # Start only once the form is gone and its key event is fully processed:
        # dismiss() runs this callback before the screen has been popped, and an
        # interactive session blocks the event loop until the agent exits.
        if spec.operation:
            self.clear_branch_selection()
            self.call_after_refresh(self._integrate, spec)
            return
        self.call_after_refresh(self._start_launch, spec, new_branch_start=new_branch_start)

    @work(exclusive=True, group="octopus")
    async def _integrate(self, spec: LaunchSpec) -> None:
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
            self.notify(f"{spec.branch}: preparing {operation} of {len(spec.inputs)} branches…")
            state = await asyncio.to_thread(
                octopus.prepare, self.backend.kind, self.repo, plan, self.worktree_dir
            )
        except (octopus.OctopusError, BusyError, VcsError) as exc:
            self.notify(f"{spec.branch}: {exc}", severity="error", timeout=10)
            await self.refresh_data()
            return
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
        self._start_launch(spec, history_text=typed, banner=state.report())

    def _start_launch(
        self,
        spec: LaunchSpec,
        *,
        new_branch_start: str | None = None,
        history_text: str | None = None,
        banner: str = "",
    ) -> None:
        try:
            # The checkout and external sessions may have changed while the
            # launch form was open. Recheck immediately before starting.
            self.branches = sort_branches(self.backend.list_branches())
            if new_branch_start is not None:
                self.backend.create_branch(spec.branch, new_branch_start)
                self.branches = sort_branches(self.backend.list_branches())
                self._pending_select = spec.branch
            if self.branch(spec.branch) is None:
                raise VcsError(f"{spec.branch} no longer exists")
            spec.main_checkout = self._main_checkout(spec.branch)
            self.sessions = running_by_branch(
                read_sessions(self.sessions_dir),
                self.repo,
                self.backend.kind,
                [b.name for b in self.branches],
                self.worktree_dir,
                main_branch=self.main_checkout_branch(),
            )
            if spec.mode == "background":
                job = self.jobs.start_background(spec, self.sessions)
                self.notify(f"{spec.branch}: started {spec.label()} (pid {job.pid})")
            elif self.cfg.ui.terminal_mode() != "inline":
                self._launch_external(spec)
            else:
                self._launch_interactive(spec, banner)
        except (BusyError, ConfigError, CheckoutConflict, VcsError, launchers.LaunchError) as exc:
            self.notify(str(exc), severity="error", timeout=8)
        except OSError as exc:
            self.notify(f"could not start project-sandbox: {exc}", severity="error", timeout=8)
        else:
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
        with self.jobs.interactive(spec, self.sessions) as job, self._suspended():
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
        self.notify(f"{spec.branch}: {launch.message}", timeout=8)

    def action_new_branch(self) -> None:
        start = self.default_start
        if not start:
            try:
                start = self.backend.default_start()
            except VcsError:
                start = ""
        self.push_screen(NewBranchScreen(start, self.backend.kind), self._new_branch_chosen)

    def _new_branch_chosen(self, result: tuple[str, str] | None) -> None:
        self._create_branch(result)

    @work(exclusive=True, group="vcs")
    async def _create_branch(self, result: tuple[str, str] | None) -> None:
        if result is None:
            return
        name, start = result
        try:
            await asyncio.to_thread(self.backend.create_branch, name, start)
        except VcsError as exc:
            self.notify(f"could not create {name}: {exc}", severity="error", timeout=8)
            return
        self.notify(f"created {name} at {start}")
        self._pending_select = name  # applied by whichever refresh sees it first
        await self.refresh_data()

    def action_open_log(self) -> None:
        name = self._require_branch()
        if name is None:
            return
        job = self.jobs.jobs.get(name)
        if job is None:
            self.notify(f"{name}: no runs yet", severity="information")
            return
        self.push_screen(LogScreen(job))

    def action_open_editor(self) -> None:
        name = self._require_branch()
        if name is None:
            return
        path = workspace_path(self.repo, self.backend.kind, name, self.worktree_dir)
        branch = self.branch(name)
        if branch is not None and branch.checkout_path is not None:
            path = branch.checkout_path  # git: wherever the branch is checked out
        if not path.exists():
            if branch is not None and branch.is_current:
                path = self.repo
            else:
                self.notify(
                    f"{name}: no worktree yet (run a session with keep_workspace)",
                    severity="warning",
                )
                return
        if self.cfg.ui.editor_command:
            tokens = [
                t.replace("{path}", str(path)) for t in shlex.split(self.cfg.ui.editor_command)
            ]
            if not any("{path}" in t for t in shlex.split(self.cfg.ui.editor_command)):
                tokens.append(str(path))
            try:
                subprocess.Popen(
                    tokens,
                    cwd=path,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as exc:
                self.notify(f"editor failed: {exc}", severity="error")
            return
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        try:
            with self._suspended(), terminal.preserved():
                subprocess.call([*shlex.split(editor), str(path)], cwd=path)
        except (OSError, ValueError) as exc:
            self.notify(f"editor failed: {exc}", severity="error")

    def action_preview(self) -> None:
        name = self._require_branch()
        if name is None:
            return
        d = resolve_launch_defaults(self.cfg, LastUsed.load(self.repo))
        try:
            spec = LaunchSpec(
                branch=name,
                agent=d.agent,
                model=d.model,
                effort=d.effort,
                main_checkout=self._main_checkout(name),
            )
            body = self._spec_preview(spec)
        except (ConfigError, CheckoutConflict) as exc:
            body = f"! {exc}"
        self.push_screen(MessageScreen(f"interactive command for {name}", body))

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
            noun = "worktrees" if self.backend.kind == "git" else "workspaces"
            body = f"No {noun} found besides {self.repo}."
            self.push_screen(MessageScreen("nothing to clean up", body))
            return
        chosen = await self.push_screen_wait(CleanupScreen(found))
        if isinstance(chosen, ForceRemove):
            await self._force_remove_workspace(chosen.workspace)
            return
        if not chosen:
            return
        try:
            results = await asyncio.to_thread(
                remove_workspaces,
                self.backend,
                self.cfg,
                chosen,
                state_dir=self.jobs.state_dir,
                sessions_dir=self.sessions_dir,
            )
        except (VcsError, OSError) as exc:
            self.notify(f"cleanup failed: {exc}", severity="error", timeout=8)
            return
        removed = sum(line.startswith("removed") for line in results)
        self.notify(f"removed {removed} workspace{'' if removed == 1 else 's'}")
        if removed != len(results):
            self.push_screen(MessageScreen("workspace cleanup", "\n".join(results)))
        await self.refresh_data()

    async def _force_remove_workspace(self, ws: Workspace) -> None:
        noun = "worktree" if self.backend.kind == "git" else "workspace"
        body = (
            f"{ws.name}: {ws.path}\nkept because: {ws.status}\n\n"
            f"Deletes the directory with everything in it, including uncommitted,\n"
            f"untracked and ignored files, and unregisters the {noun}.\n"
            f"Branches / bookmarks are kept."
        )
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

    def action_stop(self) -> None:
        name = self._require_branch()
        if name is None:
            return
        self.notify(self.jobs.stop(name, self.sessions))

    def action_clear_status(self, force: bool = False) -> None:
        name = self._require_branch()
        if name is None:
            return
        if not self.jobs.clear(name, force=force):
            self.notify(
                f"{name} is still running (C forgets the record without stopping it)",
                severity="warning",
            )
        elif force:
            self.notify(f"{name}: record cleared (no process was signalled)")
        self._render_table()

    async def action_refresh(self) -> None:
        await self.refresh_data()

    async def action_reload_config(self) -> None:
        """Reload both config files, keeping the current settings on error."""
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
        """The sheet replaces the one-line footer while it is shown."""
        self.cheatsheet_visible = visible
        self.main.query_one("#cheatsheet").set_class(not visible, "hidden")
        self.main.set_class(visible, "with-cheatsheet")

    def action_help(self) -> None:
        self.push_screen(HelpScreen())
