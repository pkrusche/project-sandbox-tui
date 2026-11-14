from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, TextArea

from project_sandbox_tui.app import SandboxTUI
from project_sandbox_tui.config import Config
from project_sandbox_tui.jobs import JobManager
from project_sandbox_tui.queue import TaskQueue
from project_sandbox_tui.sandbox import LaunchSpec
from project_sandbox_tui.screens.launch import LaunchScreen
from project_sandbox_tui.screens.queue import QueueScreen
from tests.conftest import wait_for
from tests.test_app import run_app, wait_for_screen, wait_until


def prompt(text: str, branch: str = "feature/x") -> LaunchSpec:
    return LaunchSpec(branch, "claude", mode="background", prompt_text=text)


def prompts(queue: TaskQueue, branch: str = "feature/x") -> list[str]:
    queue.load()
    return [t.spec.prompt_text for t in queue.get(branch).tasks]


def test_persistent_editing_and_undo(tmp_path: Path) -> None:
    queue = TaskQueue(tmp_path)
    a, b, c = [queue.add(prompt(text)) for text in ("a", "b", "c")]
    queue.pause("feature/x", True)
    queue.mark("feature/x", "a", a.id)
    queue.move("feature/x", [b.id, c.id], -1)
    assert prompts(queue) == ["b", "c", "a"]
    queue.edit("feature/x", a.id, prompt("edited"))
    queue.delete("feature/x", [b.id, c.id])
    other = TaskQueue(tmp_path)
    assert prompts(other) == ["edited"]
    assert other.get("feature/x").paused
    assert other.get("feature/x").marks == {"a": a.id}
    other.undo("feature/x")
    other.undo("feature/x")
    assert prompts(other) == ["b", "c", "a"]
    other.undo("feature/x", redo=True)
    assert prompts(other) == ["b", "c", "edited"]
    queue.add(prompt("new"))  # reload under lock: no lost edits from other instances
    assert prompts(other) == ["b", "c", "edited", "new"]
    other.undo("feature/x", redo=True)
    assert prompts(other) == ["b", "c", "edited", "new"]


def test_queue_rejects_interactive_and_corrupt_state(tmp_path: Path) -> None:
    queue = TaskQueue(tmp_path)
    with pytest.raises(ValueError, match="only unsupervised"):
        queue.add(LaunchSpec("feature/x", "claude"))
    with pytest.raises(ValueError, match="only unsupervised"):
        queue.add(prompt(" "))
    task = queue.add(prompt("a"))
    with pytest.raises(ValueError, match="same bookmark"):
        queue.edit("feature/x", task.id, prompt("b", "elsewhere"))
    queue.path.write_text('{"broken":')
    with pytest.raises(ValueError, match="cannot read task queue"):
        queue.add(prompt("do not overwrite corruption"))
    assert queue.path.read_text() == '{"broken":'


def test_dispatch_concurrent_restart_and_no_replay(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    cfg = fake_cfg()
    jobs = JobManager(repo, cfg)
    queue = TaskQueue(jobs.state_dir)
    first = queue.add(prompt("sleep:0.1 exit:0"))
    second = queue.add(prompt("sleep:0.1 exit:3"))
    third = queue.add(prompt("sleep:0.1 exit:0 third"))
    queue.pause("feature/x", True)
    assert queue.dispatch(jobs, lambda: {}, lambda _: None) == []
    queue.pause("feature/x", False)

    def dispatch() -> list[str]:
        manager = JobManager(repo, cfg)
        store = TaskQueue(manager.state_dir)
        return [j.queue_id for j in store.dispatch(manager, lambda: {}, lambda _: None)]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: dispatch(), range(2)))
    assert sorted(results, key=len) == [[], [first.id]]
    assert prompts(queue) == [second.spec.prompt_text, third.spec.prompt_text]
    wait_for(lambda: bool(jobs.refresh({})))
    # A new TUI resumes in FIFO order after adopting the finished run.
    queue = TaskQueue(jobs.state_dir)
    assert [j.queue_id for j in queue.dispatch(jobs, lambda: {}, lambda _: None)] == [second.id]
    wait_for(lambda: bool(jobs.refresh({})))
    queue.undo("feature/x")  # undo the third enqueue, not either consumed task
    assert prompts(queue) == []
    queue.undo("feature/x", redo=True)
    assert prompts(queue) == [third.spec.prompt_text]
    assert [j.queue_id for j in queue.dispatch(jobs, lambda: {}, lambda _: None)] == [third.id]
    wait_for(lambda: bool(jobs.refresh({})))
    queue.undo("feature/x")
    queue.undo("feature/x", redo=True)
    assert prompts(queue) == []


