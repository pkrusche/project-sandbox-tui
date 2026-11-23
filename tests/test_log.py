"""Log polling needs files and a screen, not a repository or a sandbox process."""

import asyncio
import json
from pathlib import Path

from textual.app import App
from textual.widgets import Static

from project_sandbox_tui.jobs import JobRecord
from project_sandbox_tui.logview import LiveLog, markdown_blocks
from project_sandbox_tui.sandbox import LaunchSpec
from project_sandbox_tui.screens.dialogs import MessageScreen
from project_sandbox_tui.screens.log import INITIAL_TAIL_BYTES, LogScreen
from tests.test_app import wait_until


def test_log_follows_partial_utf8_and_truncation(tmp_path: Path) -> None:
    path = tmp_path / "agent.log"
    path.write_bytes(b"first\n\xe2\x82")  # partial euro sign at a poll boundary
    job = JobRecord("main", LaunchSpec("main", "claude"), 0, "", log_path=str(path))

    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test() as pilot:
            screen = LogScreen(job)
            app.push_screen(screen)
            await pilot.pause()
            with path.open("ab") as stream:
                stream.write(b"\xac\n")
            screen._poll()
            log = screen.query_one(LiveLog)
            assert log.lines[:2] == ["first", "€"]
            path.write_bytes(b"new\n")
            screen._poll()
            assert log.lines == ["new", ""]
            # A replacement file can be larger, so size alone cannot detect rotation.
            replacement = path.with_suffix(".replacement")
            replacement.write_text("replacement contents\n")
            replacement.replace(path)
            screen._poll()
            assert log.lines == ["replacement contents", ""]

    asyncio.run(scenario())


def test_log_missing_file_appears_and_switching_resets_decoder(tmp_path: Path) -> None:
    path = tmp_path / "agent.log"
    stdout = tmp_path / "agent.out"
    job = JobRecord(
        "main",
        LaunchSpec("main", "claude"),
        0,
        "",
        log_path=str(path),
        stdout_path=str(stdout),
    )

    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test() as pilot:
            screen = LogScreen(job)
            app.push_screen(screen)
            await pilot.pause()
            log = screen.query_one(LiveLog)
            assert screen.current == path.with_suffix(".md")
            assert "has not been written yet" in "\n".join(log.lines)
            path.with_suffix(".md").write_text("transcript\n")
            screen._poll()
            assert log.lines == ["transcript", ""]
            path.write_bytes(b"\xe2\x82")
            await pilot.press("tab")
            assert screen.current == path
            stdout.write_text("output\n")
            await pilot.press("tab")
            assert screen.current == stdout
            assert log.lines == ["output", ""]  # incomplete bytes from agent.log do not leak
            await pilot.press("tab")
            assert screen.current == path.with_suffix(".md")
            assert log.lines == ["transcript", ""]

    asyncio.run(scenario())


def test_interactive_job_without_log_files() -> None:
    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test() as pilot:
            screen = LogScreen(JobRecord("main", LaunchSpec("main", "bash"), 0, ""))
            app.push_screen(screen)
            await pilot.pause()
            await pilot.press("tab", "f", "G")
            screen._poll()
            assert screen.current is None
            assert "no log files" in "\n".join(screen.query_one(LiveLog).lines)
            await pilot.press("q")
            assert app.screen is not screen

    asyncio.run(scenario())


def test_log_follow_tracks_scroll_position(tmp_path: Path) -> None:
    path = tmp_path / "agent.log"
    path.write_text("".join(f"line {i}\n" for i in range(200)))
    job = JobRecord("main", LaunchSpec("main", "claude"), 0, "", log_path=str(path))

    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test(size=(80, 24)) as pilot:
            screen = LogScreen(job)
            app.push_screen(screen)
            await pilot.pause()
            log = screen.query_one(LiveLog)
            # Scrolling to the end is deferred until after a refresh.
            await wait_until(
                pilot, lambda: log.max_scroll_y > 0 and log.scroll_y == log.max_scroll_y
            )
            assert screen._follow
            await pilot.press("pageup")  # not only k: any scrolling away pauses
            await pilot.pause()
            assert not screen._follow
            with path.open("a") as stream:
                stream.write("more\n")
            screen._poll()
            await pilot.pause()
            assert log.scroll_y < log.max_scroll_y  # the view stays put
            await pilot.press("G")
            await wait_until(pilot, lambda: log.scroll_y == log.max_scroll_y)
            assert screen._follow
            await pilot.press("f")  # an explicit pause at the end sticks
            with path.open("a") as stream:
                stream.write("again\n")
            screen._poll()
            await pilot.pause()
            assert not screen._follow and log.scroll_y < log.max_scroll_y
            await pilot.press("g", "g")
            await wait_until(pilot, lambda: log.scroll_y == 0)
            await pilot.press("j", "j", "ctrl+f")
            await pilot.pause()
            assert log.scroll_y > 2 and not screen._follow

    asyncio.run(scenario())


