"""Tail view of a job's transcript, agent log and captured output."""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Log, Static

from project_sandbox_tui.jobs import JobRecord

INITIAL_TAIL_BYTES = 256 * 1024


class LogScreen(Screen[None]):
    DEFAULT_CSS = """
    LogScreen #log-title { height: 1; background: $panel; padding: 0 1; }
    LogScreen Log { height: 1fr; border: none; }
    LogScreen #log-hint { height: 1; color: $text-disabled; padding: 0 1; }
    """

    BINDINGS = [
        Binding("q,escape,h", "app.pop_screen", "back", show=False),
        Binding("tab", "next_file", "next file", show=False),
        Binding("j", "scroll(1)", show=False),
        Binding("k", "scroll(-1)", show=False),
        Binding("ctrl+d", "scroll(15)", show=False),
        Binding("ctrl+u", "scroll(-15)", show=False),
        Binding("G", "bottom", show=False),
        Binding("g", "g_prefix", show=False),
        Binding("f", "toggle_follow", show=False),
    ]

    def __init__(self, job: JobRecord) -> None:
        super().__init__()
        self.job = job
        self._files = job.output_files()
        # Start on the first file that exists (transcript appears only at the end).
        self._idx = next((i for i, p in enumerate(self._files) if p.exists()), 0)
        self._pos = 0
        self._follow = True
        self._g_pending = False
        self._missing = False

    def compose(self) -> ComposeResult:
        yield Static("", id="log-title")
        yield Log(highlight=False, auto_scroll=True)
        yield Static(
            "q back · tab next file · j/k scroll · gg/G top/bottom · f follow", id="log-hint"
        )

    def on_mount(self) -> None:
        self._load()
        self.set_interval(1.0, self._poll)

    @property
    def current(self) -> Path | None:
        return self._files[self._idx] if self._files else None

    def _title(self) -> None:
        path = self.current
        name = path.name if path else "(no output files)"
        state = self.job.status + (
            f" · exit {self.job.exit_code}" if self.job.exit_code is not None else ""
        )
        follow = "follow" if self._follow else "paused"
        title = Text()
        title.append(f"{self.job.branch}  ", style="bold")
        title.append(f"[{self._idx + 1}/{len(self._files)}] {name}  ", style="")
        title.append(f"{state} · {follow}", style="dim")
        self.query_one("#log-title", Static).update(title)

    def _load(self) -> None:
        log = self.query_one(Log)
        log.clear()
        self._pos = 0
        path = self.current
        self._missing = path is not None and not path.exists()
        self._title()
        if path is None:
            log.write_line("This job has no log files (interactive sessions log to your terminal).")
            return
        if not path.exists():
            log.write_line(f"{path} has not been written yet.")
            return
        size = path.stat().st_size
        if size > INITIAL_TAIL_BYTES:
            self._pos = size - INITIAL_TAIL_BYTES
            log.write_line(f"… showing last {INITIAL_TAIL_BYTES // 1024} KiB …")
        self._append()

    def _append(self) -> None:
        path = self.current
        if path is None or not path.exists():
            return
        try:
            with path.open("rb") as fh:
                fh.seek(self._pos)
                chunk = fh.read()
        except OSError:
            return
        if not chunk:
            return
        self._pos += len(chunk)
        log = self.query_one(Log)
        log.auto_scroll = self._follow
        log.write(chunk.decode("utf-8", errors="replace"))

    def _poll(self) -> None:
        path = self.current
        if self._missing and path is not None and path.exists():
            self._load()
            return
        self._append()
        self._title()

    def action_next_file(self) -> None:
        if self._files:
            self._idx = (self._idx + 1) % len(self._files)
            self._load()

    def action_scroll(self, lines: int) -> None:
        log = self.query_one(Log)
        log.scroll_relative(y=lines, animate=False)
        if lines < 0:
            self._follow = False
            self._title()

    def action_bottom(self) -> None:
        self.query_one(Log).scroll_end(animate=False)
        self._follow = True
        self._title()

    def action_g_prefix(self) -> None:
        if self._g_pending:
            self.query_one(Log).scroll_home(animate=False)
            self._follow = False
            self._g_pending = False
            self._title()
        else:
            self._g_pending = True
            self.set_timer(0.8, self._reset_g)

    def _reset_g(self) -> None:
        self._g_pending = False

    def action_toggle_follow(self) -> None:
        self._follow = not self._follow
        if self._follow:
            self.query_one(Log).scroll_end(animate=False)
        self._title()
