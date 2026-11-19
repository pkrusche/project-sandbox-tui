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
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Any

from project_sandbox_tui import feedback
from project_sandbox_tui.config import (
    STATE_VERSION,
    ConfigError,
    Feedback,
    private_dir,
    write_state,
)
from project_sandbox_tui.jobs import BusyError, JobManager, JobRecord, SessionSource, _now
from project_sandbox_tui.sandbox import LaunchSpec, SessionRecord
from project_sandbox_tui.vcs import VcsError, detect, main_branch


@dataclass
class WorkflowRun:
    name: str
    action: str
    next_steps: list[tuple[str, LaunchSpec]]
    pass_state_keys: list[str]
    result_state_keys: list[str]
    state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "action": self.action,
            "next_steps": [(name, spec.to_dict()) for name, spec in self.next_steps],
            "pass_state_keys": self.pass_state_keys,
            "result_state_keys": self.result_state_keys,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any], branch: str) -> WorkflowRun:
        steps = [(name, LaunchSpec.from_dict(spec)) for name, spec in raw["next_steps"]]
        workflow = cls(
            raw["name"],
            raw["action"],
            steps,
            raw["pass_state_keys"],
            raw["result_state_keys"],
            raw["state"],
        )
        workflow.validate(branch)
        return workflow

    def validate(self, branch: str) -> None:
        if not isinstance(self.name, str) or not isinstance(self.action, str):
            raise ValueError("invalid workflow name or action")
        if len(self.next_steps) > 99 or any(
            not isinstance(name, str)
            or spec.branch != branch
            or spec.mode != "background"
            or not spec.prompt_text.strip()
            for name, spec in self.next_steps
        ):
            raise ValueError("invalid workflow step")
        if not isinstance(self.state, dict) or any(
            not isinstance(keys, list) or not all(isinstance(v, str) for v in keys)
            for keys in (self.pass_state_keys, self.result_state_keys)
        ):
            raise ValueError("invalid workflow state")


_WORKFLOW_STOPPED = "workflow stopped"
_WORKFLOW_BUSY = (
    "{branch}: a workflow is already in progress; let it finish, "
    "or delete its queued or stopped step to abandon it"
)


@dataclass
class QueuedTask:
    id: str
    spec: LaunchSpec
    job: JobRecord | None = None
    base_commit: str = ""
    result_commit: str = ""
    commits: list[str] = field(default_factory=list)

    parent_task: str = ""
    attempt: int = 1
    step: int = 1
    decision_done: bool = False
    successor_id: str = ""
    decision: str = ""
    workflow: WorkflowRun | None = None

    @property
    def status(self) -> str:
        return self.job.status if self.job else "pending"

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "spec": self.spec.to_dict(),
            "job": self.job.to_dict() if self.job else None,
            "base_commit": self.base_commit,
            "result_commit": self.result_commit,
            "commits": self.commits,
            "parent_task": self.parent_task,
            "attempt": self.attempt,
            "step": self.step,
            "decision_done": self.decision_done,
            "successor_id": self.successor_id,
            "decision": self.decision,
        }
        if self.workflow:
            data["workflow"] = self.workflow.to_dict()
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> QueuedTask:
        task = cls(
            str(raw["id"]),
            LaunchSpec.from_dict(raw["spec"]),
            JobRecord.from_dict(raw["job"]) if raw.get("job") else None,
            str(raw.get("base_commit", "")),
            str(raw.get("result_commit", "")),
            list(raw.get("commits", [])),
            str(raw.get("parent_task", "")),
            raw.get("attempt", 1),
            raw.get("step", 1),
            raw.get("decision_done") is True,
            str(raw.get("successor_id", "")),
            str(raw.get("decision", "")),
        )
        if "workflow" in raw:
            task.workflow = WorkflowRun.from_dict(raw["workflow"], task.spec.branch)
        if (
            type(task.attempt) is not int
            or not 1 <= task.attempt <= 100
            or type(task.step) is not int
            or not 1 <= task.step <= 100
            or not task.id
            or any(c not in "0123456789abcdef" for c in task.id)
            or (task.spec.mode != "background" and (not task.workflow or not task.job))
            or not task.spec.prompt_text.strip()
            or (task.job and (task.job.queue_id != task.id or task.job.branch != task.spec.branch))
            or not all(isinstance(c, str) for c in task.commits)
        ):
            raise ValueError("invalid queued task")
        return task


