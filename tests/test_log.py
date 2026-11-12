"""Log polling needs files and a screen, not a repository or a sandbox process."""

import asyncio
from pathlib import Path

from textual.app import App
from textual.widgets import Log

from project_sandbox_tui.jobs import JobRecord
from project_sandbox_tui.sandbox import LaunchSpec
from project_sandbox_tui.screens.log import LogScreen
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
            log = screen.query_one(Log)
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
            log = screen.query_one(Log)
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
            assert "no log files" in "\n".join(screen.query_one(Log).lines)
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
            log = screen.query_one(Log)
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
