"""Scrolling tail view that wraps long lines and can render Markdown.

Text is kept as logical lines, like Textual's ``Log``, and laid out into visual
rows for the current width. A layout block is one logical line for plain text,
or one Markdown block (paragraph, list, fenced code) between blank lines, so
appending output only re-renders the last block. Row and line positions convert
with :meth:`LiveLog.row_of` and :meth:`LiveLog.line_at`.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Sequence

from rich.markdown import Markdown
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.geometry import Size
from textual.reactive import reactive, var
from textual.scroll_view import ScrollView
from textual.selection import Selection
from textual.strip import Strip

_sub_escape = re.compile(r"[\x00-\x1f\x7f-\x9f]").sub
_ansi = re.compile(
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]|(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)"
)
_line_endings = re.compile(r"(\r\n|\r|\n)")
_fence = re.compile(r" {0,3}(`{3,}|~{3,})")


def _process_line(line: str) -> str:
    """Replace control codes as ``Log`` does, so rows keep their cell widths."""
    return _sub_escape("\N{REPLACEMENT CHARACTER}", _ansi.sub("", line).expandtabs())


def markdown_blocks(lines: Sequence[str], start: int, end: int) -> list[tuple[int, int]]:
    """Split ``lines[start:end]`` into ``(first, stop)`` Markdown layout blocks.

    ``start`` must lie outside a code fence. Each run of blank lines becomes a
    block of its own, so it renders as one empty row. An indented line after
    blank lines continues the previous block (list items, indented code)."""
    blocks: list[tuple[int, int]] = []
    fence = ""
    first: int | None = None  # start of the open content block
    blank: int | None = None  # start of the open blank run
    for i in range(start, end):
        line = lines[i]
        if fence:
            match = _fence.match(line)
            if match and match[1].startswith(fence) and not line[match.end() :].strip(" \t"):
                fence = ""
            continue
        if not line.strip():
            if first is not None:
                blocks.append((first, i))
                first = None
            if blank is None:
                blank = i
            continue
        if blank is not None:
            if line[:1] in " \t" and blocks:
                first = blocks.pop()[0]  # continuation: reopen the previous block
            else:
                blocks.append((blank, i))
            blank = None
        if first is None:
            first = i
        if (match := _fence.match(line)) and (match[1][0] == "~" or "`" not in line[match.end() :]):
            fence = match.group(1)
    if first is not None:
        blocks.append((first, end))
    if blank is not None:
        blocks.append((blank, end))
    return blocks


class LiveLog(ScrollView, can_focus=True):
    """Tail widget with wrapping, optional Markdown, and a highlighted cursor line.

    ``cursor_row`` is a logical line index; all rows of that line are highlighted.
    """

    ALLOW_SELECT = True
    COMPONENT_CLASSES = {"live-log--cursor"}
    DEFAULT_CSS = """
    LiveLog {
        background: $surface;
        color: $text;
        overflow-y: scroll;
        overflow-x: auto;
        &:focus { background-tint: $foreground 5%; }
    }
    LiveLog > .live-log--cursor { background: $accent 25%; }
    """

    cursor_row: reactive[int | None] = reactive(None)
    auto_scroll: var[bool] = var(True)

    def __init__(self, max_lines: int | None = None, *, wrap: bool = True) -> None:
        super().__init__()
        self.max_lines = max_lines
        self._wrap = wrap
        self._markdown = False
        self._ended_cr = False
        self._lines: list[str] = []
        self._width = 0  # the width the current layout was made for, 0 before layout
        self._max_width = 0
        self._starts: list[int] = []  # first logical line of each block
        self._rows: list[list[Strip]] = []  # rendered rows of each block
        self._offsets: list[int] = []  # first visual row of each block
        self._row_count = 0
        self.adjusting = False
        """True while the widget moves its own viewport after a relayout or pruning."""

    # Logical lines

    @property
    def lines(self) -> Sequence[str]:
        return self._lines

    @property
    def line_count(self) -> int:
        """Number of lines; a final line ending does not start another one."""
        if self._lines:
            return len(self._lines) - (self._lines[-1] == "")
        return 0

    @property
    def row_count(self) -> int:
        return self._row_count

    @property
    def wrap(self) -> bool:
        return self._wrap

    @property
    def markdown(self) -> bool:
        return self._markdown

    def set_mode(self, *, markdown: bool | None = None, wrap: bool | None = None) -> None:
        """Change rendering and lay all lines out again, keeping the view's position."""
        changed = False
        if markdown is not None and markdown != self._markdown:
            self._markdown, changed = markdown, True
        if wrap is not None and wrap != self._wrap:
            self._wrap, changed = wrap, True
        if changed:
            self._relayout()

    def clear(self) -> None:
        self._ended_cr = False
        self._lines.clear()
        self._starts.clear()
        self._rows.clear()
        self._offsets.clear()
        self._row_count = 0
        self._max_width = 0
        self.scroll_to(0, 0, animate=False, immediate=True)
        self._update_virtual_size()
        self.refresh()

    def write(self, data: str) -> None:
        if not data:
            return
        if self._ended_cr:
            data = data.removeprefix("\n")
        self._ended_cr = data.endswith("\r")
        if not self._lines:
            self._lines.append("")
        changed = len(self._lines) - 1
        for piece in _line_endings.split(data):
            if piece and piece in "\r\n":
                self._lines.append("")
            else:
                self._lines[-1] += piece
        if self.max_lines is not None and len(self._lines) > self.max_lines:
            removed = self._prune(len(self._lines) - self.max_lines)
            changed -= removed
            if self.cursor_row is not None:
                self.cursor_row = max(0, self.cursor_row - removed)
        self._layout_from(max(0, changed))
        if self.auto_scroll:
            self.scroll_end(animate=False, immediate=True, x_axis=False)

    def write_line(self, line: str) -> None:
        self.write(line + "\n")

    # Row/line mapping

    def _block_of_line(self, line: int) -> int:
        return max(0, bisect_right(self._starts, line) - 1)

    def _block_of_row(self, row: int) -> int:
        return max(0, bisect_right(self._offsets, row) - 1)

    def row_of(self, line: int) -> int:
        """First visual row showing logical ``line``."""
        if not self._starts:
            return 0
        return self._offsets[self._block_of_line(line)]

    def rows_of(self, line: int) -> int:
        """Number of rows showing the block that contains ``line``."""
        if not self._starts:
            return 1
        return max(1, len(self._rows[self._block_of_line(line)]))

    def line_at(self, row: int) -> int:
        """Logical line shown on visual ``row`` (the block's first line)."""
        if not self._starts:
            return 0
        return self._starts[self._block_of_row(min(max(row, 0), self._row_count - 1))]

    # Layout

    def _layout_width(self) -> int:
        return self.scrollable_content_region.width

    def _render_block(self, first: int, stop: int, width: int) -> list[Strip]:
        console = self.app.console
        if self._markdown:
            text = "\n".join(_process_line(line) for line in self._lines[first:stop])
            if not text.strip():
                return [Strip.blank(0)]
            options = console.options.update(width=width, height=None, highlight=False)
            rendered = console.render_lines(Markdown(text), options, pad=False)
            rows = [Strip(line) for line in rendered]
            # Blank-line blocks separate blocks; drop Rich's own margins (before lists).
            while rows and not rows[0].cell_length:
                rows.pop(0)
            while rows and not rows[-1].cell_length:
                rows.pop()
            return rows or [Strip.blank(0)]
        content = Text(_process_line(self._lines[first]), no_wrap=not self._wrap)
        if not self._wrap:
            return [Strip(content.render(console), content.cell_len)]
        wrapped = content.wrap(console, width, overflow="fold") or [Text()]
        return [Strip(line.render(console), line.cell_len) for line in wrapped]

    def _layout_from(self, line: int) -> None:
        """Lay out again from the block containing ``line`` to the end."""
        width = self._layout_width()
        if width <= 0:
            self._width = 0  # laid out on the first resize
            return
        if width != self._width:
            self._relayout()
            return
        end = self.line_count
        index = self._block_of_line(line) if self._starts else 0
        if self._markdown:
            index = max(0, index - 2)  # an indented line after blank lines continues a block
        start = self._starts[index] if self._starts else 0
        del self._starts[index:], self._rows[index:], self._offsets[index:]
        row = self._offsets[-1] + len(self._rows[-1]) if self._starts else 0
        if self._markdown:
            blocks = markdown_blocks(self._lines, start, end)
        else:
            blocks = [(i, i + 1) for i in range(start, end)]
        for first, stop in blocks:
            rows = self._render_block(first, stop, width)
            self._starts.append(first)
            self._rows.append(rows)
            self._offsets.append(row)
            row += len(rows)
            if not self._wrap and not self._markdown:
                self._max_width = max(self._max_width, rows[0].cell_length)
        self._row_count = row
        self._update_virtual_size()
        self.refresh()

    def _relayout(self) -> None:
        """Lay everything out for the current width, keeping the top line in view."""
        width = self._layout_width()
        if width <= 0:
            return
        top = self.line_at(self.scroll_offset.y) if self._row_count else 0
        self._width = width
        self._starts.clear()
        self._rows.clear()
        self._offsets.clear()
        self._max_width = 0
        self._layout_from(0)
        self.adjusting = True
        try:
            if self.auto_scroll:
                self.scroll_end(animate=False, immediate=True, x_axis=False)
            else:
                self.scroll_to(y=self.row_of(top), animate=False, immediate=True)
        finally:
            self.adjusting = False

    def _prune(self, excess: int) -> int:
        """Drop at least ``excess`` leading lines at a block boundary; return the count."""
        if not self._starts or self._width == 0:
            del self._lines[:excess]
            self._starts.clear()
            self._rows.clear()
            self._offsets.clear()
            self._width = 0
            return excess
        index = bisect_right(self._starts, excess - 1)  # first block starting at or after excess
        if index >= len(self._starts):
            remove = excess  # one block covers the cut: lay it out again
            index = len(self._starts) - 1
        else:
            remove = self._starts[index]
        dropped_rows = self._offsets[index]
        del self._lines[:remove]
        del self._starts[:index], self._rows[:index], self._offsets[:index]
        self._starts = [max(0, start - remove) for start in self._starts]
        self._offsets = [offset - dropped_rows for offset in self._offsets]
        self._row_count -= dropped_rows
        if dropped_rows and not self.auto_scroll:
            self.adjusting = True
            try:
                y = max(0, self.scroll_offset.y - dropped_rows)
                self._update_virtual_size()
                self.scroll_to(y=y, animate=False, immediate=True)
            finally:
                self.adjusting = False
        return remove

    def _update_virtual_size(self) -> None:
        width = self._max_width if not self._wrap and not self._markdown else self._width
        self.virtual_size = Size(width, self._row_count)

    def on_resize(self, event: events.Resize) -> None:
        if self._layout_width() != self._width:
            self._relayout()

    # Rendering

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        text = "\n".join(strip.text for rows in self._rows for strip in rows)
        return selection.extract(text), "\n"

    def selection_updated(self, selection: Selection | None) -> None:
        self.refresh()

    def _row(self, y: int) -> tuple[int, Strip] | None:
        if not 0 <= y < self._row_count:
            return None
        block = self._block_of_row(y)
        return block, self._rows[block][y - self._offsets[block]]

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        width = self.scrollable_content_region.width
        style = self.rich_style
        row = scroll_y + y
        found = self._row(row)
        if found is None:
            return Strip.blank(width, style)
        block, strip = found
        selection = self.text_selection
        if selection is not None and (span := selection.get_span(row)) is not None:
            end = strip.cell_length if span[1] == -1 else min(span[1], strip.cell_length)
            start = min(span[0], end)
            before, middle, after = strip.divide([start, end, strip.cell_length])
            highlight = self.screen.get_component_rich_style("screen--selection")
            strip = Strip.join([before, middle.apply_style(highlight), after])
        strip = strip.apply_style(style).crop_extend(scroll_x, scroll_x + width, style)
        if (
            self.cursor_row is not None
            and 0 <= self.cursor_row < self.line_count
            and self._block_of_line(self.cursor_row) == block
        ):
            cursor: Style = self.get_component_rich_style("live-log--cursor")
            strip = Strip(Segment.apply_style(strip, post_style=cursor), strip.cell_length)
        return strip.apply_offsets(scroll_x, row)

    def watch_cursor_row(self) -> None:
        self.refresh()