def test_recover_between_job_save_and_queue_save(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    jobs = JobManager(repo, fake_cfg())
    queue = TaskQueue(jobs.state_dir)
    task = queue.add(prompt("sleep:0.1"))
    original = jobs.start_background(task.spec, {}, queue_id=task.id)
    wait_for(lambda: bool(jobs.refresh({})))
    # A later manual run may already have archived the queued run.
    jobs.start_background(prompt("sleep:0.1 later"), {})
    recovered = TaskQueue(jobs.state_dir).dispatch(jobs, lambda: {}, lambda _: None)
    assert [j.started_at for j in recovered] == [original.started_at]
    assert prompts(queue) == []
    wait_for(lambda: bool(jobs.refresh({})))


def test_launch_failure_and_missing_bookmark_pause_only_their_queue(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    cfg = fake_cfg()
    jobs = JobManager(repo, cfg)
    queue = TaskQueue(jobs.state_dir)
    queue.add(prompt("a"))
    queue.add(prompt("b", "deleted"))
    cfg.sandbox.command = "/nonexistent/sandbox"
    assert (
        queue.dispatch(jobs, lambda: {}, lambda b: "bookmark missing" if b == "deleted" else None)
        == []
    )
    assert prompts(queue) == ["a"]
    assert queue.get("feature/x").paused and queue.get("feature/x").error
    assert queue.get("deleted").paused and queue.get("deleted").error == "bookmark missing"
    jobs.cfg = fake_cfg()
    queue.pause("feature/x", False)
    assert len(queue.dispatch(jobs, lambda: {}, lambda _: None)) == 1
    assert queue.get("deleted").paused
    wait_for(lambda: bool(jobs.refresh({})))


def test_queue_vim_editing(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app.queue.pause("feature/x", True)
        for text in ("alpha", "beta", "gamma"):
            app.queue.add(prompt(text))
        await pilot.press("j", "Q")
        assert app.screen is app.main
        await pilot.press("L")
        screen = await wait_for_screen(app, pilot, QueueScreen)
        assert screen.table.row_count == 3
        await pilot.press("m", "a", "G", "apostrophe", "a")
        assert screen.table.cursor_row == 0
        await pilot.press("J")
        assert prompts(app.queue) == ["beta", "alpha", "gamma"]
        await pilot.press("u")
        assert prompts(app.queue) == ["alpha", "beta", "gamma"]
        await pilot.press("ctrl+r")
        assert prompts(app.queue) == ["beta", "alpha", "gamma"]
        await pilot.press("g", "g", "v", "j", "d", "d")
        assert prompts(app.queue) == ["gamma"]
        await pilot.press("u")
        assert prompts(app.queue) == ["beta", "alpha", "gamma"]
        await pilot.press("slash")
        screen.query_one(Input).value = "gamma"
        await pilot.press("enter")
        assert screen.table.row_count == 1
        await pilot.press("e")
        form = await wait_for_screen(app, pilot, LaunchScreen)
        assert form.query_one("#branch", Input).disabled
        form.query_one("#prompt", TextArea).text = "updated gamma"
        await pilot.press("ctrl+s")
        await wait_for_screen(app, pilot, QueueScreen)
        assert prompts(app.queue) == ["beta", "alpha", "updated gamma"]
        await pilot.press("escape", "n")
        form = await wait_for_screen(app, pilot, LaunchScreen)
        form.query_one("#prompt", TextArea).text = "delta"
        await pilot.press("ctrl+s")
        await wait_for_screen(app, pilot, QueueScreen)
        assert prompts(app.queue)[-1] == "delta"
        await pilot.press("L")
        assert app.screen is app.main
        assert app.queue.get("feature/x").paused

    run_app(repo, fake_cfg(), scenario)


def test_busy_run_queues_but_interactive_does_not(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        reservation = app.jobs.record_external(LaunchSpec("feature/x", "claude"), ["external"])
        app.queue.pause("feature/x", True)
        await app.refresh_data()
        await pilot.press("j", "i")
        assert app.screen is app.main
        await pilot.press("r")
        form = await wait_for_screen(app, pilot, LaunchScreen)
        form.query_one("#prompt", TextArea).text = "sleep:0.1 queued"
        await pilot.press("ctrl+s")
        await wait_until(pilot, lambda: len(app.queue.get("feature/x").tasks) == 1)
        assert app.jobs.jobs["feature/x"].started_at == reservation.started_at
        app.jobs.clear("feature/x", force=True)
        await pilot.press("L")
        await wait_for_screen(app, pilot, QueueScreen)
        await pilot.press("p")
        await wait_until(pilot, lambda: bool(app.jobs.jobs.get("feature/x", None)))
        await wait_until(pilot, lambda: not app.jobs.jobs["feature/x"].running)
        assert app.jobs.jobs["feature/x"].spec.prompt_text == "sleep:0.1 queued"
        assert prompts(app.queue) == []

    run_app(repo, fake_cfg(), scenario)


def test_queue_resumes_on_tui_restart(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    cfg = fake_cfg()
    jobs = JobManager(repo, cfg)
    queue = TaskQueue(jobs.state_dir)
    queue.add(prompt("sleep:0.1 first"))
    queue.add(prompt("sleep:0.1 second"))

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await wait_until(
            pilot,
            lambda: (
                bool(app.jobs.jobs.get("feature/x"))
                and app.jobs.jobs["feature/x"].spec.prompt_text.endswith("second")
                and not app.jobs.jobs["feature/x"].running
            ),
        )
        await app.refresh_data()
        runs = [n for n in app.graph.state.nodes.values() if n.kind == "run"]
        assert len(runs) == 2
        assert all(n.status == "completed" for n in runs)
        assert prompts(app.queue) == []
        await asyncio.sleep(0)  # let notification callbacks finish

    run_app(repo, cfg, scenario)


def test_pause_is_per_bookmark_and_busy_sessions_wait(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    jobs = JobManager(repo, fake_cfg())
    queue = TaskQueue(jobs.state_dir)
    queue.add(prompt("sleep:0.1", "a"))
    queue.add(prompt("sleep:0.1", "b"))
    reservation = jobs.record_external(LaunchSpec("a", "claude"), ["external"])
    assert [j.branch for j in queue.dispatch(jobs, lambda: {}, lambda _: None)] == ["b"]
    assert jobs.jobs["a"].started_at == reservation.started_at
    queue.pause("a", True)
    jobs.clear("a", force=True)
    assert queue.dispatch(jobs, lambda: {}, lambda _: None) == []
    assert prompts(queue, "a") == ["sleep:0.1"]
    queue.pause("a", False)
    assert [j.branch for j in queue.dispatch(jobs, lambda: {}, lambda _: None)] == ["a"]
    wait_for(lambda: (jobs.refresh({}), jobs.running_count() == 0)[1])


@pytest.mark.parametrize("change", ["edit", "delete"])
def test_queue_form_keeps_draft_after_concurrent_change(
    repo: Path, fake_cfg: Callable[..., Config], change: str
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app.queue.pause("feature/x", True)
        task = app.queue.add(prompt("original"))
        await pilot.press("j", "L")
        await wait_for_screen(app, pilot, QueueScreen)
        await pilot.press("e")
        form = await wait_for_screen(app, pilot, LaunchScreen)
        form.query_one("#prompt", TextArea).text = "my draft"
        other = TaskQueue(app.jobs.state_dir)
        if change == "edit":
            other.edit("feature/x", task.id, prompt("other editor"))
        else:
            other.delete("feature/x", [task.id])
        await pilot.press("ctrl+s")
        assert app.screen is form
        assert form.query_one("#prompt", TextArea).text == "my draft"
        assert prompts(other) == (["other editor"] if change == "edit" else [])

    run_app(repo, fake_cfg(), scenario)


def test_newer_queue_state_is_preserved(tmp_path: Path) -> None:
    queue = TaskQueue(tmp_path)
    contents = '{"version": 999, "bookmarks": {}}'
    queue.path.write_text(contents)
    with pytest.raises(ValueError, match="unsupported queue state version"):
        queue.add(prompt("new"))
    assert queue.path.read_text() == contents


def test_dry_run_does_not_consume_pending_tasks(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app.dry_run = True
        app.cfg.sandbox.extra_args.append("--dry-run")
        app.queue.add(prompt("real pending task"))
        await app.refresh_data()
        assert not app.jobs.jobs
        app._start_launch(prompt("sleep:0.1 preview"))
        await wait_until(pilot, lambda: not app.jobs.jobs["feature/x"].running)
        assert "--dry-run" in app.jobs.jobs["feature/x"].argv
        assert prompts(app.queue) == ["real pending task"]

    run_app(repo, fake_cfg(), scenario)