def test_log_resize_keeps_follow_and_paused_view(tmp_path: Path) -> None:
    path = tmp_path / "agent.log"
    path.write_text("\n".join(json.dumps({"row": i, "body": "x" * 100}) for i in range(50)))
    job = JobRecord("main", LaunchSpec("main", "claude"), 0, "", log_path=str(path))

    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test(size=(100, 20)) as pilot:
            screen = LogScreen(job)
            await app.push_screen(screen)
            log = screen.query_one(LiveLog)
            await wait_until(pilot, lambda: log.max_scroll_y > 0 and log.is_vertical_scroll_end)
            await pilot.press("w")  # long records scroll horizontally without wrapping
            await wait_until(pilot, lambda: log.is_vertical_scroll_end)
            assert not log.wrap and log.row_count == log.line_count
            log.scroll_to(x=8, animate=False)

            await pilot.resize_terminal(80, 12)
            await pilot.pause()
            assert screen._follow and log.is_vertical_scroll_end
            assert log.scroll_x == 8
            assert log.cursor_row == log.line_count - 1

            await pilot.press("f")
            await pilot.resize_terminal(100, 20)
            await pilot.pause()
            assert not screen._follow and log.is_vertical_scroll_end

            await pilot.press("ctrl+b")
            await pilot.pause()
            top = log.scroll_offset.y
            row = log.cursor_row
            assert row is not None and top < row < log.line_count - 1
            await pilot.resize_terminal(80, 20)
            await pilot.pause()
            assert log.scroll_offset.y == top
            assert log.cursor_row == row

            await pilot.resize_terminal(80, 12)
            await pilot.pause()
            assert not screen._follow
            assert log.cursor_row == row
            line = row - log.scroll_offset.y
            assert 0 <= line < log.scrollable_content_region.height
            assert log.render_line(line).text.startswith(f"{row},")
            await pilot.press("enter")
            assert isinstance(app.screen, MessageScreen)
            assert json.loads(app.screen.body)["row"] == row

    asyncio.run(scenario())


def test_log_shift_tab_cycles_files_backwards(tmp_path: Path) -> None:
    path = tmp_path / "agent.log"
    stdout = tmp_path / "agent.out"
    job = JobRecord(
        "main", LaunchSpec("main", "claude"), 0, "", log_path=str(path), stdout_path=str(stdout)
    )

    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test() as pilot:
            screen = LogScreen(job)
            app.push_screen(screen)
            await pilot.pause()
            assert screen.current == path.with_suffix(".md")
            await pilot.press("shift+tab")
            assert screen.current == stdout
            await pilot.press("shift+tab")
            assert screen.current == path

    asyncio.run(scenario())


def test_live_stdout_and_json_record_inspection(tmp_path: Path) -> None:
    path = tmp_path / "agent.log"
    stdout = tmp_path / "agent.out"
    feedback = tmp_path / "feedback.json"
    first = {"type": "message", "content": {"text": "[literal]", "items": [1, 2]}}
    second = {"type": "tool", "name": "read"}
    path.write_text(f"{json.dumps(first)}\nsandbox diagnostic\n{json.dumps(second)}\n")
    stdout.write_text("# Assistant\n\nHello\n")
    feedback.write_text('{"phase":"running"}\n')
    job = JobRecord(
        "main",
        LaunchSpec("main", "codex", mode="background"),
        0,
        "",
        log_path=str(path),
        stdout_path=str(stdout),
        feedback_path=str(feedback),
        feedback_status="valid",
    )

    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test() as pilot:
            screen = LogScreen(job)
            app.push_screen(screen)
            await pilot.pause()
            log = screen.query_one(LiveLog)
            assert screen.current == stdout
            assert log.lines[:3] == ["# Assistant", "", "Hello"]
            with stdout.open("a") as stream:
                stream.write("\n## Tool\n")
            screen._poll()
            assert "## Tool" in log.lines

            await pilot.press("v")
            assert screen.current == path
            first_style = next(iter(log.render_line(0))).style
            selected_style = next(iter(log.render_line(2))).style
            assert first_style and selected_style and first_style.bgcolor != selected_style.bgcolor
            await pilot.press("enter")
            assert isinstance(app.screen, MessageScreen)
            assert app.screen.body == json.dumps(second, indent=2)
            await pilot.press("escape", "k", "enter")
            assert app.screen is screen  # plain diagnostic between JSON records
            await pilot.press("k")
            assert not screen._follow
            with path.open("a") as stream:
                stream.write('{"type":"new"}\n')
            screen._poll()
            await pilot.press("enter")
            assert isinstance(app.screen, MessageScreen)
            assert app.screen.body == json.dumps(first, indent=2)

            await pilot.press("q")
            await pilot.click(LiveLog, offset=(1, 2), times=2)
            assert isinstance(app.screen, MessageScreen)
            assert app.screen.body == json.dumps(second, indent=2)
            await pilot.press("q", "v")
            assert app.screen is screen and screen.current == stdout
            assert "## Tool" in log.lines

            await pilot.press("shift+tab")
            assert screen.current == feedback
            assert log.lines[0] == '{"phase":"running"}'
            assert job.feedback_detail() in str(screen.query_one("#log-title", Static).render())
            await pilot.press("v")
            assert screen.current == stdout

    asyncio.run(scenario())


