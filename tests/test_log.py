"""Log polling needs files and a screen, not a repository or a sandbox process."""

import asyncio
from pathlib import Path

from textual.app import App
from textual.widgets import Log

from project_sandbox_tui.jobs import JobRecord
from project_sandbox_tui.sandbox import LaunchSpec
from project_sandbox_tui.screens.log import LogScreen


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
