"""Durable, editable FIFO queues for unsupervised bookmark sessions.

Lock order is queue.lock -> jobs.lock. No caller holds a job or graph lock
while entering a queue transaction. Started IDs are removed from every undo
snapshot, so undo can never replay a session that has already started.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import uuid
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from project_sandbox_tui.config import STATE_VERSION, ConfigError, private_dir, write_state
from project_sandbox_tui.jobs import BusyError, JobManager, JobRecord
from project_sandbox_tui.sandbox import LaunchSpec, SessionRecord


@dataclass
class QueuedTask:
    id: str
    spec: LaunchSpec

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "spec": self.spec.to_dict()}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> QueuedTask:
        task = cls(str(raw["id"]), LaunchSpec.from_dict(raw["spec"]))
        if not task.id or task.spec.mode != "background" or not task.spec.prompt_text.strip():
            raise ValueError("invalid queued task")
        return task


@dataclass
class BookmarkQueue:
    tasks: list[QueuedTask] = field(default_factory=list)
    paused: bool = False
    error: str = ""
    undo: list[list[QueuedTask]] = field(default_factory=list)
    redo: list[list[QueuedTask]] = field(default_factory=list)
    marks: dict[str, str] = field(default_factory=dict)

    def checkpoint(self) -> None:
        self.undo.append(copy.deepcopy(self.tasks))
        self.undo = self.undo[-100:]
        self.redo.clear()

    def consume(self, task_id: str) -> None:
        self.tasks = [t for t in self.tasks if t.id != task_id]
        self.undo = [[t for t in snapshot if t.id != task_id] for snapshot in self.undo]
        self.redo = [[t for t in snapshot if t.id != task_id] for snapshot in self.redo]
        self.marks = {key: value for key, value in self.marks.items() if value != task_id}

    def to_dict(self) -> dict[str, Any]:
        return {
            "tasks": [t.to_dict() for t in self.tasks],
            "paused": self.paused,
            "error": self.error,
            "undo": [[t.to_dict() for t in snapshot] for snapshot in self.undo],
            "redo": [[t.to_dict() for t in snapshot] for snapshot in self.redo],
            "marks": self.marks,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> BookmarkQueue:
        return cls(
            tasks=[QueuedTask.from_dict(t) for t in raw.get("tasks", [])],
            paused=bool(raw.get("paused", False)),
            error=str(raw.get("error", "")),
            undo=[[QueuedTask.from_dict(t) for t in s] for s in raw.get("undo", [])],
            redo=[[QueuedTask.from_dict(t) for t in s] for s in raw.get("redo", [])],
            marks=dict(raw.get("marks", {})),
        )


class TaskQueue:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.path = state_dir / "queue.json"
        self.queues: dict[str, BookmarkQueue] = {}

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("version") != STATE_VERSION:
                raise ValueError("unsupported queue state version")
            queues = {name: BookmarkQueue.from_dict(q) for name, q in raw["bookmarks"].items()}
            for name, queue in queues.items():
                if any(
                    t.spec.branch != name
                    for s in [queue.tasks, *queue.undo, *queue.redo]
                    for t in s
                ):
                    raise ValueError("queued task belongs to another bookmark")
        except FileNotFoundError:
            queues = {}
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError(f"cannot read task queue {self.path}: {exc}") from exc
        self.queues = queues

    def get(self, branch: str) -> BookmarkQueue:
        return self.queues.get(branch, BookmarkQueue())

    @contextlib.contextmanager
    def transaction(self) -> Generator[dict[str, BookmarkQueue], None, None]:
        private_dir(self.state_dir)
        with (self.state_dir / "queue.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                self.load()
                queues = self.queues
                before = copy.deepcopy({name: q.to_dict() for name, q in queues.items()})
                yield queues
                after = {name: q.to_dict() for name, q in queues.items()}
                if after != before:
                    write_state(
                        self.path,
                        {"version": STATE_VERSION, "bookmarks": after},
                        durable=True,
                    )
                self.queues = queues
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def add(self, spec: LaunchSpec) -> QueuedTask:
        if spec.mode != "background" or not spec.prompt_text.strip():
            raise ValueError("only unsupervised tasks with a prompt can be queued")
        task = QueuedTask(uuid.uuid4().hex, copy.deepcopy(spec))
        with self.transaction() as queues:
            queue = queues.setdefault(spec.branch, BookmarkQueue())
            queue.checkpoint()
            queue.tasks.append(task)
        return task

    def edit(
        self, branch: str, task_id: str, spec: LaunchSpec, *, expected: LaunchSpec | None = None
    ) -> None:
        if spec.branch != branch or spec.mode != "background" or not spec.prompt_text.strip():
            raise ValueError("an edited task must remain unsupervised and on the same bookmark")
        with self.transaction() as queues:
            queue = queues.setdefault(branch, BookmarkQueue())
            task = next((t for t in queue.tasks if t.id == task_id), None)
            if task is None:
                raise ValueError("task already started or was deleted")
            if expected is not None and task.spec != expected:
                raise ValueError(
                    "task changed in another editor; reopen it to edit the latest version"
                )
            queue.checkpoint()
            task.spec = copy.deepcopy(spec)

    def delete(self, branch: str, ids: list[str]) -> None:
        with self.transaction() as queues:
            queue = queues.setdefault(branch, BookmarkQueue())
            if any(t.id in ids for t in queue.tasks):
                queue.checkpoint()
                queue.tasks = [t for t in queue.tasks if t.id not in ids]

    def move(self, branch: str, ids: list[str], step: int) -> None:
        with self.transaction() as queues:
            queue = queues.setdefault(branch, BookmarkQueue())
            before = list(queue.tasks)
            indices = range(len(before)) if step < 0 else reversed(range(len(before)))
            after = list(before)
            for i in indices:
                other = i + step
                if after[i].id in ids and 0 <= other < len(after) and after[other].id not in ids:
                    after[i], after[other] = after[other], after[i]
            if after != before:
                queue.checkpoint()
                queue.tasks = after

    def undo(self, branch: str, *, redo: bool = False) -> None:
        with self.transaction() as queues:
            queue = queues.setdefault(branch, BookmarkQueue())
            source, target = (queue.redo, queue.undo) if redo else (queue.undo, queue.redo)
            # Dispatch may have made old snapshots identical to the current list.
            while source and source[-1] == queue.tasks:
                source.pop()
            if source:
                target.append(copy.deepcopy(queue.tasks))
                queue.tasks = source.pop()

    def pause(self, branch: str, paused: bool) -> None:
        with self.transaction() as queues:
            queue = queues.setdefault(branch, BookmarkQueue())
            queue.paused = paused
            if not paused:
                queue.error = ""

    def mark(self, branch: str, key: str, task_id: str) -> None:
        with self.transaction() as queues:
            queue = queues.setdefault(branch, BookmarkQueue())
            if any(t.id == task_id for t in queue.tasks):
                queue.marks[key] = task_id

    def dispatch(
        self,
        jobs: JobManager,
        sessions: Callable[[], dict[str, SessionRecord]],
        valid: Callable[[str], str | None],
    ) -> list[JobRecord]:
        """Start at most one task per idle bookmark; failures pause that queue.

        The job's queue_id makes retrying after a crash between job persistence
        and queue persistence idempotent, including after that job has finished.
        """
        started: list[JobRecord] = []
        with self.transaction() as queues:
            for branch, queue in queues.items():
                if queue.paused or not queue.tasks:
                    continue
                task = queue.tasks[0]
                if error := valid(branch):
                    queue.paused, queue.error = True, error
                    continue
                try:
                    job = jobs.start_background(task.spec, sessions(), queue_id=task.id)
                except BusyError:
                    continue
                except (OSError, ConfigError, ValueError) as exc:
                    queue.paused, queue.error = True, str(exc)
                    continue
                queue.consume(task.id)
                started.append(job)
        return started
