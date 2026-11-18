"""Tail view of a job's transcript, agent log and captured output, and of the
output of ``:*`` tasks run in the bookmark's workspace."""

from __future__ import annotations

import codecs
import json
import os
from pathlib import Path
from typing import Protocol

from rich.segment import Segment
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.geometry import Region, Size
from textual.reactive import reactive
from textual.screen import Screen
from textual.strip import Strip
from textual.widgets import Log, Static

from project_sandbox_tui.jobs import JobRecord
from project_sandbox_tui.screens.dialogs import MessageScreen
from project_sandbox_tui.widgets import KeyHints, PrefixKeys

INITIAL_TAIL_BYTES = 256 * 1024
MAX_LINES = 10_000


class LogSource(Protocol):
    """A sandbox run (``JobRecord``) or a ``:*`` task (``TaskRecord``)."""

    @property
    def branch(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def exit_code(self) -> int | None: ...

    def output_files(self) -> list[Path]: ...


class LiveLog(Log):
    """Tail widget with a highlighted row when viewing agent records."""

    COMPONENT_CLASSES = {"live-log--cursor"}
    DEFAULT_CSS = """
    LiveLog > .live-log--cursor { background: $accent 25%; }
    """

    cursor_row: reactive[int | None] = reactive(None)

    def render_line(self, y: int) -> Strip:
        line = super().render_line(y)
        if self.cursor_row == y + self.scroll_offset.y:
            style = self.get_component_rich_style("live-log--cursor")
            return Strip(Segment.apply_style(line, post_style=style), line.cell_length)
        return line


class LogScreen(Screen[None]):
    DEFAULT_CSS = """
    LogScreen #log-title { height: 1; background: $panel; padding: 0 1; }
    LogScreen Log { height: 1fr; border: none; }
    LogScreen #log-hint { height: auto; color: $text-disabled; padding: 0 1; }
    """

    BINDINGS = [
        Binding("q,escape,h", "app.pop_screen", "back", show=False),
        Binding("tab", "next_file(1)", "next file", show=False),
        Binding("shift+tab", "next_file(-1)", "previous file", show=False),
        Binding("v", "toggle_output", "switch stdout / agent log", show=False),
        Binding("enter", "inspect_record", "inspect JSON record", show=False),
        Binding("j", "move(1)", show=False),
        Binding("k", "move(-1)", show=False),
        Binding("ctrl+d", "page(0.5)", show=False),
        Binding("ctrl+u", "page(-0.5)", show=False),
        Binding("ctrl+f", "page(1)", show=False),
        Binding("ctrl+b", "page(-1)", show=False),
        Binding("G", "bottom", show=False),
        Binding("g", "g_prefix", show=False),
        Binding("f", "toggle_follow", show=False),
    ]

    def __init__(self, *sources: LogSource, start: int = 0) -> None:
        """Show the files of each source in turn, starting with source ``start``."""
        super().__init__()
        self._owners = [(source, path) for source in sources for path in source.output_files()]
        self.job = sources[start] if sources else None
        owned = [i for i, (source, _) in enumerate(self._owners) if source is self.job]
        candidates = owned or range(len(self._owners))
        self._idx = next(
            (i for i in candidates if self._owners[i][1].is_file()), next(iter(candidates), 0)
        )
        if isinstance(self.job, JobRecord) and self.job.running and self.job.spec.agent != "bash":
            stdout = Path(self.job.stdout_path)
            self._idx = next(
                (i for i in owned if self._owners[i][1] == stdout and stdout.is_file()), self._idx
            )
        self._pos = 0
        self._follow = True
        self._prefix = PrefixKeys()
        self._missing = False
        self._file_id: tuple[int, int] | None = None
        self._skip_partial = False
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def compose(self) -> ComposeResult:
        yield Static("", id="log-title")
        yield LiveLog(max_lines=MAX_LINES)
        yield Static("", id="log-hint")

    def on_mount(self) -> None:
        self._load()
        self.set_interval(1.0, self._poll)
        self.watch(self.query_one(LiveLog), "scroll_y", self._scrolled, init=False)

    def on_key(self, event: events.Key) -> None:
        self._prefix.other_key(event.key)

    def on_click(self, event: events.Click) -> None:
        log = self.query_one(LiveLog)
        if log.cursor_row is None or event.widget is not log:
            return
        offset = event.get_content_offset(log)
        if offset is None:
            return
        row = offset.y + log.scroll_offset.y
        if row < log.line_count:
            self._set_follow(False)
            log.cursor_row = row
            if event.chain == 2:
                self.action_inspect_record()

    def _scrolled(self, _old: float, new: float) -> None:
        """Like ``less +F``: scrolling away from the end pauses following, any
        scrolling (keys, mouse) back to the end resumes it."""
        log = self.query_one(LiveLog)
        follow = new >= log.max_scroll_y
        if follow != self._follow:
            self._follow = follow
            log.auto_scroll = follow
            self._title()
        self._sync_cursor(log)

    @property
    def current(self) -> Path | None:
        return self._owners[self._idx][1] if self._owners else None

    @property
    def _source(self) -> LogSource | None:
        return self._owners[self._idx][0] if self._owners else self.job

    @property
    def _raw_log(self) -> bool:
        source = self._source
        return (
            isinstance(source, JobRecord)
            and source.spec.agent != "bash"
            and self.current == Path(source.log_path)
        )

    def _sync_cursor(self, log: LiveLog) -> None:
        if not self._raw_log or self._file_id is None or not log.line_count:
            log.cursor_row = None
        elif self._follow:
            log.cursor_row = log.line_count - 1
        else:
            top = min(log.scroll_offset.y, log.line_count - 1)
            bottom = min(top + max(1, log.scrollable_content_region.height) - 1, log.line_count - 1)
            row = log.cursor_row if log.cursor_row is not None else top
            log.cursor_row = min(max(row, top), bottom)

    def _title(self) -> None:
        path = self.current
        name = path.name if path else "(no output files)"
        source = self._source
        if isinstance(source, JobRecord):
            if path == Path(source.stdout_path):
                name = f"stdout ({name})"
            elif path == Path(source.log_path):
                name = f"agent log ({name})"
        state = ""
        if source is not None:
            state = source.status + (
                f" · exit {source.exit_code}" if source.exit_code is not None else ""
            )
        if isinstance(source, JobRecord) and source.feedback_path:
            state += " · " + source.feedback_detail()
        follow = "follow" if self._follow else "paused"
        title = Text()
        title.append(f"{source.branch if source else ''}  ", style="bold")
        title.append(f"[{self._idx + 1}/{len(self._owners)}] {name}  ", style="")
        title.append(f"{state} · {follow}", style="dim")
        self.query_one("#log-title", Static).update(title)

    def _load(self) -> None:
        log = self.query_one(LiveLog)
        log.clear()
        log.cursor_row = None
        self._pos = 0
        self._file_id = None
        self._skip_partial = False
        self._decoder.reset()
        path = self.current
        self._missing = path is not None and not path.is_file()
        movement = "j/k select · enter inspect" if self._raw_log else "j/k scroll"
        source = self._source
        switch = (
            " · v stdout/agent log"
            if isinstance(source, JobRecord) and (source.stdout_path and source.log_path)
            else ""
        )
        self.query_one("#log-hint", Static).update(
            KeyHints(
                f"q back · tab/shift+tab file{switch} · {movement} · ^d/^u scroll"
                " · gg/G top/bottom · f follow"
            )
        )
        self._title()
        if path is None:
            log.write_line("This job has no log files (interactive sessions log to your terminal).")
            return
        if self._missing:
            log.write_line(f"{path} has not been written yet.")
            return
        try:
            with path.open("rb") as fh:
                stat = os.fstat(fh.fileno())
                if stat.st_size > INITIAL_TAIL_BYTES:
                    self._pos = stat.st_size - INITIAL_TAIL_BYTES
                    if self._raw_log:
                        fh.seek(self._pos - 1)
                        self._skip_partial = fh.read(1) != b"\n"
                    log.write(f"… showing last {INITIAL_TAIL_BYTES // 1024} KiB …\n")
        except OSError:
            self._missing = True
            return
        self._file_id = (stat.st_dev, stat.st_ino)
        self._append()

    def _append(self) -> None:
        path = self.current
        if path is None:
            return
        try:
            with path.open("rb") as fh:
                stat = os.fstat(fh.fileno())
                if (stat.st_dev, stat.st_ino) != self._file_id or stat.st_size < self._pos:
                    self._load()
                    return
                fh.seek(self._pos)
                chunk = fh.read(INITIAL_TAIL_BYTES)
        except OSError:
            self._missing = True
            return
        if not chunk:
            return
        self._pos += len(chunk)
        if self._skip_partial:
            _, newline, chunk = chunk.partition(b"\n")
            self._skip_partial = not newline
        data = self._decoder.decode(chunk)
        log = self.query_one(LiveLog)
        dropped = 0
        top = log.scroll_offset.y
        if log.cursor_row is not None and not self._follow:
            # Log.write can evict old rows as new line endings arrive.
            endings = data.count("\n") + data.count("\r") - data.count("\r\n")
            dropped = max(0, len(log.lines) + endings - MAX_LINES)
            log.cursor_row = max(0, log.cursor_row - dropped)
        log.auto_scroll = self._follow
        log.write(data)
        if log.virtual_size.height != log.line_count:
            log.virtual_size = Size(log.virtual_size.width, log.line_count)
        if dropped and top:
            log.scroll_to(y=max(0, top - dropped), animate=False, immediate=True)
        self._sync_cursor(log)

    def _poll(self) -> None:
        path = self.current
        if self._missing and path is not None and path.is_file():
            self._load()
            return
        self._append()
        self._title()

    def action_next_file(self, step: int) -> None:
        if self._owners:
            self._idx = (self._idx + step) % len(self._owners)
            self._load()

    def action_toggle_output(self) -> None:
        source = self._source
        if not isinstance(source, JobRecord) or not (source.log_path and source.stdout_path):
            return
        stdout = Path(source.stdout_path)
        target = Path(source.log_path) if self.current == stdout else stdout
        self._idx = next(
            i for i, (owner, path) in enumerate(self._owners) if owner is source and path == target
        )
        self._load()

    def action_inspect_record(self) -> None:
        log = self.query_one(LiveLog)
        row = log.cursor_row
        if row is None or row >= log.line_count:
            return
        try:
            body = json.dumps(json.loads(log.lines[row]), indent=2)
        except (ValueError, RecursionError):
            self.notify("No complete JSON record on this line", severity="warning")
            return
        self.app.push_screen(MessageScreen("JSON record", body))

    def action_move(self, lines: int) -> None:
        log = self.query_one(LiveLog)
        if log.cursor_row is None:
            self.action_scroll(lines)
            return
        row = min(max(log.cursor_row + lines, 0), log.line_count - 1)
        if row == log.cursor_row:
            return
        log.scroll_to_region(Region(0, row, 1, 1), x_axis=False, animate=False, immediate=True)
        self._set_follow(row == log.line_count - 1)
        log.cursor_row = row

    def action_scroll(self, lines: int) -> None:
        self.query_one(Log).scroll_relative(y=lines, animate=False)

    def action_page(self, pages: float) -> None:
        self.action_scroll(round(pages * max(2, self.query_one(Log).size.height)))

    def action_bottom(self) -> None:
        self._set_follow(True)

    def action_g_prefix(self) -> None:
        if self._prefix.press("g"):
            log = self.query_one(LiveLog)
            log.scroll_home(animate=False, immediate=True)
            if log.cursor_row is not None:
                self._set_follow(False)
                log.cursor_row = 0

    def action_toggle_follow(self) -> None:
        self._set_follow(not self._follow)

    def _set_follow(self, follow: bool) -> None:
        """Follow from the end, or pause where the view is (also at the end)."""
        log = self.query_one(LiveLog)
        self._follow = log.auto_scroll = follow
        if follow:
            log.scroll_end(animate=False)
            self._sync_cursor(log)
        self._title()