def test_json_log_tail_preserves_complete_records(tmp_path: Path) -> None:
    path = tmp_path / "agent.log"
    path.write_text("x" * INITIAL_TAIL_BYTES + '\n{"type":"ready"}\n{"type":')
    job = JobRecord("main", LaunchSpec("main", "codex"), 0, "", log_path=str(path))
    assert job.output_files() == [path.with_suffix(".md"), path]

    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test() as pilot:
            screen = LogScreen(job)
            app.push_screen(screen)
            await pilot.pause()
            log = screen.query_one(LiveLog)
            assert log.lines[1:] == ['{"type":"ready"}', '{"type":']
            await pilot.press("enter")
            assert app.screen is screen
            with path.open("a") as stream:
                stream.write('"done"}')
            screen._poll()
            await pilot.press("enter")
            assert isinstance(app.screen, MessageScreen)
            assert json.loads(app.screen.body) == {"type": "done"}

    asyncio.run(scenario())


def test_markdown_blocks_respect_fences_and_continuations() -> None:
    lines = [
        "# Title",  # 0
        "text",
        "",
        "",
        "```sh",  # 4: blank lines inside a fence stay in its block
        "a",
        "",
        "```",
        "",
        "- item",  # 9
        "",
        "  continued",  # 11: indented after a blank line continues the list
        "",
        "after",  # 13
    ]
    assert markdown_blocks(lines, 0, len(lines)) == [
        (0, 2),
        (2, 4),
        (4, 8),
        (8, 9),
        (9, 12),
        (12, 13),
        (13, 14),
    ]
    assert markdown_blocks(["```", "open", ""], 0, 3) == [(0, 3)]
    assert markdown_blocks(["```inline```", "", "next"], 0, 3) == [(0, 1), (1, 2), (2, 3)]
    assert markdown_blocks(["~~~", "    ~~~", "", "code", "  ~~~"], 0, 5) == [(0, 5)]


def _rows(log: LiveLog) -> list[str]:
    return [log.render_line(y).text.rstrip() for y in range(log.scrollable_content_region.height)]


def test_log_wraps_long_lines_and_toggles(tmp_path: Path) -> None:
    stdout = tmp_path / "agent.out"
    words = " ".join(f"word{i}" for i in range(30))
    stdout.write_text(f"short\n{words}\n")
    job = JobRecord("main", LaunchSpec("main", "bash"), 0, "", stdout_path=str(stdout))

    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test(size=(40, 20)) as pilot:
            screen = LogScreen(job)
            await app.push_screen(screen)
            log = screen.query_one(LiveLog)
            await wait_until(pilot, lambda: log.row_count > log.line_count == 2)
            assert not log.markdown  # bash output is plain text
            rows = _rows(log)
            assert rows[0] == "short" and rows[1].startswith("word0 word1")
            assert " ".join(rows[1 : log.row_count]) == words  # wrapped at word boundaries
            assert all(len(row) <= log.scrollable_content_region.width for row in rows)
            assert log.row_of(1) == 1 and log.line_at(log.row_count - 1) == 1

            await pilot.press("w")
            await pilot.pause()
            assert not log.wrap and log.row_count == 2 and log.virtual_size.width > 40
            assert "no wrap" in str(screen.query_one("#log-title", Static).render())
            await pilot.press("m")  # not a Markdown file: no effect
            assert not log.markdown
            await pilot.press("w")
            await pilot.pause()
            assert log.wrap and log.row_count > 2

            wide = log.row_count
            await pilot.resize_terminal(30, 20)
            await wait_until(pilot, lambda: log.row_count > wide)

    asyncio.run(scenario())


