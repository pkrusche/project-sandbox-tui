from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from textual.widgets import Input, TextArea
from textual.widgets.data_table import ColumnKey

from project_sandbox_tui.app import SandboxTUI
from project_sandbox_tui.config import Config
from project_sandbox_tui.jobs import BusyError, JobManager
from project_sandbox_tui.queue import BookmarkQueue, TaskQueue
from project_sandbox_tui.sandbox import LaunchSpec
from project_sandbox_tui.screens.dialogs import MessageScreen
from project_sandbox_tui.screens.launch import LaunchScreen
from project_sandbox_tui.screens.queue import QueueScreen
from project_sandbox_tui.vcs import VcsError
from project_sandbox_tui.widgets import TABLE_PREVIEW_WIDTH
from tests.conftest import wait_for
from tests.test_app import run_app, wait_for_screen, wait_until


def prompt(text: str, branch: str = "feature/x") -> LaunchSpec:
    return LaunchSpec(branch, "claude", mode="background", prompt_text=text)


def prompts(queue: TaskQueue, branch: str = "feature/x") -> list[str]:
    queue.load()
    return [t.description for t in queue.get(branch).tasks]


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
    assert prompts(queue) == [second.description, third.description]
    wait_for(lambda: bool(jobs.refresh({})))
    # A new TUI resumes in FIFO order after adopting the finished run.
    queue = TaskQueue(jobs.state_dir)
    assert [j.queue_id for j in queue.dispatch(jobs, lambda: {}, lambda _: None)] == [second.id]
    wait_for(lambda: bool(jobs.refresh({})))
    queue.undo("feature/x")  # undo the third enqueue, not either consumed task
    assert prompts(queue) == []
    queue.undo("feature/x", redo=True)
    assert prompts(queue) == [third.description]
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
    assert task.spec is not None
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


