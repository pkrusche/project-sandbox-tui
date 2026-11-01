"""Tail view of a job's transcript, agent log and captured output, and of the
output of ``:*`` tasks run in the bookmark's workspace."""

from __future__ import annotations

import codecs
from pathlib import Path
from typing import Protocol

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Log, Static

from project_sandbox_tui.widgets import KeyHints, PrefixKeys

INITIAL_TAIL_BYTES = 256 * 1024


class LogSource(Protocol):
    """A sandbox run (``JobRecord``) or a ``:*`` task (``TaskRecord``)."""

    @property
    def branch(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def exit_code(self) -> int | None: ...

    def output_files(self) -> list[Path]: ...


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
        Binding("j", "scroll(1)", show=False),
        Binding("k", "scroll(-1)", show=False),
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
        self._files = [path for _source, path in self._owners]
        self.job = sources[start] if sources else None
        # Start on the first file that exists (transcript appears only at the end).
        first = next((i for i, (s, _p) in enumerate(self._owners) if s is self.job), 0)
        self._idx = next((i for i, p in enumerate(self._files) if i >= first and p.exists()), first)
        self._pos = 0
        self._follow = True
        self._prefix = PrefixKeys()
        self._missing = False
        self._file_id: tuple[int, int] | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def compose(self) -> ComposeResult:
        yield Static("", id="log-title")
        yield Log(highlight=False, auto_scroll=True, max_lines=10_000)
        yield Static(
            KeyHints("q back · tab/shift+tab file · j/k ^d/^u scroll · gg/G top/bottom · f follow"),
            id="log-hint",
        )

    def on_mount(self) -> None:
        self._load()
        self.set_interval(1.0, self._poll)
        self.watch(self.query_one(Log), "scroll_y", self._scrolled, init=False)

    def on_key(self, event: events.Key) -> None:
        self._prefix.other_key(event.key)

    def _scrolled(self, _old: float, new: float) -> None:
        """Like ``less +F``: scrolling away from the end pauses following, any
        scrolling (keys, mouse) back to the end resumes it."""
        log = self.query_one(Log)
        follow = new >= log.max_scroll_y
        if follow != self._follow:
            self._follow = follow
            log.auto_scroll = follow
            self._title()

    @property
    def current(self) -> Path | None:
        return self._files[self._idx] if self._files else None

    def _title(self) -> None:
        path = self.current
        name = path.name if path else "(no output files)"
        source = self._owners[self._idx][0] if self._owners else self.job
        state = ""
        if source is not None:
            state = source.status + (
                f" · exit {source.exit_code}" if source.exit_code is not None else ""
            )
        follow = "follow" if self._follow else "paused"
        title = Text()
        title.append(f"{source.branch if source else ''}  ", style="bold")
        title.append(f"[{self._idx + 1}/{len(self._files)}] {name}  ", style="")
        title.append(f"{state} · {follow}", style="dim")
        self.query_one("#log-title", Static).update(title)

    def _load(self) -> None:
        log = self.query_one(Log)
        log.clear()
        self._pos = 0
        self._file_id = None
        self._decoder.reset()
        path = self.current
        self._missing = path is not None and not path.exists()
        self._title()
        if path is None:
            log.write_line("This job has no log files (interactive sessions log to your terminal).")
            return
        if not path.exists():
            log.write_line(f"{path} has not been written yet.")
            return
        try:
            stat = path.stat()
        except OSError:
            return
        self._file_id = (stat.st_dev, stat.st_ino)
        size = stat.st_size
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
                stat = path.stat()
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
        log = self.query_one(Log)
        log.auto_scroll = self._follow
        log.write(self._decoder.decode(chunk))

    def _poll(self) -> None:
        path = self.current
        if self._missing and path is not None and path.exists():
            self._load()
            return
        self._append()
        self._title()

    def action_next_file(self, step: int) -> None:
        if self._files:
            self._idx = (self._idx + step) % len(self._files)
            self._load()

    def action_scroll(self, lines: int) -> None:
        self.query_one(Log).scroll_relative(y=lines, animate=False)

    def action_page(self, pages: float) -> None:
        self.action_scroll(round(pages * max(2, self.query_one(Log).size.height)))

    def action_bottom(self) -> None:
        self._set_follow(True)

    def action_g_prefix(self) -> None:
        if self._prefix.press("g"):
            self.query_one(Log).scroll_home(animate=False)

    def action_toggle_follow(self) -> None:
        self._set_follow(not self._follow)

    def _set_follow(self, follow: bool) -> None:
        """Follow from the end, or pause where the view is (also at the end)."""
        log = self.query_one(Log)
        self._follow = log.auto_scroll = follow
        if follow:
            log.scroll_end(animate=False)
        self._title()