@dataclass
class BookmarkQueue:
    tasks: list[QueuedTask] = field(default_factory=list)
    history: list[QueuedTask] = field(default_factory=list)
    paused: bool = False
    error: str = ""
    undo: list[list[QueuedTask]] = field(default_factory=list)
    redo: list[list[QueuedTask]] = field(default_factory=list)
    marks: dict[str, str] = field(default_factory=dict)

    @property
    def entries(self) -> list[QueuedTask]:
        """Pending tasks in FIFO order, followed by runs newest first."""
        return self.tasks + list(reversed(self.history))

    def real_runs(self) -> Iterator[QueuedTask]:
        """Yield retained runs newest first, skipping dry-run previews."""
        return (
            task for task in reversed(self.history) if task.job and "--dry-run" not in task.job.argv
        )

    @property
    def predecessor(self) -> QueuedTask | None:
        return next(self.real_runs(), None)

    def workflow_in_progress(self) -> bool:
        """A workflow step is pending, running, awaiting its decision, or stopped while paused."""
        task = self.predecessor
        return any(t.workflow for t in self.tasks) or bool(
            task
            and task.workflow
            and (
                (task.job and task.job.running)
                or not task.decision_done
                or (self.paused and task.decision == _WORKFLOW_STOPPED)
            )
        )

    def needs_decision(self, policy: Feedback) -> bool:
        task = self.predecessor
        return bool(
            task
            and (task.workflow or policy.makes_decisions)
            and task.job
            and not task.job.running
            and not task.decision_done
        )

    def checkpoint(self) -> None:
        self.undo.append(copy.deepcopy(self.tasks))
        self.undo = self.undo[-100:]
        self.redo.clear()

    def consume(self, task_id: str) -> None:
        self.tasks = [t for t in self.tasks if t.id != task_id]
        self.undo = [[t for t in snapshot if t.id != task_id] for snapshot in self.undo]
        self.redo = [[t for t in snapshot if t.id != task_id] for snapshot in self.redo]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tasks": [t.to_dict() for t in self.tasks],
            "history": [t.to_dict() for t in self.history],
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
            history=[QueuedTask.from_dict(t) for t in raw.get("history", [])],
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
        self.tasks_dir = state_dir / "queue-tasks"
        self.journal = state_dir / "queue-transaction.json"
        self.queues: dict[str, BookmarkQueue] = {}

    def load(self) -> None:
        with self._locked():
            self._recover()
            self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("version") != STATE_VERSION:
                raise ValueError("unsupported queue state version")
            if raw.get("format", 1) not in (1, 2):
                raise ValueError("unsupported queue storage format")
            if raw.get("format") == 2:
                for q in raw["bookmarks"].values():
                    for key in ("tasks", "history"):
                        records = []
                        for task_id in q.get(key, []):
                            if (
                                not isinstance(task_id, str)
                                or not task_id
                                or any(c not in "0123456789abcdef" for c in task_id)
                            ):
                                raise ValueError("invalid task id")
                            record = json.loads(
                                (self.tasks_dir / f"{task_id}.json").read_text(encoding="utf-8")
                            )
                            if record["id"] != task_id:
                                raise ValueError("task id does not match its file")
                            records.append(record)
                        q[key] = records
            queues = self._decode_bookmarks(raw["bookmarks"])
        except FileNotFoundError as exc:
            if self.path.exists():
                raise ValueError(f"cannot read task queue {self.path}: {exc}") from exc
            queues = {}
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError(f"cannot read task queue {self.path}: {exc}") from exc
        self._publish(queues)

    @staticmethod
    def _decode_bookmarks(raw: dict[str, Any]) -> dict[str, BookmarkQueue]:
        queues = {name: BookmarkQueue.from_dict(q) for name, q in raw.items()}
        live_ids: set[str] = set()
        for name, queue in queues.items():
            for snapshot in [queue.entries, *queue.undo, *queue.redo]:
                if any(t.spec.branch != name for t in snapshot):
                    raise ValueError("queued task belongs to another bookmark")
                if len({t.id for t in snapshot}) != len(snapshot):
                    raise ValueError("duplicate queued task id")
            for task in queue.entries:
                if task.id in live_ids:
                    raise ValueError("duplicate queued task id")
                live_ids.add(task.id)
        return queues

    def _publish(self, queues: dict[str, BookmarkQueue]) -> None:
        # Keep log viewers' job references current without changing an editor's
        # saved spec (its optimistic concurrency check needs the original).
        previous = {t.id: t for q in self.queues.values() for t in q.history}
        for queue in queues.values():
            for task in queue.history:
                old = previous.get(task.id)
                if old and old.job and task.job:
                    old.job.__dict__.update(task.job.__dict__)
                    task.job = old.job
        self.queues = queues

    def get(self, branch: str) -> BookmarkQueue:
        return self.queues.get(branch, BookmarkQueue())

    @contextlib.contextmanager
    def _locked(self) -> Generator[None, None, None]:
        private_dir(self.state_dir)
        with (self.state_dir / "queue.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _persist(self, data: dict[str, Any]) -> None:
        """Materialize a journaled transaction, then commit its index."""
        private_dir(self.tasks_dir)
        index = copy.deepcopy(data)
        index["format"] = 2
        live = set()
        for queue in index["bookmarks"].values():
            for key in ("tasks", "history"):
                records = queue.get(key, [])
                for record in records:
                    task = QueuedTask.from_dict(record)
                    path = self.tasks_dir / f"{task.id}.json"
                    live.add(path)
                    try:
                        previous = json.loads(path.read_text(encoding="utf-8"))
                    except (FileNotFoundError, ValueError):
                        previous = None
                    if previous != record:
                        write_state(path, record, durable=True)
                queue[key] = [record["id"] for record in records]
        write_state(self.path, index, durable=True)
        for path in self.tasks_dir.glob("*.json"):
            if path not in live:
                path.unlink()
        self.journal.unlink(missing_ok=True)

    def _recover(self) -> None:
        if self.journal.exists():
            try:
                data = json.loads(self.journal.read_text(encoding="utf-8"))
                if data.get("version") != STATE_VERSION:
                    raise ValueError("unsupported queue transaction version")
                # Validate the entire transaction before touching any records.
                self._decode_bookmarks(data["bookmarks"])
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise ValueError(f"cannot recover task queue {self.journal}: {exc}") from exc
            self._persist(data)

    @contextlib.contextmanager
    def transaction(self) -> Generator[dict[str, BookmarkQueue], None, None]:
        """Publish a complete saved snapshot; failed edits remain private."""
        with self._locked():
            self._recover()
            self._load()
            queues = copy.deepcopy(self.queues)
            before = copy.deepcopy({name: q.to_dict() for name, q in queues.items()})
            yield queues
            after = {name: q.to_dict() for name, q in queues.items()}
            if after != before:
                data = {"version": STATE_VERSION, "bookmarks": after}
                write_state(self.journal, data, durable=True)
                self._persist(data)
            self._publish(queues)

    @staticmethod
    def _new_task(spec: LaunchSpec) -> QueuedTask:
        if spec.mode != "background" or not spec.prompt_text.strip():
            raise ValueError("only unsupervised tasks with a prompt can be queued")
        return QueuedTask(uuid.uuid4().hex, copy.deepcopy(spec))

    def add(self, spec: LaunchSpec, *, workflow: WorkflowRun | None = None) -> QueuedTask:
        if workflow:
            workflow.validate(spec.branch)
        task = self._new_task(spec)
        task.workflow = workflow
        with self.transaction() as queues:
            queue = queues.setdefault(spec.branch, BookmarkQueue())
            if workflow and queue.workflow_in_progress():
                raise BusyError(_WORKFLOW_BUSY.format(branch=spec.branch))
            queue.checkpoint()
            queue.tasks.append(task)
        return task

    def start(
        self,
        spec: LaunchSpec,
        jobs: JobManager,
        sessions: SessionSource,
        commit: Callable[[], str] | None = None,
    ) -> JobRecord:
        """Retain an immediate run without dispatching or editing pending tasks."""
        task = self._new_task(spec)
        with self.transaction() as queues:
            job = jobs.start_background(spec, sessions, queue_id=task.id, base_commit=commit)
            task.job = copy.deepcopy(job)
            task.base_commit = job.base_commit
            queues.setdefault(spec.branch, BookmarkQueue()).history.append(task)
        return job

    @contextlib.contextmanager
    def start_workflow(
        self, spec: LaunchSpec, workflow: WorkflowRun, jobs: JobManager
    ) -> Generator[QueuedTask, None, None]:
        """Register the interactive reservation before handing it a terminal."""
        workflow.validate(spec.branch)
        if spec.mode != "interactive" or not spec.prompt_text.strip() or not workflow.next_steps:
            raise ValueError(
                "a workflow needs an interactive plan and at least one background step"
            )
        with self.transaction() as queues:
            self._refresh(queues, jobs)
            queue = queues.setdefault(spec.branch, BookmarkQueue())
            previous = queue.predecessor
            if queue.workflow_in_progress():
                raise BusyError(_WORKFLOW_BUSY.format(branch=spec.branch))
            if (previous and previous.job and previous.job.running) or queue.needs_decision(
                jobs.cfg.feedback
            ):
                raise BusyError(f"{spec.branch}: finish or resume the current queue first")
            task = QueuedTask(uuid.uuid4().hex, spec, workflow=workflow)
            yield task
            assert task.job is not None
            task.job = copy.deepcopy(task.job)
            task.base_commit = task.job.base_commit
            queue.history.append(task)

    def discard_workflow_start(self, job: JobRecord, jobs: JobManager) -> None:
        """Roll back the interactive reservation when terminal handoff fails."""
        with self.transaction() as queues:
            jobs.discard(job)
            queue = queues.get(job.branch)
            if queue:
                queue.history = [task for task in queue.history if task.id != job.queue_id]
                queue.marks = {
                    key: value for key, value in queue.marks.items() if value != job.queue_id
                }

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
            if any(t.id in ids and t.status == "running" for t in queue.history):
                raise ValueError("running tasks cannot be deleted; cancel the run first")
            if any(t.id in ids for t in queue.tasks):
                queue.checkpoint()
                queue.tasks = [t for t in queue.tasks if t.id not in ids]
            queue.history = [t for t in queue.history if t.id not in ids]
            queue.marks = {k: v for k, v in queue.marks.items() if v not in ids}

    def move(self, branch: str, ids: list[str], step: int) -> None:
        with self.transaction() as queues:
            queue = queues.setdefault(branch, BookmarkQueue())
            before = list(queue.tasks)
            indices = range(len(before)) if step < 0 else reversed(range(len(before)))
            after = list(before)
            for i in indices:
                other = i + step
                if after[i].id in ids and 0 <= other < len(after) and after[other].id not in ids:
                    if any(task.workflow and task.parent_task for task in (after[i], after[other])):
                        continue
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
            task = queue.predecessor
            if (
                not paused
                and queue.paused
                and task
                and task.workflow
                and task.decision == _WORKFLOW_STOPPED
                and task.spec.mode == "background"
            ):
                if task.attempt >= 100:
                    raise ValueError("workflow retry limit reached")
                retry = self._new_task(task.spec)
                retry.attempt, retry.step = task.attempt + 1, task.step
                retry.workflow = task.workflow
                self._insert_successor(queue, task, retry, "workflow retry")
            queue.paused = paused
            if not paused:
                queue.error = ""

    def mark(self, branch: str, key: str, task_id: str) -> None:
        with self.transaction() as queues:
            queue = queues.setdefault(branch, BookmarkQueue())
            if any(t.id == task_id for t in queue.entries):
                queue.marks[key] = task_id

    def refresh(
        self,
        jobs: JobManager,
        outcome: Callable[[QueuedTask], tuple[str, list[str]] | None] | None = None,
    ) -> None:
        """Retain the exact run and its pinned commits, independently of job pruning."""
        with self.transaction() as queues:
            self._refresh(queues, jobs, outcome)

    def _refresh(
        self,
        queues: dict[str, BookmarkQueue],
        jobs: JobManager,
        outcome: Callable[[QueuedTask], tuple[str, list[str]] | None] | None = None,
    ) -> None:
        with jobs._locked():
            jobs.load()
            for branch, queue in queues.items():
                current = jobs.jobs.get(branch)
                records = jobs.history(branch) + ([current] if current else [])
                by_id = {job.queue_id: job for job in records if job.queue_id}
                for task in queue.history:
                    job = by_id.get(task.id)
                    if job and (task.job is None or task.job.running or not job.running):
                        if task.job:
                            task.job.__dict__.update(copy.deepcopy(job.__dict__))
                        else:
                            task.job = copy.deepcopy(job)
                    if (
                        task.job
                        and task.job.running
                        and (current is None or current.queue_id != task.id)
                    ):
                        # Force-forgetting a run archives its last (running)
                        # snapshot. It must not remain running forever here.
                        task.job.status = "orphaned"
                        task.job.ended_at = _now()
                    if task.job and not task.job.running and not task.result_commit and outcome:
                        try:
                            result = outcome(task)
                        except (OSError, ValueError, VcsError) as exc:
                            queue.paused, queue.error = True, str(exc)
                            continue
                        if result:
                            task.result_commit, task.commits = result

    @staticmethod
    def _insert_successor(
        queue: BookmarkQueue, task: QueuedTask, successor: QueuedTask, decision: str
    ) -> None:
        successor.parent_task = task.id
        task.successor_id = successor.id
        task.decision = decision
        queue.tasks.insert(0, successor)
        if successor.workflow:
            for snapshot in (*queue.undo, *queue.redo):
                snapshot.insert(0, copy.deepcopy(successor))

    def _decide_workflow(self, queue: BookmarkQueue, task: QueuedTask, job: JobRecord) -> None:
        workflow = task.workflow
        assert workflow is not None
        report_state = (job.feedback_snapshot or {}).get("state", {})
        planning = task.step == 1
        keys = workflow.pass_state_keys if planning else []
        if not workflow.next_steps:
            keys = workflow.result_state_keys
        missing = [key for key in keys if key not in report_state]
        if not job.reported_success or missing:
            detail = f"{job.status}; {job.feedback_detail()}"
            if job.reported_success:
                detail = "missing report state: " + ", ".join(missing)
            queue.paused = True
            queue.error = f"{workflow.name} / {workflow.action}: {detail}"
            task.decision = _WORKFLOW_STOPPED
            return
        if not workflow.next_steps:
            task.decision = "workflow complete"
            return
        (name, spec), *remaining = workflow.next_steps
        successor = self._new_task(spec)
        successor.step = task.step + 1
        successor.workflow = replace(
            workflow,
            action=name,
            next_steps=remaining,
            state={key: report_state[key] for key in keys} if planning else workflow.state,
        )
        self._insert_successor(queue, task, successor, f"workflow step {name}")

    def _decide(self, jobs: JobManager) -> None:
        """Commit successors before launching them; never replay a decision."""
        cfg = jobs.cfg
        policy = cfg.feedback
        with self.transaction() as queues:
            if not policy.makes_decisions and not any(
                (task := queue.predecessor) and task.workflow and not task.decision_done
                for queue in queues.values()
            ):
                return
            self._refresh(queues, jobs)
            for queue in queues.values():
                if queue.paused or not queue.needs_decision(policy):
                    continue
                task = queue.predecessor
                assert task is not None
                job = task.job
                assert job is not None
                task.decision_done = True
                if task.workflow:
                    self._decide_workflow(queue, task, job)
                    continue
                result = job.reported_outcome
                successor = None
                if job.exit_code == 0 and result in policy.retry_outcomes:
                    if task.attempt < policy.max_attempts:
                        successor = self._new_task(task.spec)
                        successor.attempt, successor.step = task.attempt + 1, task.step
                        task.decision = "retry"
                    else:
                        queue.paused, queue.error = True, "feedback retry limit reached"
                        task.decision = queue.error
                elif job.exit_code == 0 and result in feedback.OUTCOMES:
                    action_name = getattr(policy, f"on_{result}")
                    if action_name:
                        if task.step >= policy.max_steps:
                            queue.paused, queue.error = True, "feedback workflow step limit reached"
                            task.decision = queue.error
                        else:
                            action = cfg.actions[action_name]
                            spec = copy.deepcopy(task.spec)
                            main = cfg.project.main_branch
                            if "{main}" in action.prompt and not main:
                                try:
                                    branches = detect(jobs.repo).list_branches()
                                except (VcsError, OSError) as exc:
                                    queue.paused, queue.error = True, str(exc)
                                    task.decision_done = False  # retry the lookup on resume
                                    continue
                                main = main_branch([b.name for b in branches]) or ""
                            spec.prompt_text = action.render(spec.branch, main)
                            for key in ("agent", "model", "effort"):
                                if value := getattr(action, key):
                                    setattr(spec, key, value)
                            if action.timeout is not None:
                                spec.timeout = action.timeout
                            successor = self._new_task(spec)
                            successor.step = task.step + 1
                            task.decision = f"action {action_name}"
                if successor:
                    self._insert_successor(queue, task, successor, task.decision)
                elif (
                    policy.require_reported_success
                    and not job.reported_success
                    and not queue.paused
                ):
                    queue.paused = True
                    queue.error = f"feedback policy paused: {job.status}; {job.feedback_detail()}"
                    task.decision = queue.error
                elif not task.decision:
                    task.decision = "continue"

    @staticmethod
    def _input_state(queue: BookmarkQueue, task: QueuedTask, jobs: JobManager) -> dict[str, Any]:
        if task.workflow:
            return task.workflow.state
        keys = jobs.cfg.feedback.pass_state_keys
        if not keys:
            return {}
        if task.parent_task:
            source = next((t for t in queue.history if t.id == task.parent_task), None)
        else:
            source = next((t for t in queue.real_runs() if not t.workflow), None)
            if not source:
                return {}
        job = source.job if source else None
        if not job or job.running or job.reported_outcome == "unknown" or not job.feedback_snapshot:
            raise ValueError("state handoff requires a valid final predecessor report")
        state = job.feedback_snapshot.get("state", {})
        missing = [key for key in keys if key not in state]
        if missing:
            raise ValueError("predecessor state is missing keys: " + ", ".join(missing))
        selected = {key: state[key] for key in keys}
        feedback.encode(selected)
        return selected

    def dispatch(
        self,
        jobs: JobManager,
        sessions: Callable[[], dict[str, SessionRecord]],
        valid: Callable[[str], str | None],
        commit: Callable[[str], str] | None = None,
        outcome: Callable[[QueuedTask], tuple[str, list[str]] | None] | None = None,
    ) -> list[JobRecord]:
        """Start at most one task per idle bookmark; failures pause that queue.

        The job's queue_id makes retrying after a crash between job persistence
        and queue persistence idempotent, including after that job has finished.
        """
        self._decide(jobs)
        started: list[JobRecord] = []
        with self.transaction() as queues:
            self._refresh(queues, jobs, outcome)
            for branch, queue in queues.items():
                if queue.paused or not queue.tasks:
                    continue
                task = queue.tasks[0]
                if queue.history and queue.history[-1].job and queue.history[-1].job.running:
                    continue
                # Another instance can finalize a predecessor after _decide's
                # transaction. Defer until its policy decision is durable.
                if queue.needs_decision(jobs.cfg.feedback):
                    continue
                try:
                    if error := valid(branch):
                        queue.paused, queue.error = True, error
                        continue
                    job = jobs.start_background(
                        task.spec,
                        sessions,
                        queue_id=task.id,
                        base_commit=partial(commit, branch) if commit else None,
                        previous_state=self._input_state(queue, task, jobs),
                        require_feedback=task.workflow is not None,
                    )
                except BusyError:
                    continue
                except (OSError, ConfigError, ValueError, VcsError) as exc:
                    queue.paused, queue.error = True, str(exc)
                    continue
                queue.consume(task.id)
                task.job = copy.deepcopy(job)
                task.base_commit = job.base_commit
                queue.history.append(task)
                started.append(job)
        return started