def test_markdown_transcript_renders_streams_and_toggles(tmp_path: Path) -> None:
    path = tmp_path / "agent.log"
    transcript = path.with_suffix(".md")
    transcript.write_text("# Session\n\nSome **bold** text.\n\n```python\nx = 1\n")
    job = JobRecord("main", LaunchSpec("main", "claude"), 0, "", log_path=str(path))

    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test(size=(60, 20)) as pilot:
            screen = LogScreen(job)
            await app.push_screen(screen)
            log = screen.query_one(LiveLog)
            await wait_until(pilot, lambda: log.row_count > 0)
            assert screen.current == transcript and log.markdown
            assert "markdown" in str(screen.query_one("#log-title", Static).render())
            rows = _rows(log)
            assert "Some bold text." in [row.strip() for row in rows]
            assert not any("**" in row or row.startswith("#") for row in rows)
            # Blank lines inside an unclosed fence do not end the code block.
            with transcript.open("a") as stream:
                stream.write("\ny = 2\n```\n\n- done\n")
            screen._poll()
            await pilot.pause()
            rows = [row.strip() for row in _rows(log)]
            assert "x = 1" in rows and "y = 2" in rows and "```" not in rows
            assert "• done" in rows

            await pilot.press("m")
            await pilot.pause()
            assert not log.markdown
            assert "Some **bold** text." in _rows(log)
            await pilot.press("tab")  # the raw agent log is never Markdown
            assert screen.current == path and not log.markdown
            await pilot.press("shift+tab")  # the choice holds across files
            assert screen.current == transcript and not log.markdown
            await pilot.press("m")
            assert log.markdown and "Some **bold** text." not in _rows(log)

    asyncio.run(scenario())


def test_wrapped_json_records_keep_line_cursor(tmp_path: Path) -> None:
    path = tmp_path / "agent.log"
    records = [{"row": i, "body": "x " * 40} for i in range(3)]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    job = JobRecord("main", LaunchSpec("main", "claude"), 0, "", log_path=str(path))

    async def scenario() -> None:
        app: App[None] = App()
        async with app.run_test(size=(40, 30)) as pilot:
            screen = LogScreen(job)
            await app.push_screen(screen)
            log = screen.query_one(LiveLog)  # no transcript: opens on the agent log
            await wait_until(pilot, lambda: screen.current == path and log.row_count > 3)
            assert log.cursor_row == 2
            per_record = log.rows_of(0)
            assert per_record > 1 and log.row_of(1) == per_record

            def highlighted() -> list[int]:
                plain = next(iter(log.render_line(log.row_count)), None)
                base = plain.style.bgcolor if plain and plain.style else None
                return [
                    y
                    for y in range(log.row_count)
                    if (seg := next(iter(log.render_line(y)))).style and seg.style.bgcolor != base
                ]

            assert highlighted() == list(range(2 * per_record, 3 * per_record))
            await pilot.press("k")
            assert log.cursor_row == 1 and not screen._follow
            assert highlighted() == list(range(per_record, 2 * per_record))
            await pilot.click(LiveLog, offset=(1, per_record - 1), times=2)  # last row of record 0
            assert isinstance(app.screen, MessageScreen)
            assert json.loads(app.screen.body) == records[0]

    asyncio.run(scenario())


def test_markdown_pruning_cuts_at_block_boundaries() -> None:
    async def scenario() -> None:
        app = App[None]()
        async with app.run_test(size=(40, 20)) as pilot:
            log = LiveLog(max_lines=10)
            await app.mount(log)
            await pilot.pause()
            log.set_mode(markdown=True)
            log.write("".join(f"para {i}\nmore {i}\n\n" for i in range(3)))
            assert log.line_count == 9
            before = log.render_line(log.row_of(4))
            log.cursor_row = 4  # second logical line in a paragraph
            assert list(log.render_line(log.row_of(4))) != list(before)
            log.write("```\ncode\n```\n")
            # 13 lines: the first paragraph and its blank line go, not part of a block.
            assert log.cursor_row == 1
            assert log.lines[0] == "para 1" and log.line_count == 9
            rows = [log.render_line(y).text.strip() for y in range(log.row_count)]
            assert rows[0] == "para 1 more 1" and "code" in rows
            log.auto_scroll = False
            log.write("".join(f"line {i}\n" for i in range(12)))  # one block over the limit
            assert len(log.lines) <= 10 and log.lines[-1] == ""

    asyncio.run(scenario())


def test_streamed_controls_and_split_crlf() -> None:
    async def scenario() -> None:
        app = App[None]()
        async with app.run_test() as pilot:
            log = LiveLog()
            await app.mount(log)
            await pilot.pause()
            log.write("\x1b[31mred\x1b[0m\tX\r")
            log.write("\nA\x1f\x7f\x80B")
            for markdown in (False, True):
                log.set_mode(markdown=markdown)
                rendered = " ".join(_rows(log)).strip()
                assert "red" in rendered and "A���B" in rendered
                assert "\x1b" not in rendered and "[31m" not in rendered
            assert log.line_count == 2

    asyncio.run(scenario())
