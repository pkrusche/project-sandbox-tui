"""``=``: review changed files in each commit or in a direct comparison."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Static

from project_sandbox_tui.review import SeenFiles
from project_sandbox_tui.vcs import FileChange, FileChanges, ReviewCommit
from project_sandbox_tui.widgets import KeyHints, PrefixKeys

STATUS_STYLES = {"A": "green", "D": "red", "M": "yellow", "R": "cyan", "C": "cyan"}
COLUMNS = ("seen", "status", "file", "lines", "size")


def human_size(size: int) -> str:
    """``812 B``, ``4.2 KiB``, ``13 MiB``."""
    if size < 1024:
        return f"{size} B"
    value = size / 1024
    units = ["KiB", "MiB", "GiB"]
    while value >= 1024 and len(units) > 1:
        value /= 1024
        units.pop(0)
    return f"{value:.1f} {units[0]}" if value < 10 else f"{value:.0f} {units[0]}"


def lines_cell(change: FileChange) -> Text:
    if change.added is None or change.removed is None:
        return Text("binary", style="dim")
    if not (change.added or change.removed):
        return Text("·", style="dim")  # renamed, mode change or empty file
    cell = Text()
    if change.added:
        cell.append(f"+{change.added}", style="green")
    if change.removed:
        if cell:
            cell.append(" ")
        cell.append(f"−{change.removed}", style="red")
    return cell


def size_cell(change: FileChange) -> Text:
    if change.status == "D":
        return Text("deleted", style="dim")
    if change.size is None:
        return Text("?", style="dim")
    return Text(human_size(change.size), justify="right")


class DiffReviewScreen(Screen[None]):
    DEFAULT_CSS = """
    DiffReviewScreen #review-title { height: 1; background: $panel; padding: 0 1; }
    DiffReviewScreen DataTable { height: 1fr; }
    DiffReviewScreen #review-hint { height: auto; color: $text-disabled; padding: 0 1; }
    """

    BINDINGS = [
        Binding("q,escape,h", "back", "back", show=False),
        Binding("j", "cursor(1)", show=False),
        Binding("k", "cursor(-1)", show=False),
        Binding("ctrl+d", "page(1)", show=False),
        Binding("ctrl+u", "page(-1)", show=False),
        Binding("G", "bottom", show=False),
        Binding("g", "g_prefix", show=False),
        Binding("space", "toggle_seen", show=False),
        Binding("A", "toggle_all", show=False),
        Binding("l", "view_file", show=False),
        Binding("equals_sign", "view_unseen", show=False),
    ]

    HINTS = (
        "q back · j/k move · enter/l view file · space seen · "
        "= view unseen (all once seen) · A all seen / unseen"
    )

    def __init__(
        self,
        title: str,
        changes: FileChanges,
        seen: SeenFiles,
        view: Callable[[Sequence[FileChange]], None],
    ) -> None:
        super().__init__()
        self.title_text = title
        self.files = changes.files
        self.seen_files = seen
        self.seen = seen.seen() & {f.key for f in self.files}
        self.view = view
        self._prefix = PrefixKeys()

    def compose(self) -> ComposeResult:
        yield Static("", id="review-title")
        table: DataTable[Text] = DataTable(cursor_type="row", zebra_stripes=False)
        yield table
        yield Static(KeyHints(self.HINTS), id="review-hint")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        for key, label in zip(COLUMNS, ("", "", "file", "lines", "size"), strict=True):
            table.add_column(label, key=key)
        for index, change in enumerate(self.files):
            table.add_row(*self._cells(change), key=str(index))
        self._title()
        table.focus()

    def on_key(self, event: events.Key) -> None:
        self._prefix.other_key(event.key)

    def action_back(self) -> None:
        self.dismiss(None)

    def _cells(self, change: FileChange) -> tuple[Text, ...]:
        seen = change.key in self.seen
        return (
            Text("✓", style="green") if seen else Text("●", style="bold"),
            Text(change.status, style=STATUS_STYLES.get(change.status, "")),
            Text(change.display, style="dim" if seen else ""),
            lines_cell(change),
            size_cell(change),
        )

    def _title(self) -> None:
        added = sum(f.added or 0 for f in self.files)
        removed = sum(f.removed or 0 for f in self.files)
        files = len(self.files)
        title = Text()
        title.append(f"diff {self.title_text}  ", style="bold")
        title.append(f"{files} file{'' if files == 1 else 's'} ")
        title.append(f"+{added}", style="green")
        title.append(" ")
        title.append(f"−{removed}", style="red")
        title.append(f"  ·  {len(self.seen)}/{files} seen", style="dim")
        self.query_one("#review-title", Static).update(title)

    @property
    def current_row(self) -> int | None:
        row = self.query_one(DataTable).cursor_row
        return row if 0 <= row < len(self.files) else None

    def _mark(self, rows: Sequence[int], seen: bool) -> None:
        keys = [self.files[row].key for row in rows]
        if seen:
            self.seen.update(keys)
        else:
            self.seen.difference_update(keys)
        try:
            self.seen_files.mark(keys, seen)
        except OSError as exc:
            self.notify(f"could not save seen files: {exc}", severity="error", markup=False)
        table = self.query_one(DataTable)
        for row in rows:
            for column, cell in zip(COLUMNS, self._cells(self.files[row]), strict=True):
                table.update_cell(str(row), column, cell)
        self._title()

    def action_toggle_seen(self) -> None:
        row = self.current_row
        if row is not None:
            self._mark([row], self.files[row].key not in self.seen)
            self.action_cursor(1)  # like marking bookmarks: mark and move on

    def action_toggle_all(self) -> None:
        self._mark(range(len(self.files)), len(self.seen) < len(self.files))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()  # the app opens a bookmark's log on the list's own RowSelected
        self.action_view_file()

    def action_view_file(self) -> None:
        row = self.current_row
        if row is not None:
            self.view([self.files[row]])

    def action_view_unseen(self) -> None:
        if self.files:
            self.view([f for f in self.files if f.key not in self.seen] or self.files)

    def action_cursor(self, step: int) -> None:
        table = self.query_one(DataTable)
        if self.files:
            table.move_cursor(row=min(max(table.cursor_row + step, 0), len(self.files) - 1))

    def action_page(self, direction: int) -> None:
        self.action_cursor(direction * max(1, self.query_one(DataTable).size.height // 2))

    def action_bottom(self) -> None:
        self.action_cursor(len(self.files))

    def action_g_prefix(self) -> None:
        if self._prefix.press("g"):
            self.action_cursor(-len(self.files))


class CommitReviewScreen(DiffReviewScreen):
    """The file review view, repeated for each commit from main to a bookmark."""

    DEFAULT_CSS = """
    CommitReviewScreen #review-commit { height: 1; background: $panel; padding: 0 1; }
    """

    BINDINGS = [
        Binding("left_square_bracket", "commit(-1)", show=False),
        Binding("right_square_bracket", "commit(1)", show=False),
    ]

    HINTS = "[ / ] previous / next commit · " + DiffReviewScreen.HINTS

    def __init__(
        self,
        title: str,
        commits: Sequence[ReviewCommit],
        changes: Sequence[FileChanges],
        seen: SeenFiles,
        view: Callable[[ReviewCommit, Sequence[FileChange]], None],
    ) -> None:
        self.commits = commits
        self.changes = changes
        self.commit_index = 0
        super().__init__(title, changes[0], seen, lambda files: view(self.current_commit, files))

    @property
    def current_commit(self) -> ReviewCommit:
        return self.commits[self.commit_index]

    def compose(self) -> ComposeResult:
        yield Static("", id="review-commit")
        yield from super().compose()

    def _title(self) -> None:
        super()._title()
        commit = self.current_commit
        title = Text(f"commit {self.commit_index + 1}/{len(self.commits)}  ", style="bold")
        title.append(commit.commit[:12], style="cyan")
        title.append(f"  {commit.subject or '(no description set)'}", style="")
        self.query_one("#review-commit", Static).update(title)

    def action_commit(self, step: int) -> None:
        index = min(max(self.commit_index + step, 0), len(self.commits) - 1)
        if index == self.commit_index:
            return
        self.commit_index = index
        self.files = self.changes[index].files
        self.seen = self.seen_files.seen() & {f.key for f in self.files}
        table = self.query_one(DataTable)
        table.clear()
        for row, change in enumerate(self.files):
            table.add_row(*self._cells(change), key=str(row))
        table.move_cursor(row=0)
        self._title()
