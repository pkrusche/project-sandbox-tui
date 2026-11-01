from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from typing import Any

import pytest

from project_sandbox_tui.tasks import KEEP, TaskManager, TaskRecord
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
    # The status is renamed into place, so readers never see it half-written.
    assert Path(ok.exit_path).read_text() == "0"
    assert not Path(f"{ok.exit_path}.tmp").exists()
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


def test_adopted_task_with_an_empty_exit_file_is_still_running(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path / "state")
    task = start(manager, tmp_path, "sleep 30")
    # What an older wrapper's `printf >file` leaves between creating and writing it.
    Path(task.exit_path).write_text("")
    later = TaskManager(tmp_path / "state")
    later.refresh()
    assert later.tasks[task.id].running
    os.killpg(task.pid, signal.SIGKILL)
    wait_for(lambda: finished(manager, task.id))


def test_task_finished_by_another_manager_is_reaped(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path / "state")
    task = start(manager, tmp_path, "true")
    proc = manager._procs[task.id]
    later = TaskManager(tmp_path / "state")  # another TUI, or a cleanup scan
    wait_for(lambda: finished(later, task.id))
    manager.refresh()
    assert task.id not in manager._procs and proc.returncode is not None


def _rewrite(manager: TaskManager, task: TaskRecord, **change: Any) -> None:
    path = manager._record_path(task.id)
    raw = json.loads(path.read_text())
    path.write_text(json.dumps({**raw, "status": "running", **change}))


@pytest.mark.parametrize(
    "change",
    [
        {"pid": 0},  # killpg(0) would signal the TUI's own process group
        {"pid": -1},
        {"pid": True},
        {"pid": 2**31},
        {"pid": "123"},
        {"started_at": 5},
        {"exit_path": None},
        {"id": "other-id"},  # must name its own record file
    ],
)
def test_invalid_task_records_are_ignored(tmp_path: Path, change: dict[str, Any]) -> None:
    manager = TaskManager(tmp_path / "state")
    task = start(manager, tmp_path, "true")
    wait_for(lambda: finished(manager, task.id))
    _rewrite(manager, task, **change)
    later = TaskManager(tmp_path / "state")
    assert later.tasks == {}
    assert later.cancel("feature/x") == []


def test_task_record_fields_are_normalised(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path / "state")
    task = start(manager, tmp_path, "true")
    wait_for(lambda: finished(manager, task.id))
    _rewrite(manager, task, status="bogus", exit_code=1.5, notified="false", ended_at=None)
    loaded = TaskManager(tmp_path / "state").tasks[task.id]
    assert (loaded.status, loaded.exit_code, loaded.notified, loaded.ended_at) == (
        "orphaned",
        None,
        False,
        "",
    )


def test_load_swaps_in_a_complete_dict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """refresh() loads in a worker thread while the UI iterates the tasks."""
    manager = TaskManager(tmp_path / "state")
    for bookmark in ("a", "b"):
        task = start(manager, tmp_path, "true", bookmark=bookmark)
        wait_for(lambda task=task: finished(manager, task.id))
    old = manager.tasks
    during: list[bool] = []
    real = TaskRecord.from_dict

    def spy(raw: Any) -> TaskRecord:
        during.append(manager.tasks is old)
        return real(raw)

    monkeypatch.setattr(TaskRecord, "from_dict", spy)
    manager.load()
    assert during == [True, True]
    assert manager.tasks is not old and len(manager.tasks) == 2