def test_queue_previews_long_prompts_and_shows_full_text(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        full_prompt = "First line\n\n  " + "界" * 500 + "\nsuffix"
        app.queue.pause("feature/x", True)
        task = app.queue.add(prompt(full_prompt))
        app.queue.add(prompt("short prompt"))
        await pilot.press("j", "L")
        screen = await wait_for_screen(app, pilot, QueueScreen)
        preview = screen.table.get_cell(task.id, "prompt")
        assert preview.cell_len == TABLE_PREVIEW_WIDTH
        assert preview.plain.startswith("First line 界") and preview.plain.endswith("…")
        assert screen.table.columns[ColumnKey("prompt")].content_width == TABLE_PREVIEW_WIDTH
        assert str(screen.table.get_cell(task.id, "outcome")) == "—"
        assert str(screen.table.get_cell(task.id, "flow")) == ""

        await pilot.press("slash", *"suffix", "enter")
        assert screen.visible_ids() == [task.id]
        await pilot.press("s")
        modal = await wait_for_screen(app, pilot, MessageScreen)
        assert modal.body == full_prompt
        await pilot.press("q")
        assert app.screen is screen and screen.selected_id() == task.id

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


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("target", ["@", "feature/x"])
def test_immediate_background_launch_retains_task(
    repo: Path, fake_cfg: Callable[..., Config], dry_run: bool, target: str
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app._refresh_timer.pause()
        await app.refresh_data()
        app.dry_run = dry_run
        if dry_run:
            app.cfg.sandbox.extra_args.append("--dry-run")
        spec = prompt("sleep:0.1 immediate run", target)
        app._start_launch(spec)
        job = app.jobs.jobs[target]
        stored = TaskQueue(app.jobs.state_dir)
        stored.load()
        queue = stored.get(target)
        assert not queue.tasks
        (task,) = queue.history
        assert task.id == job.queue_id
        assert task.spec == spec
        assert task.job and task.job.running
        assert task.base_commit == app.backend._description_commit(target)
        assert task.base_commit == job.base_commit
        assert (stored.tasks_dir / f"{task.id}.json").is_file()
        await wait_until(
            pilot, lambda: (app.jobs.refresh({}), not app.jobs.jobs[target].running)[1]
        )
        await app.refresh_data()
        app.jobs.clear(target)
        await app.refresh_data()
        stored.load()
        (finished,) = stored.get(target).history
        assert finished.id == task.id and finished.status == "completed"
        assert finished.result_commit == task.base_commit
        assert finished.job and any(p.is_file() for p in finished.job.output_files())
        if target == "feature/x":
            await pilot.press("j")
        await pilot.press("L")
        screen = await wait_for_screen(app, pilot, QueueScreen)
        assert screen.visible_ids() == [task.id]
        assert "completed" in [str(c) for c in screen.table.get_row(task.id)]

    run_app(repo, fake_cfg(), scenario)


@pytest.mark.parametrize("cli_flag", [True, False])
def test_dry_run_does_not_consume_pending_tasks(
    repo: Path, fake_cfg: Callable[..., Config], cli_flag: bool
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app.dry_run = cli_flag
        app.cfg.sandbox.extra_args.append("--dry-run")
        pending = app.queue.add(prompt("real pending task"))
        await app.refresh_data()
        assert not app.jobs.jobs
        assert prompts(app.queue) == ["real pending task"]
        app.queue.pause("feature/x", True)
        before = app.queue.get("feature/x").to_dict()
        await app.refresh_data()
        assert not app.jobs.jobs
        app._start_launch(prompt("sleep:0.1 preview"))
        await wait_until(pilot, lambda: not app.jobs.jobs["feature/x"].running)
        assert "--dry-run" in app.jobs.jobs["feature/x"].argv
        assert prompts(app.queue) == ["real pending task"]
        queue = app.queue.get("feature/x")
        assert queue.tasks[0].id == pending.id
        assert queue.paused
        assert queue.to_dict()["undo"] == before["undo"]
        assert len(queue.history) == 1
        assert queue.history[0].id == app.jobs.jobs["feature/x"].queue_id
        assert queue.history[0].description == "sleep:0.1 preview"

    run_app(repo, fake_cfg(), scenario)


def test_immediate_launch_failure_preserves_pending_queue(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    jobs = JobManager(repo, fake_cfg())
    queue = TaskQueue(jobs.state_dir)
    queue.add(prompt("real pending task"))
    queue.pause("feature/x", True)
    before = queue.path.read_bytes()
    reservation = jobs.record_external(LaunchSpec("feature/x", "claude"), ["external"])
    with pytest.raises(BusyError):
        queue.start(prompt("preview"), jobs, {})
    assert queue.path.read_bytes() == before
    assert prompts(queue) == ["real pending task"]
    assert not queue.get("feature/x").history
    assert jobs.jobs["feature/x"].started_at == reservation.started_at


def test_legacy_queue_migrates_to_individual_records(tmp_path: Path) -> None:
    import json

    from project_sandbox_tui.config import STATE_VERSION

    queue = TaskQueue(tmp_path)
    task_id = "a" * 32
    legacy = {"id": task_id, "spec": prompt("old prompt").to_dict()}
    queue.path.write_text(
        json.dumps(
            {
                "version": STATE_VERSION,
                "bookmarks": {
                    "feature/x": {
                        "tasks": [legacy],
                        "undo": [[]],
                        "marks": {"a": task_id},
                        "paused": True,
                    }
                },
            }
        )
    )
    assert prompts(queue) == ["old prompt"]
    queue.edit("feature/x", task_id, prompt("edited"))
    raw = json.loads(queue.path.read_text())
    assert raw["bookmarks"]["feature/x"]["tasks"] == [task_id]
    record = json.loads((queue.tasks_dir / f"{task_id}.json").read_text())
    assert record["spec"]["prompt_text"] == "edited"
    other = TaskQueue(tmp_path)
    other.undo("feature/x")
    assert prompts(other) == ["old prompt"]
    assert other.get("feature/x").paused
    assert other.get("feature/x").marks == {"a": task_id}


@pytest.mark.parametrize("broken", ["missing", "corrupt", "wrong-id"])
def test_bad_task_record_preserves_queue(tmp_path: Path, broken: str) -> None:
    import json

    queue = TaskQueue(tmp_path)
    task = queue.add(prompt("keep this"))
    before = queue.path.read_bytes()
    path = queue.tasks_dir / f"{task.id}.json"
    if broken == "missing":
        path.unlink()
    elif broken == "corrupt":
        path.write_text("{")
    else:
        raw = json.loads(path.read_text())
        raw["id"] = "b" * 32
        path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="cannot read task queue"):
        queue.add(prompt("should not be saved"))
    assert queue.path.read_bytes() == before


@pytest.mark.parametrize("fail_on", ["record", "index"])
def test_queue_recovers_partial_multifile_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_on: str,
) -> None:
    import project_sandbox_tui.queue as module

    queue = TaskQueue(tmp_path)
    task = queue.add(prompt("original"))
    write = module.write_state
    target = queue.path if fail_on == "index" else queue.tasks_dir / f"{task.id}.json"

    def fail(path: Path, data: Any, *, durable: bool = False) -> None:
        if path == target:
            raise OSError("disk full")
        write(path, data, durable=durable)

    with monkeypatch.context() as patch:
        patch.setattr(module, "write_state", fail)
        with pytest.raises(OSError, match="disk full"):
            queue.edit("feature/x", task.id, prompt("saved draft"))
    other = TaskQueue(tmp_path)
    assert prompts(other) == ["saved draft"]
    assert not other.journal.exists()
    other.undo("feature/x")
    assert prompts(other) == ["original"]


def test_finished_tasks_keep_logs_and_commits_until_deleted(
    repo: Path,
    fake_cfg: Callable[..., Config],
) -> None:
    jobs = JobManager(repo, fake_cfg())
    queue = TaskQueue(jobs.state_dir)
    first = queue.add(prompt("sleep:0.1 exit:3 first"))
    second = queue.add(prompt("sleep:0.1 second"))
    calls = []

    def commit(branch: str) -> str:
        calls.append(branch)
        return "base" if len(calls) == 1 else "result"

    queue.dispatch(jobs, lambda: {}, lambda _: None, commit)
    with pytest.raises(ValueError, match="running tasks"):
        queue.delete("feature/x", [first.id, second.id])
    queue.dispatch(jobs, lambda: {}, lambda _: None, commit)
    assert calls == ["feature/x"]  # a busy run must not pin the next task's base
    wait_for(lambda: bool(jobs.refresh({})))
    queue.refresh(jobs, lambda _: ("result", ["commit-one", "result"]))
    queue.dispatch(jobs, lambda: {}, lambda _: None, commit)
    wait_for(lambda: bool(jobs.refresh({})))
    queue.refresh(jobs)
    old = queue.get("feature/x").history[0]
    assert old.status == "failed" and old.job and old.job.exit_code == 3
    assert old.base_commit == "base" and old.result_commit == "result"
    assert old.commits == ["commit-one", "result"]
    assert queue.get("feature/x").history[1].base_commit == "result"
    assert old.job.output_files()[-1].read_text()
    # Retention is independent of the bounded job history and the latest run.
    for path in jobs.jobs_dir.rglob("*.json"):
        path.unlink()
    restored = TaskQueue(jobs.state_dir)
    restored.refresh(jobs)
    assert [t.status for t in restored.get("feature/x").history] == ["failed", "completed"]
    assert restored.get("feature/x").history[0].to_dict() == old.to_dict()
    restored.delete("feature/x", [first.id])
    restored.undo("feature/x")
    restored.undo("feature/x", redo=True)
    assert [t.id for t in restored.get("feature/x").entries] == [second.id]
    assert not (restored.tasks_dir / f"{first.id}.json").exists()
    assert restored.dispatch(jobs, lambda: {}, lambda _: None) == []


@pytest.mark.parametrize("save", [True, False])
def test_edit_pauses_dispatch_across_instances(
    repo: Path,
    fake_cfg: Callable[..., Config],
    save: bool,
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app._refresh_timer.pause()
        await app.refresh_data()
        task = app.queue.add(prompt("original"))
        await pilot.press("j", "L", "e")
        form = await wait_for_screen(app, pilot, LaunchScreen)
        other = TaskQueue(app.jobs.state_dir)
        other.load()
        assert other.get("feature/x").paused
        assert other.dispatch(app.jobs, lambda: {}, lambda _: None) == []
        form.query_one("#prompt", TextArea).text = "updated"
        await pilot.press("ctrl+s" if save else "escape")
        await wait_for_screen(app, pilot, QueueScreen)
        assert prompts(other) == ["updated" if save else "original"]
        assert other.get("feature/x").paused
        assert other.get("feature/x").tasks[0].id == task.id
        await pilot.press("p")
        other.load()
        assert not other.get("feature/x").paused

    run_app(repo, fake_cfg(), scenario)


def test_task_list_keeps_pinned_commits_and_opens_finished_logs(
    repo: Path,
    fake_cfg: Callable[..., Config],
) -> None:
    from project_sandbox_tui.screens.dialogs import MessageScreen
    from project_sandbox_tui.screens.log import LogScreen
    from project_sandbox_tui.screens.review import CommitReviewScreen
    from tests.conftest import jj

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app._refresh_timer.pause()
        await app.refresh_data()
        task = app.queue.add(prompt("sleep:0.1 tracked run"))
        job = app._dispatch_queue()[0]
        # Deliberately omit graph recording, as after a crash just after launch.
        base = job.base_commit
        jj(repo, "new", "feature/x")
        (repo / "produced.txt").write_text("first change\n")
        jj(repo, "commit", "-m", "first task commit")
        first = app.backend._jj("log", "--no-graph", "-r", "@-", "-T", "commit_id").strip()
        (repo / "produced.txt").write_text("second change\n")
        jj(repo, "commit", "-m", "second task commit")
        jj(repo, "bookmark", "set", "feature/x", "-r", "@-")
        result = app.backend._bookmark_commit("feature/x")
        wait_for(lambda: bool(app.jobs.refresh({})))
        await app.refresh_data()
        tracked = app.queue.get("feature/x").history[0]
        assert tracked.status == "completed"
        assert tracked.base_commit == base and tracked.result_commit == result
        assert tracked.commits == [first, result]
        # The next run and later bookmark changes cannot change this task's links.
        app.queue.add(prompt("sleep:0.1 later run"))
        app._dispatch_queue()
        jj(repo, "describe", "feature/x", "-m", "later rewritten commit")
        wait_for(lambda: bool(app.jobs.refresh({})))
        await app.refresh_data()
        assert app.queue.get("feature/x").history[0].commits == [first, result]
        await pilot.press("j", "L")
        screen = await wait_for_screen(app, pilot, QueueScreen)
        assert screen.table.row_count == 2
        screen.table.move_cursor(row=screen.visible_ids().index(task.id))
        assert "completed" in [str(c) for c in screen.table.get_row(task.id)]
        await pilot.press("enter")
        log = await wait_for_screen(app, pilot, LogScreen)
        assert log.job and log.job.status == "completed"
        assert log.current in job.output_files()
        await pilot.press("q", "P")
        details = await wait_for_screen(app, pilot, MessageScreen)
        assert first in details.body
        await pilot.press("q", "c")
        await wait_for_screen(app, pilot, CommitReviewScreen)
        await pilot.press("q", "d", "d")
        await wait_for_screen(app, pilot, QueueScreen)
        assert task.id not in screen.visible_ids()

    run_app(repo, fake_cfg(), scenario)


@pytest.mark.parametrize("journal", ['{"version": 999}', '{"version": 1}', "[]"])
def test_corrupt_recovery_journal_is_reported_without_overwriting(
    tmp_path: Path,
    journal: str,
) -> None:
    queue = TaskQueue(tmp_path)
    queue.add(prompt("keep this"))
    before = queue.path.read_bytes()
    queue.journal.write_text(journal)
    with pytest.raises(ValueError, match="cannot recover task queue"):
        queue.add(prompt("do not overwrite"))
    assert queue.path.read_bytes() == before
    assert queue.journal.read_text() == journal


def test_commit_lookup_failure_pauses_only_its_queue(
    repo: Path,
    fake_cfg: Callable[..., Config],
) -> None:
    from project_sandbox_tui.vcs import VcsError

    jobs = JobManager(repo, fake_cfg())
    queue = TaskQueue(jobs.state_dir)
    queue.add(prompt("sleep:0.1", "broken"))
    queue.add(prompt("sleep:0.1", "healthy"))

    def commit(branch: str) -> str:
        if branch == "broken":
            raise VcsError("ambiguous bookmark")
        return "base"

    started = queue.dispatch(jobs, lambda: {}, lambda _: None, commit)
    assert [j.branch for j in started] == ["healthy"]
    assert queue.get("broken").paused
    assert queue.get("broken").error == "ambiguous bookmark"
    assert prompts(queue, "broken") == ["sleep:0.1"]
    wait_for(lambda: bool(jobs.refresh({})))


def test_forgetting_running_job_leaves_deletable_task_history(
    repo: Path,
    fake_cfg: Callable[..., Config],
) -> None:
    jobs = JobManager(repo, fake_cfg())
    queue = TaskQueue(jobs.state_dir)
    task = queue.add(prompt("sleep:10"))
    queue.dispatch(jobs, lambda: {}, lambda _: None)
    log_source = queue.get("feature/x").history[0].job
    assert log_source and log_source.running
    jobs.clear("feature/x", force=True)
    queue.refresh(jobs)
    assert log_source.status == "orphaned"
    finished = log_source.ended_at
    queue.refresh(jobs)
    assert log_source.status == "orphaned" and log_source.ended_at == finished
    queue.delete("feature/x", [task.id])
    assert not queue.get("feature/x").entries


@pytest.mark.parametrize("abort", [False, True])
def test_queue_readers_see_only_committed_transactions(tmp_path: Path, abort: bool) -> None:
    queue = TaskQueue(tmp_path)
    queue.add(prompt("original"))
    editing, release = Event(), Event()

    def edit() -> None:
        with queue.transaction() as queues:
            launch = queues["feature/x"].tasks[0].spec
            assert launch is not None
            launch.prompt_text = "updated"
            queues["other"] = BookmarkQueue(paused=True)
            editing.set()
            assert release.wait(5)
            if abort:
                raise RuntimeError("abort edit")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(edit)
        try:
            assert editing.wait(5)
            assert queue.get("feature/x").tasks[0].description == "original"
            assert "other" not in queue.queues
        finally:
            release.set()
        if abort:
            with pytest.raises(RuntimeError, match="abort edit"):
                future.result(timeout=5)
        else:
            future.result(timeout=5)
    assert queue.get("feature/x").tasks[0].description == ("original" if abort else "updated")
    assert ("other" in queue.queues) is not abort
    assert prompts(TaskQueue(tmp_path)) == ["original" if abort else "updated"]


@pytest.mark.parametrize("failure", ["outcome", "validation"])
def test_bookmark_lookup_errors_do_not_block_other_queues(
    repo: Path, fake_cfg: Callable[..., Config], failure: str
) -> None:
    jobs = JobManager(repo, fake_cfg())
    queue = TaskQueue(jobs.state_dir)
    if failure == "outcome":
        queue.add(prompt("sleep:0.1", "broken"))
        queue.dispatch(jobs, lambda: {}, lambda _: None)
        wait_for(lambda: bool(jobs.refresh({})))
    queue.add(prompt("pending", "broken"))
    queue.add(prompt("sleep:0.1", "healthy"))

    def outcome(task: Any) -> tuple[str, list[str]] | None:
        raise VcsError("cannot resolve old revision")

    def valid(branch: str) -> str | None:
        if failure == "validation" and branch == "broken":
            raise VcsError("cannot resolve old revision")
        return None

    started = queue.dispatch(jobs, lambda: {}, valid, outcome=outcome)
    assert [j.branch for j in started] == ["healthy"]
    assert queue.get("broken").paused
    assert queue.get("broken").error == "cannot resolve old revision"
    assert prompts(queue, "broken") == ["pending"]
    if failure == "outcome":
        assert queue.get("broken").history[0].status == "completed"
    wait_for(lambda: bool(jobs.refresh({})))


@pytest.mark.parametrize("broken", ["wrong-branch", "duplicate-id"])
def test_recovery_validates_bookmark_invariants_before_writing(tmp_path: Path, broken: str) -> None:
    from project_sandbox_tui.config import STATE_VERSION

    queue = TaskQueue(tmp_path)
    task = queue.add(prompt("original"))
    task_path = queue.tasks_dir / f"{task.id}.json"
    index_before, task_before = queue.path.read_bytes(), task_path.read_bytes()
    bookmarks = {name: q.to_dict() for name, q in queue.queues.items()}
    bookmarks["feature/x"]["tasks"][0]["spec"]["prompt_text"] = "do not persist"
    if broken == "wrong-branch":
        bookmarks["other"] = bookmarks.pop("feature/x")
    else:
        bookmarks["feature/x"]["tasks"] *= 2
    journal = json.dumps({"version": STATE_VERSION, "bookmarks": bookmarks})
    queue.journal.write_text(journal)
    with pytest.raises(ValueError, match="cannot recover task queue"):
        queue.load()
    assert queue.path.read_bytes() == index_before
    assert task_path.read_bytes() == task_before
    assert queue.journal.read_text() == journal


def test_valid_journal_repairs_corrupt_task_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import project_sandbox_tui.queue as module

    queue = TaskQueue(tmp_path)
    task = queue.add(prompt("original"))
    write = module.write_state

    def fail(path: Path, data: Any, *, durable: bool = False) -> None:
        if path == queue.path:
            raise OSError("disk full")
        write(path, data, durable=durable)

    with monkeypatch.context() as patch:
        patch.setattr(module, "write_state", fail)
        with pytest.raises(OSError, match="disk full"):
            queue.edit("feature/x", task.id, prompt("recover this"))
    (queue.tasks_dir / f"{task.id}.json").write_text("{")
    assert prompts(queue) == ["recover this"]
    assert not queue.journal.exists()
