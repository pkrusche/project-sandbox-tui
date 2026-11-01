"""Host commands run in bookmark workspaces by ``:*`` — in parallel, output captured.

Each task runs detached (``start_new_session``) under a small shell wrapper that
writes the command's exit status to a file, so a TUI started later still learns
how a task ended. Records live in ``<state>/<repo>/tasks/<id>.json`` and output
in ``<state>/<repo>/logs/<id>.task.out``; the newest :data:`KEEP` per bookmark
are kept, along with running tasks and completions awaiting notification.

Unlike sandbox jobs (jobs.py), a bookmark may run several tasks at once.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import time
import uuid
from collections.abc import Generator, Mapping
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from project_sandbox_tui.config import private_dir
from project_sandbox_tui.jobs import SIGINT_EXITS, _now, _parse_ts, branch_slug
from project_sandbox_tui.processes import reap_later
from project_sandbox_tui.sandbox import pid_alive

KEEP = 10  # records per bookmark
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGKILL)
STOP_DEBOUNCE_S = 1.0

# The trap keeps the wrapper alive when x sends SIGINT to the process group, so
# it still records the command's status; the command itself gets the default
# action, since trapped signals are reset in children. The status is renamed
# into place: a TUI that sees the exit file must never read it half-written.
WRAPPER = 'trap : INT TERM; sh -c "$1"; printf %s "$?" >"$2.tmp" && mv -f "$2.tmp" "$2"'
STATUSES = frozenset({"running", "completed", "failed", "interrupted", "orphaned"})
_TEXT_FIELDS = ("id", "bookmark", "line", "script", "cwd", "started_at", "out_path", "exit_path")


@dataclass
class TaskRecord:
    id: str
    bookmark: str
    line: str  # the command line as typed, without the colon
    script: str
    cwd: str
    pid: int
    started_at: str
    out_path: str
    exit_path: str
    status: str = "running"  # running | completed | failed | interrupted | orphaned
    exit_code: int | None = None
    ended_at: str = ""
    notified: bool = False

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def branch(self) -> str:  # for the log viewer
        return self.bookmark

    @property
    def name(self) -> str:
        """Short label for the status column: the command name, or the program after ``!``."""
        body = self.line.lstrip("*").strip()
        word = body.removeprefix("!").split(maxsplit=1)
        return (word[0] if word else body)[:12]

    def output_files(self) -> list[Path]:
        return [Path(self.out_path)]

    def started(self) -> float:
        return _parse_ts(self.started_at)

    @classmethod
    def from_dict(cls, raw: Any) -> TaskRecord:
        if not isinstance(raw, dict):
            raise ValueError("invalid task record")
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in raw.items() if k in known}
        task = cls(**values)
        # As for job records: pid 0 or a negative pid would make cancel signal
        # this TUI's own process group or every process it may signal.
        if type(task.pid) is not int or not 0 < task.pid < 2**31:
            raise ValueError("invalid process id")
        if not all(isinstance(getattr(task, name), str) for name in _TEXT_FIELDS):
            raise ValueError("invalid task record")
        if task.status not in STATUSES:
            task.status = "orphaned"
        if type(task.exit_code) is not int:
            task.exit_code = None
        task.ended_at = task.ended_at if isinstance(task.ended_at, str) else ""
        task.notified = task.notified is True
        return task


def status_for_exit(code: int | None) -> str:
    if code is None:
        return "orphaned"
    if code == 0:
        return "completed"
    if code in SIGINT_EXITS or code in (-signal.SIGTERM, 143, -signal.SIGKILL, 137):
        return "interrupted"
    return "failed"


class TaskManager:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.tasks_dir = state_dir / "tasks"
        self.logs_dir = state_dir / "logs"
        self.tasks: dict[str, TaskRecord] = {}
        self._procs: dict[str, subprocess.Popen[bytes]] = {}
        self._stop_stage: dict[str, int] = {}
        self._stop_last: dict[str, float] = {}
        self.load()

    # ------------------------------------------------------------ persistence

    @contextlib.contextmanager
    def _locked(self) -> Generator[None, None, None]:
        private_dir(self.state_dir)
        with (self.state_dir / "tasks.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _record_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    def save(self, task: TaskRecord) -> None:
        private_dir(self.tasks_dir)
        path = self._record_path(task.id)
        tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(asdict(task), indent=2), encoding="utf-8")
            tmp.replace(path)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    def load(self) -> None:
        previous = self.tasks
        tasks: dict[str, TaskRecord] = {}
        for path in self.tasks_dir.glob("*.json") if self.tasks_dir.is_dir() else ():
            try:
                task = TaskRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                continue
            if task.id != path.stem:  # the id names the files that _forget deletes
                continue
            old = previous.get(task.id)
            if old is not None:
                old.__dict__.update(task.__dict__)  # keep an open log screen's record current
                task = old
            tasks[task.id] = task
        # One assignment: refresh() runs in a worker thread while the UI thread
        # iterates the tasks, so it must see the old dict or the new one, never
        # one being filled.
        self.tasks = tasks
        for task_id in [t for t in self._stop_stage if t not in tasks]:
            self._stop_stage.pop(task_id, None)
            self._stop_last.pop(task_id, None)

    def _forget(self, task: TaskRecord) -> None:
        self._record_path(task.id).unlink(missing_ok=True)
        for path in (task.out_path, task.exit_path, f"{task.exit_path}.tmp"):
            with contextlib.suppress(OSError):
                Path(path).unlink(missing_ok=True)
        self.tasks.pop(task.id, None)
        if proc := self._procs.pop(task.id, None):
            reap_later(proc)
        self._stop_stage.pop(task.id, None)
        self._stop_last.pop(task.id, None)

    def _prune(self, bookmark: str) -> None:
        for task in self.for_bookmark(bookmark)[KEEP:]:
            if not task.running and task.notified:
                self._forget(task)

    # ------------------------------------------------------------ queries

    def for_bookmark(self, bookmark: str) -> list[TaskRecord]:
        """The bookmark's tasks, newest first."""
        found = [t for t in self.tasks.values() if t.bookmark == bookmark]
        return sorted(found, key=lambda t: t.started_at, reverse=True)

    def latest(self, bookmark: str) -> TaskRecord | None:
        found = self.for_bookmark(bookmark)
        return found[0] if found else None

    def running(self, bookmark: str) -> list[TaskRecord]:
        return [t for t in self.for_bookmark(bookmark) if t.running]

    def running_count(self) -> int:
        return sum(1 for t in self.tasks.values() if t.running)

    # ------------------------------------------------------------ running

    def start(
        self, bookmark: str, line: str, script: str, cwd: Path, env: Mapping[str, str]
    ) -> TaskRecord:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        task_id = f"{branch_slug(bookmark)}-{stamp}-{uuid.uuid4().hex[:8]}"
        out = self.logs_dir / f"{task_id}.task.out"
        exit_file = self.logs_dir / f"{task_id}.task.exit"
        with self._locked():
            self.load()
            private_dir(self.logs_dir)
            try:
                with out.open("wb") as stream:
                    proc = subprocess.Popen(
                        ["sh", "-c", WRAPPER, "sbx-task", script, str(exit_file)],
                        cwd=cwd,
                        env=dict(env),
                        stdin=subprocess.DEVNULL,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,  # survives the TUI; own group for cancel()
                    )
            except OSError:
                out.unlink(missing_ok=True)
                raise
            task = TaskRecord(
                id=task_id,
                bookmark=bookmark,
                line=line,
                script=script,
                cwd=str(cwd),
                pid=proc.pid,
                started_at=_now(),
                out_path=str(out),
                exit_path=str(exit_file),
            )
            self._procs[task_id] = proc
            self.tasks[task_id] = task
            try:
                self.save(task)
            except OSError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                self._forget(task)
                raise
            self._prune(bookmark)
            return task

    def _read_exit(self, task: TaskRecord) -> int | None:
        try:
            return int(Path(task.exit_path).read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None

    def _finished(self, task: TaskRecord) -> tuple[bool, int | None]:
        """(finished, the wrapper's own exit status if this TUI started it)."""
        proc = self._procs.get(task.id)
        if proc is not None and proc.pid == task.pid:
            code = proc.poll()
            return code is not None, code
        # Started by an earlier TUI. Writing the exit file is the wrapper's last
        # step. Older wrappers wrote it in place, so an empty one may be in progress.
        if self._read_exit(task) is not None:
            return True, None
        try:
            pid, _status = os.waitpid(task.pid, os.WNOHANG) if task.pid > 0 else (0, 0)
        except ChildProcessError:
            pass  # not our child
        else:
            if pid == task.pid and pid > 0:
                return True, None
        return not pid_alive(task.pid), None

    def refresh(self) -> list[TaskRecord]:
        """Update running tasks; return finished ones not yet announced."""
        with self._locked():
            self.load()
            # Reap children whose records another TUI (or a cleanup scan) finalised.
            for task_id, proc in list(self._procs.items()):
                task = self.tasks.get(task_id)
                if proc.poll() is not None and (
                    task is None or task.pid != proc.pid or not task.running
                ):
                    self._procs.pop(task_id, None)
            out = []
            for task in list(self.tasks.values()):
                if task.running:
                    done, wrapper = self._finished(task)
                    if not done:
                        continue
                    code = self._read_exit(task)
                    if code is None and wrapper not in (None, 0):
                        code = wrapper  # killed before it could write the status
                    task.exit_code = code
                    task.status = status_for_exit(code)
                    if task.id in self._stop_stage and task.status in ("failed", "orphaned"):
                        task.status = "interrupted"
                    task.ended_at = _now()
                    # Saved first: if that fails, the next pass still has the child's status.
                    self.save(task)
                    self._procs.pop(task.id, None)
                    self._stop_stage.pop(task.id, None)
                    self._stop_last.pop(task.id, None)
                if not task.notified:
                    out.append(task)
            for bookmark in {task.bookmark for task in self.tasks.values()}:
                self._prune(bookmark)
            return out

    def mark_notified(self, task: TaskRecord) -> bool:
        """Claim a notification once, even when several TUIs see the task end."""
        with self._locked():
            try:
                raw = json.loads(self._record_path(task.id).read_text(encoding="utf-8"))
                current = TaskRecord.from_dict(raw)
            except (OSError, ValueError, TypeError):
                return False
            if current.notified:
                return False
            current.notified = True
            self.save(current)
            task.notified = True
            self.load()
            self._prune(current.bookmark)
            return True

    # ------------------------------------------------------------ cancelling

    def cancel(self, bookmark: str) -> list[str]:
        """Escalate on each call, as for sandbox runs: SIGINT → SIGTERM → SIGKILL.

        Returns a message per running task of ``bookmark``; finished ones are
        left alone (they are history, not status)."""
        with self._locked():
            self.load()
            return [self._signal(task) for task in self.running(bookmark)]

    def cancel_task(self, task_id: str) -> str:
        """Escalate one task's cancel; a finished task's record and output are forgotten."""
        with self._locked():
            self.load()
            task = self.tasks.get(task_id)
            if task is None:
                return "task already forgotten"
            if task.running:
                return self._signal(task)
            self._forget(task)
            return f"{task.bookmark}: :{task.line} forgotten"

    def _signal(self, task: TaskRecord) -> str:
        where = f"{task.bookmark}: :{task.line}"
        now = time.time()
        last = self._stop_last.get(task.id)
        if last is not None and now - last < STOP_DEBOUNCE_S:
            return f"{where} cancel already requested"
        if task.pid <= 0:  # never killpg(0): that is this TUI's own process group
            return f"{task.bookmark}: invalid process id {task.pid}"
        stage = min(self._stop_stage.get(task.id, 0), len(STOP_SIGNALS) - 1)
        sig = STOP_SIGNALS[stage]
        try:
            os.killpg(task.pid, sig)
        except ProcessLookupError:
            return f"{where} already finished"
        except PermissionError:
            return f"{task.bookmark}: not permitted to signal pid {task.pid}"
        self._stop_stage[task.id] = stage + 1
        self._stop_last[task.id] = now
        return f"{task.bookmark}: sent {signal.Signals(sig).name} to :{task.line}"

    def cancel_requested(self, task_id: str) -> bool:
        return task_id in self._stop_stage

    def cancel_started(self, bookmark: str) -> bool:
        return any(t.id in self._stop_stage for t in self.running(bookmark))
