from __future__ import annotations

import os
import time
from pathlib import Path

from project_sandbox_tui.tasks import KEEP, TaskManager
from tests.conftest import wait_for


def start(manager: TaskManager, tmp_path: Path, script: str, bookmark: str = "feature/x"):
    return manager.start(bookmark, f"*!{script}", script, tmp_path, dict(os.environ))


def finished(manager: TaskManager, task_id: str) -> bool:
    manager.refresh()
    return not manager.tasks[task_id].running


def test_task_runs_in_its_directory_and_records_its_status(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path / "state")
    ok = start(manager, tmp_path, "pwd; echo out; echo err >&2")
    bad = start(manager, tmp_path, "exit 3", bookmark="fix/y")
    assert manager.running_count() == 2
    wait_for(lambda: finished(manager, ok.id) and finished(manager, bad.id))
    assert (ok.status, ok.exit_code) == ("completed", 0)
    assert Path(ok.out_path).read_text() == f"{tmp_path}\nout\nerr\n"
    assert (bad.status, bad.exit_code) == ("failed", 3)
    assert ok.name == "pwd;" and bad.name == "exit"

    # Finished tasks are reported until claimed, once across managers.
    other = TaskManager(tmp_path / "state")
    assert {t.id for t in other.refresh()} == {ok.id, bad.id}
    assert other.mark_notified(other.tasks[ok.id])
    assert not manager.mark_notified(ok)
    assert [t.id for t in manager.refresh()] == [bad.id]


def test_adopted_task_reads_the_exit_file(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path / "state")
    task = start(manager, tmp_path, "sleep 0.3; exit 4")
    later = TaskManager(tmp_path / "state")  # a new TUI: not the parent process
    assert later.tasks[task.id].running
    wait_for(lambda: finished(later, task.id))
    assert later.tasks[task.id].exit_code == 4
    manager.refresh()  # reap the child


def test_task_without_exit_file_is_orphaned(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path / "state")
    task = start(manager, tmp_path, "true")
    wait_for(lambda: finished(manager, task.id))
    task.status, task.exit_code = "running", None
    manager.save(task)
    Path(task.exit_path).unlink()
    later = TaskManager(tmp_path / "state")
    later.refresh()
    assert later.tasks[task.id].status == "orphaned"


def test_cancel_escalates(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path / "state")
    task = start(manager, tmp_path, "sleep 30")
    time.sleep(0.2)  # let the shells start
    assert manager.cancel("feature/x") == ["feature/x: sent SIGINT to :*!sleep 30"]
    assert manager.cancel_started("feature/x")
    wait_for(lambda: finished(manager, task.id))
    assert task.status == "interrupted"
    assert manager.cancel("feature/x") == []  # nothing running


def test_cancel_marks_a_killed_task_interrupted(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path / "state")
    # The command ignores SIGINT and SIGTERM, as a stuck test run might.
    task = start(manager, tmp_path, "trap '' INT TERM; sleep 30")
    time.sleep(0.2)
    for stage in range(3):
        manager._stop_last.clear()  # skip the double-press debounce
        manager.cancel("feature/x")
        if stage < 2:
            assert task.running
    wait_for(lambda: finished(manager, task.id))
    assert task.status == "interrupted"


def test_old_tasks_are_pruned(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path / "state")
    tasks = []
    for _ in range(KEEP + 2):
        tasks.append(start(manager, tmp_path, "true"))
        wait_for(lambda: finished(manager, tasks[-1].id))
    start(manager, tmp_path, "true", bookmark="other")
    kept = manager.for_bookmark("feature/x")
    assert [t.id for t in kept] == [t.id for t in reversed(tasks)][:KEEP]
    assert not Path(tasks[0].out_path).exists()
    assert manager.latest("other") is not None
