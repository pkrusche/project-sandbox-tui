"""Background / interactive job tracking — at most one agent per branch.

Every job has a JSON record in ``<state>/<repo>/jobs/<branch-slug>.json`` so a
restarted TUI re-adopts running background sessions and still notifies about
ones that finished while it was closed. A record replaced by a newer run, or
forgotten, moves to ``jobs/history/<branch-slug>/`` (the newest
``HISTORY_PER_BRANCH`` per branch), so earlier runs' session ids and outcomes
stay available.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from project_sandbox_tui import feedback, feedback_transport
from project_sandbox_tui.config import Config, ConfigError, private_dir, repo_state_dir
from project_sandbox_tui.processes import reap_later
from project_sandbox_tui.sandbox import (
    LaunchSpec,
    RunPaths,
    SessionRecord,
    build_argv,
    pid_alive,
    runtime_cli,
)

Status = Literal[
    "running", "completed", "failed", "rate_limited", "timeout", "interrupted", "orphaned"
]
TERMINAL: frozenset[str] = frozenset(
    {"completed", "failed", "rate_limited", "timeout", "interrupted", "orphaned"}
)

TIMEOUT_EXIT = 124
RATE_LIMIT_EXIT = 75
SIGINT_EXITS = {130, -signal.SIGINT}
# Refreshes in a row without its session before an external session counts as
# ended: one miss may be a session record that was being rewritten.
EXTERNAL_MISSES = 2
HISTORY_PER_BRANCH = 50


class BusyError(RuntimeError):
    """The branch already has an agent session."""


# Prepares a launch's workspace (``workspace_skills.WorkspacePreparer``):
# (config, spec, sandbox start?) -> None, raising to stop the launch.
Preparer = Callable[[Config, LaunchSpec, bool], None]
SessionSource = dict[str, SessionRecord] | Callable[[], dict[str, SessionRecord]]


def branch_slug(branch: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in branch)
    return f"{safe}-{hashlib.sha256(branch.encode()).hexdigest()[:6]}"


def _now() -> str:
    # Always with microseconds: records sort and compare these strings, and
    # isoformat() would drop the fraction when it happens to be zero.
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def status_for_exit(code: int | None) -> Status:
    if code is None:
        return "orphaned"
    if code == 0:
        return "completed"
    if code == TIMEOUT_EXIT:
        return "timeout"
    if code == RATE_LIMIT_EXIT:
        return "rate_limited"
    if code in SIGINT_EXITS:
        return "interrupted"
    return "failed"


@dataclass
class JobRecord:
    branch: str
    spec: LaunchSpec
    pid: int
    started_at: str
    argv: list[str] = field(default_factory=list)
    log_path: str = ""
    summary_path: str = ""
    stdout_path: str = ""
    status: Status = "running"
    exit_code: int | None = None
    ended_at: str = ""
    session_id: str = ""
    container_name: str = ""
    notified: bool = False
    external: bool = False  # interactive session launched in another terminal
    session_seen: bool = False  # external only: a project-sandbox session was observed
    runtime: str = ""
    host: bool = False
    queue_id: str = ""  # durable identity of a dispatched unsupervised task
    base_commit: str = ""  # pinned immediately before a queued launch

    run_id: str = ""
    feedback_path: str = ""
    feedback_status: str = "unsupported"
    feedback_error: str = ""
    feedback_snapshot: dict[str, Any] | None = None
    feedback_frozen: bool = False

    @property
    def reported_outcome(self) -> str:
        report = self.feedback_snapshot
        if self.feedback_status == "valid" and report and report["phase"] == "finished":
            return str(report["outcome"])
        return "unknown"

    @property
    def reported_success(self) -> bool:
        return (
            self.status == "completed"
            and self.exit_code == 0
            and self.reported_outcome == "success"
        )

    @property
    def blocked_summary(self) -> str:
        if not self.running and self.reported_outcome == "blocked" and self.feedback_snapshot:
            return str(self.feedback_snapshot["summary"])
        return ""

    def launch_argv(self) -> list[str]:
        if self.host and self.feedback_path:
            return feedback_transport.host_argv(Path(self.feedback_path).parent.parent, self.argv)
        return self.argv

    def feedback_detail(self) -> str:
        label = f"reported {self.reported_outcome} ({self.feedback_status})"
        if self.feedback_error:
            label += f": {self.feedback_error}"
        elif self.feedback_snapshot:
            label += f": {self.feedback_snapshot['summary']}"
        return label

    @property
    def mode(self) -> str:
        return self.spec.mode

    @property
    def running(self) -> bool:
        return self.status == "running"

    def run_paths(self) -> RunPaths | None:
        if not self.log_path:
            return None
        return RunPaths(
            log=Path(self.log_path),
            summary=Path(self.summary_path),
            stdout=Path(self.stdout_path),
        )

    def output_files(self) -> list[Path]:
        """Files worth showing in the log view, most useful first."""
        paths = self.run_paths()
        files = [paths.transcript(), paths.log] if paths else []
        if self.feedback_path:
            files.append(Path(self.feedback_path))
        if self.stdout_path:
            files.append(Path(self.stdout_path))
        return files

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "spec": self.spec.to_dict(),
            "pid": self.pid,
            "started_at": self.started_at,
            "argv": self.argv,
            "log_path": self.log_path,
            "summary_path": self.summary_path,
            "stdout_path": self.stdout_path,
            "status": self.status,
            "exit_code": self.exit_code,
            "ended_at": self.ended_at,
            "session_id": self.session_id,
            "container_name": self.container_name,
            "notified": self.notified,
            "external": self.external,
            "session_seen": self.session_seen,
            "runtime": self.runtime,
            "host": self.host,
            "queue_id": self.queue_id,
            "base_commit": self.base_commit,
            "run_id": self.run_id,
            "feedback_path": self.feedback_path,
            "feedback_status": self.feedback_status,
            "feedback_error": self.feedback_error,
            "feedback_snapshot": self.feedback_snapshot,
            "feedback_frozen": self.feedback_frozen,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> JobRecord:
        if not isinstance(raw, dict) or not isinstance(raw.get("spec", {}), dict):
            raise ValueError("invalid job record")
        pid = raw.get("pid", 0)
        # pid_t is a signed 32-bit integer on the supported platforms. Never
        # coerce floats or strings: truncating one could signal another process.
        if type(pid) is not int or not 0 <= pid < 2**31:
            raise ValueError("invalid process id")
        status = raw.get("status", "orphaned")
        return cls(
            branch=str(raw["branch"]),
            spec=LaunchSpec.from_dict(raw.get("spec") or {}),
            pid=pid,
            started_at=str(raw.get("started_at") or ""),
            argv=[str(a) for a in raw.get("argv") or []],
            log_path=str(raw.get("log_path") or ""),
            summary_path=str(raw.get("summary_path") or ""),
            stdout_path=str(raw.get("stdout_path") or ""),
            status=status if status in (*TERMINAL, "running") else "orphaned",
            exit_code=raw.get("exit_code") if type(raw.get("exit_code")) is int else None,
            ended_at=str(raw.get("ended_at") or ""),
            session_id=str(raw.get("session_id") or ""),
            container_name=str(raw.get("container_name") or ""),
            notified=bool(raw.get("notified")),
            external=bool(raw.get("external")),
            session_seen=bool(raw.get("session_seen")),
            runtime=str(raw.get("runtime") or ""),
            host=bool(raw.get("host")),
            queue_id=str(raw.get("queue_id") or ""),
            base_commit=str(raw.get("base_commit") or ""),
            run_id=str(raw.get("run_id") or ""),
            feedback_path=str(raw.get("feedback_path") or ""),
            feedback_status=str(raw.get("feedback_status") or "unsupported"),
            feedback_error=str(raw.get("feedback_error") or ""),
            feedback_snapshot=(
                feedback.validate(raw["feedback_snapshot"], str(raw.get("run_id", "")))
                if raw.get("feedback_snapshot") is not None
                else None
            ),
            feedback_frozen=raw.get("feedback_frozen") is True,
        )


class JobManager:
    def __init__(self, repo: Path, cfg: Config, state_dir: Path | None = None) -> None:
        self.repo = repo
        self.cfg = cfg
        self.state_dir = state_dir or repo_state_dir(repo)
        self.jobs_dir = self.state_dir / "jobs"
        self.logs_dir = self.state_dir / "logs"
        self.jobs: dict[str, JobRecord] = {}
        self._procs: dict[str, subprocess.Popen[bytes]] = {}
        self._stop_stage: dict[str, int] = {}  # cancel steps taken so far
        self._stop_last: dict[str, float] = {}  # when the last one was sent
        self._feedback_supported: bool | None = None
        self._feedback_command: tuple[str, ...] = ()
        self._missed: dict[tuple[str, str], int] = {}  # external runs: refreshes without a session
        # Called under the lock after the busy check, before a launch starts.
        self.prepare: Preparer | None = None
        self.load()

    # ------------------------------------------------------------ persistence

    @contextlib.contextmanager
    def _locked(self) -> Generator[None, None, None]:
        """Serialize record changes across TUI instances for this repository."""
        private_dir(self.state_dir)
        with (self.state_dir / "jobs.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _record_path(self, branch: str) -> Path:
        return self.jobs_dir / f"{branch_slug(branch)}.json"

    def _history_dir(self, branch: str) -> Path:
        return self.jobs_dir / "history" / branch_slug(branch)

    def _archive(self, branch: str) -> None:
        """Copy the branch's current record into its history before it is replaced.

        Best effort: a full disk must not block cancelling or forgetting a run.
        """
        path = self._record_path(branch)
        try:
            text = path.read_text(encoding="utf-8")
            started = str(json.loads(text).get("started_at") or "")
        except (OSError, ValueError, AttributeError):
            return
        stamp = "".join(c for c in started if c.isalnum()) or uuid.uuid4().hex
        history = self._history_dir(branch)
        try:
            private_dir(history.parent)
            private_dir(history)
            target = history / f"{stamp}.json"
            if not target.exists():
                tmp = history / f".{uuid.uuid4().hex}.tmp"
                try:
                    tmp.write_text(text, encoding="utf-8")
                    tmp.replace(target)
                finally:
                    with contextlib.suppress(OSError):
                        tmp.unlink(missing_ok=True)
            for old in sorted(history.glob("*.json"))[:-HISTORY_PER_BRANCH]:
                old.unlink(missing_ok=True)
        except OSError:
            pass

    def history(self, branch: str) -> list[JobRecord]:
        """Earlier runs on ``branch``, newest first (the current record excluded)."""
        records: list[JobRecord] = []
        for path in sorted(self._history_dir(branch).glob("*.json"), reverse=True):
            try:
                records.append(JobRecord.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return records

    def save(self, job: JobRecord) -> None:
        private_dir(self.state_dir)
        private_dir(self.jobs_dir)
        path = self._record_path(job.branch)
        tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(job.to_dict(), indent=2), encoding="utf-8")
            tmp.replace(path)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    def load(self) -> None:
        previous = self.jobs
        jobs: dict[str, JobRecord] = {}
        for path in sorted(self.jobs_dir.glob("*.json")) if self.jobs_dir.is_dir() else ():
            try:
                job = JobRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError, TypeError):
                continue
            old = previous.get(job.branch)
            if old is not None and (old.pid, old.started_at) == (job.pid, job.started_at):
                # Keep references held by an open log screen up to date.
                old.__dict__.update(job.__dict__)
                job = old
            jobs[job.branch] = job
        # One assignment: worker threads load while the UI thread iterates the
        # jobs, so it must see the old dict or the new one, never one being filled.
        self.jobs = jobs
        self._missed = {
            run: n
            for run, n in self._missed.items()
            if (j := jobs.get(run[0])) and j.running and j.started_at == run[1]
        }
        for branch in list(self._stop_stage):
            old, new = previous.get(branch), self.jobs.get(branch)
            if old is None or new is None or (old.pid, old.started_at) != (new.pid, new.started_at):
                self._stop_stage.pop(branch, None)
                self._stop_last.pop(branch, None)

    def clear(self, branch: str, force: bool = False) -> bool:
        """Forget a finished job. Returns False if it is still running.

        ``force`` also drops a record that still looks running — the escape
        hatch for a record whose PID was reused after a crash. It does not
        signal any process.
        """
        with self._locked():
            self.load()
            job = self.jobs.get(branch)
            if job is None:
                return True
            if job.running and not force:
                return False
            self._forget(branch)
            return True

    def discard(self, job: JobRecord) -> None:
        """Roll back a failed launch without removing a replacement run."""
        with self._locked():
            current = self._read_record(job.branch)
            if current is not None and current.started_at == job.started_at:
                # The launch never ran: keep it out of the job history.
                self._forget(job.branch, archive=False)
                if job.feedback_path:
                    root = self.state_dir / "runs" / job.run_id
                    if Path(job.feedback_path) == root / "output" / "feedback.json":
                        shutil.rmtree(root, ignore_errors=True)

    # ------------------------------------------------------------ queries

    def busy_reason(self, branch: str, sessions: SessionSource) -> str | None:
        with self._locked():
            self.load()
            return self._busy_reason(branch, sessions)

    def _busy_reason(self, branch: str, sessions: SessionSource) -> str | None:
        job = self.jobs.get(branch)
        if job is not None and job.running:
            kind = "host agent" if job.host else job.mode
            return f"{branch} already has a {kind} session (pid {job.pid})"
        records = sessions if isinstance(sessions, dict) else sessions()
        if branch in records:
            rec = records[branch]
            return f"{branch} already has a running project-sandbox session {rec.session_id}"
        return None

    def running_count(self) -> int:
        return sum(1 for j in self.jobs.values() if j.running)

    # ------------------------------------------------------------ launching

    def new_run_paths(self, branch: str) -> RunPaths:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        base = self.logs_dir / f"{branch_slug(branch)}-{stamp}-{uuid.uuid4().hex[:8]}"
        # Append extensions: with_suffix() would strip dots in branch names.
        return RunPaths(
            log=Path(f"{base}.log"),
            summary=Path(f"{base}.summary.json"),
            stdout=Path(f"{base}.out"),
        )

    def _prepare_feedback(
        self,
        spec: LaunchSpec,
        task_id: str = "",
        previous_state: dict[str, Any] | None = None,
        *,
        host: bool = False,
        require_feedback: bool = False,
        blocked_guidance: str = "",
    ) -> tuple[str, Path | None]:
        run_id = uuid.uuid4().hex
        if not self.cfg.feedback.enabled:
            if require_feedback:
                raise ConfigError("workflow requires feedback.enabled")
            return run_id, None
        root = self.state_dir / "runs" / run_id
        if not host and "--dry-run" in (*self.cfg.sandbox.extra_args, *spec.extra_args):
            if require_feedback:
                raise ConfigError("a workflow step requires a live project-sandbox launch")
            return run_id, root
        command = tuple(self.cfg.sandbox_command()) if not host else ()
        if not host and (self._feedback_supported is None or command != self._feedback_command):
            self._feedback_supported = feedback_transport.supported(list(command), cwd=self.repo)
            self._feedback_command = command
        if not host and not self._feedback_supported:
            if require_feedback or self.cfg.feedback.requires_reporting:
                raise ConfigError("feedback policy requires project-sandbox with --mount support")
            return run_id, None
        if not host:
            feedback_transport.mounts(root)  # validate mount path before creating files
        if host:
            from project_sandbox_tui import initialization
            from project_sandbox_tui.vcs import workspace_path

            workspace = (
                self.repo
                if spec.main_checkout
                else workspace_path(
                    self.repo,
                    spec.branch,
                    self.cfg.resolve_path(self.cfg.project.worktree_dir, self.repo)
                    if self.cfg.project.worktree_dir
                    else None,
                )
            )
            if workspace.is_dir():
                initialization.install_feedback(workspace)
        try:
            feedback_transport.prepare(
                root,
                run_id,
                task_id,
                previous_state,
                interactive=spec.mode == "interactive",
                planning_prompt=spec.prompt_text if task_id and spec.mode == "interactive" else "",
                blocked_guidance=blocked_guidance,
            )
        except (OSError, ValueError):
            shutil.rmtree(root, ignore_errors=True)
            raise
        return run_id, root

    @staticmethod
    def _attach_feedback(job: JobRecord, run_id: str, root: Path | None) -> None:
        job.run_id = run_id
        if root:
            job.feedback_path = str(root / "output" / "feedback.json")
            job.feedback_status = "missing"

    def _collect_feedback(self, job: JobRecord, *, final: bool = False) -> None:
        if job.feedback_frozen or not job.feedback_path:
            return
        before = (
            job.feedback_status,
            job.feedback_error,
            job.feedback_snapshot,
            job.feedback_frozen,
        )
        try:
            report = feedback.read_json(Path(job.feedback_path))
            job.feedback_snapshot = feedback.accept(report, job.run_id, job.feedback_snapshot)
            job.feedback_status, job.feedback_error = "valid", ""
        except FileNotFoundError:
            job.feedback_status, job.feedback_error = "missing", ""
        except feedback.UnsupportedFeedback as exc:
            job.feedback_status, job.feedback_error = "unsupported", str(exc)
        except (OSError, ValueError) as exc:
            job.feedback_status, job.feedback_error = "invalid", str(exc)
        job.feedback_frozen = final
        after = (
            job.feedback_status,
            job.feedback_error,
            job.feedback_snapshot,
            job.feedback_frozen,
        )
        if before != after:
            self.save(job)

    def start_background(
        self,
        spec: LaunchSpec,
        sessions: SessionSource,
        *,
        queue_id: str = "",
        base_commit: Callable[[], str] | None = None,
        previous_state: dict[str, Any] | None = None,
        require_feedback: bool = False,
    ) -> JobRecord:
        with self._locked():
            self.load()
            if queue_id:
                current = self.jobs.get(spec.branch)
                records = ([current] if current else []) + self.history(spec.branch)
                if previous := next((j for j in records if j.queue_id == queue_id), None):
                    return previous
            reason = self._busy_reason(spec.branch, sessions)
            if reason:
                raise BusyError(reason)
            commit = base_commit() if base_commit else ""
            paths = self.new_run_paths(spec.branch)
            build_argv(self.cfg, self.repo, spec, paths)  # validate before staging resources
            self._prepare(spec, sandbox=True)
            run_id, feedback_dir = self._prepare_feedback(
                spec, queue_id, previous_state, require_feedback=require_feedback
            )
            argv = build_argv(self.cfg, self.repo, spec, paths, feedback_dir)
            self._stop_stage.pop(spec.branch, None)
            self._stop_last.pop(spec.branch, None)
            private_dir(paths.log.parent)
            try:
                with paths.stdout.open("wb") as out:
                    proc = subprocess.Popen(
                        argv,
                        cwd=self.repo,
                        stdin=subprocess.DEVNULL,
                        stdout=out,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,  # survive the TUI; own group for cancel()
                    )
            except OSError:
                paths.stdout.unlink(missing_ok=True)
                if feedback_dir:
                    shutil.rmtree(feedback_dir, ignore_errors=True)
                raise
            self._release_process(spec.branch)
            self._procs[spec.branch] = proc
            self._archive(spec.branch)
            job = JobRecord(
                branch=spec.branch,
                spec=spec,
                pid=proc.pid,
                started_at=_now(),
                argv=argv,
                log_path=str(paths.log),
                summary_path=str(paths.summary),
                stdout_path=str(paths.stdout),
                queue_id=queue_id,
                base_commit=commit,
            )
            self._attach_feedback(job, run_id, feedback_dir)
            self.jobs[spec.branch] = job
            try:
                self.save(job)
            except OSError:
                # A detached process without a durable record cannot be
                # guarded or adopted. Tear it down if persistence fails.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
                # The parent may have exited before its children did.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                self._procs.pop(spec.branch, None)
                self.load()  # retain the previous run if its record is still on disk
                for path in (paths.stdout, paths.log, paths.summary, paths.transcript()):
                    with contextlib.suppress(OSError):
                        path.unlink(missing_ok=True)
                if feedback_dir:
                    shutil.rmtree(feedback_dir, ignore_errors=True)
                raise
            reap_later(proc)
            return job

    @contextlib.contextmanager
    def interactive(
        self,
        spec: LaunchSpec,
        sessions: SessionSource,
        *,
        argv: list[str] | None = None,
        host: bool = False,
        queue_id: str = "",
        blocked_guidance: str = "",
    ) -> Generator[JobRecord, None, None]:
        """Hold the branch for the duration of an in-terminal session."""
        with self._locked():
            self.load()
            reason = self._busy_reason(spec.branch, sessions)
            if reason:
                raise BusyError(reason)
            self._prepare(spec, sandbox=not host)
            run_id, feedback_dir = self._prepare_feedback(
                spec,
                queue_id,
                host=host,
                require_feedback=bool(queue_id),
                blocked_guidance=blocked_guidance,
            )
            launch_argv = (
                argv
                if argv is not None
                else build_argv(self.cfg, self.repo, spec, feedback_dir=feedback_dir)
            )
            job = JobRecord(
                branch=spec.branch,
                spec=spec,
                pid=os.getpid(),  # the TUI itself holds the terminal
                started_at=_now(),
                argv=launch_argv,
                notified=True,  # nothing to notify: the user is watching
                host=host,
                queue_id=queue_id,
            )
            self._attach_feedback(job, run_id, feedback_dir)
            self._archive(spec.branch)
            self.save(job)
            # The caller owns the exit code until it returns. Polling can reload
            # the cached record from disk while the terminal is still active.
            self.jobs[spec.branch] = JobRecord.from_dict(job.to_dict())
        try:
            yield job
        finally:
            job.ended_at = _now()
            job.status = status_for_exit(job.exit_code)
            with self._locked():
                # A force-forgotten/replaced run must not overwrite a newer one.
                current = self._read_record(spec.branch)
                if current is not None and current.started_at == job.started_at:
                    # Keep the ids another instance saw meanwhile in the session records.
                    job.session_id = job.session_id or current.session_id
                    job.container_name = job.container_name or current.container_name
                    job.runtime = job.runtime or current.runtime
                    job.feedback_snapshot = current.feedback_snapshot
                    self._collect_feedback(job, final=True)
                    self.save(job)
                    self.load()

    def _prepare(self, spec: LaunchSpec, *, sandbox: bool) -> None:
        """Create the workspace and deploy skills; with the lock held, after the busy check."""
        if self.prepare is not None:
            self.prepare(self.cfg, spec, sandbox)

    def _read_record(self, branch: str) -> JobRecord | None:
        try:
            return JobRecord.from_dict(json.loads(self._record_path(branch).read_text("utf-8")))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def record_external(
        self,
        spec: LaunchSpec,
        argv: list[str],
        sessions: SessionSource | None = None,
        *,
        host: bool = False,
        queue_id: str = "",
        blocked_guidance: str = "",
    ) -> JobRecord:
        """Note an interactive session started in another terminal window.

        Its liveness comes from project-sandbox session records afterwards, so
        the record is closed on the next refresh in which no session is seen
        for the branch (after a grace period covering the image build). Host
        sessions instead report their runner PID and exit code in a summary.
        """
        with self._locked():
            self.load()
            reason = self._busy_reason(spec.branch, sessions or {})
            if reason:
                raise BusyError(reason)
            self._prepare(spec, sandbox=not host)
            run_id, feedback_dir = self._prepare_feedback(
                spec,
                queue_id,
                host=host,
                require_feedback=bool(queue_id),
                blocked_guidance=blocked_guidance,
            )
            paths = self.new_run_paths(spec.branch) if host or feedback_dir else None
            if paths:
                private_dir(paths.summary.parent)
            if feedback_dir and not host:
                assert paths is not None
                argv = [*argv, *feedback_transport.mounts(feedback_dir)]
                if not queue_id:
                    argv.append(f"--json-summary={paths.summary}")
            job = JobRecord(
                branch=spec.branch,
                spec=spec,
                pid=0,
                started_at=_now(),
                argv=argv,
                notified=not (host or queue_id),
                external=True,
                host=host,
                queue_id=queue_id,
            )
            self._attach_feedback(job, run_id, feedback_dir)
            if paths:
                job.summary_path = str(paths.summary)
            self._archive(spec.branch)
            self.save(job)
            self.jobs[spec.branch] = job
            return job

    # ------------------------------------------------------------ polling

    def _process_exit(self, job: JobRecord) -> tuple[bool, int | None]:
        """Return (finished, returncode-if-known) for a background job."""
        proc = self._procs.get(job.branch)
        if proc is not None and proc.pid == job.pid:
            code = proc.poll()
            return code is not None, code
        # Adopted from an earlier TUI run: not our child, so no returncode.
        # A summary can be written before teardown finishes. Keep the branch
        # busy until the process exits, including after adoption.
        try:
            pid, status = os.waitpid(job.pid, os.WNOHANG) if job.pid > 0 else (0, 0)
        except ChildProcessError:
            pass  # a true adoption: this process is not our child
        else:
            if pid == job.pid and pid > 0:
                return True, os.waitstatus_to_exitcode(status)
        return not pid_alive(job.pid), None

    def _read_summary(self, job: JobRecord) -> dict[str, Any] | None:
        if not job.summary_path:
            return None
        try:
            raw = json.loads(Path(job.summary_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return raw if isinstance(raw, dict) else None

    def refresh(
        self,
        sessions: dict[str, SessionRecord],
        external_grace_s: float = 600.0,
    ) -> list[JobRecord]:
        """Update running jobs; return those that just reached a terminal state."""
        with self._locked():
            self.load()
            # Reap children even when their records were force-forgotten.
            for branch, proc in list(self._procs.items()):
                job = self.jobs.get(branch)
                if proc.poll() is not None and (
                    job is None or job.pid != proc.pid or not job.running
                ):
                    self._procs.pop(branch, None)
            return self._refresh(sessions, external_grace_s)

    def _refresh(
        self, sessions: dict[str, SessionRecord], external_grace_s: float
    ) -> list[JobRecord]:
        finished: list[JobRecord] = []
        now = time.time()
        for job in list(self.jobs.values()):
            if not job.running:
                if not job.notified:
                    finished.append(job)  # finished while the TUI was closed
                continue
            self._collect_feedback(job)
            rec = sessions.get(job.branch) if not job.host else None
            if rec is not None:
                ids = (job.session_id, job.container_name, job.runtime)
                job.session_id = job.session_id or rec.session_id
                job.container_name = job.container_name or rec.container_name
                job.runtime = job.runtime or rec.runtime
                if (job.session_id, job.container_name, job.runtime) != ids:
                    self.save(job)

            if job.external and (job.host or job.queue_id):
                summary = self._read_summary(job) or {}
                pid = job.pid or summary.get("pid")
                if type(pid) is int and 0 < pid < 2**31:
                    if job.pid != pid:
                        job.pid = pid
                        self.save(job)
                    # The runner writes its outcome before exiting. Keep the
                    # workspace reserved until teardown has actually finished.
                    if pid_alive(pid):
                        continue
                elif now - _parse_ts(job.started_at) <= external_grace_s:
                    continue  # the terminal has not started the runner yet
                code = summary.get("exit_code")
                job.exit_code = code if type(code) is int else None
                job.status = status_for_exit(job.exit_code)
                job.ended_at = _now()
                self._collect_feedback(job, final=True)
                self.save(job)
                if not job.notified:
                    finished.append(job)
                continue

            if job.external:
                run = (job.branch, job.started_at)
                if rec is not None:
                    self._missed.pop(run, None)
                    if not job.session_seen:
                        job.session_seen = True
                        self.save(job)
                    continue
                # Done once the session we saw has gone, or if none ever showed
                # up within the grace period (which covers the image build).
                if job.session_seen:
                    self._missed[run] = self._missed.get(run, 0) + 1
                    if self._missed[run] < EXTERNAL_MISSES:
                        continue
                elif now - _parse_ts(job.started_at) <= external_grace_s:
                    continue
                self._missed.pop(run, None)
                summary = self._read_summary(job) or {}
                code = summary.get("exit_code")
                job.exit_code = code if type(code) is int else None
                job.status = (
                    status_for_exit(job.exit_code) if job.exit_code is not None else "completed"
                )
                job.ended_at = _now()
                self._collect_feedback(job, final=True)
                self.save(job)
                continue

            if job.mode == "interactive":
                if not pid_alive(job.pid):
                    # The TUI that held this branch died mid-session.
                    job.status = "interrupted"
                    job.ended_at = _now()
                    self._collect_feedback(job, final=True)
                    self.save(job)
                continue

            done, returncode = self._process_exit(job)
            if not done:
                continue
            summary = self._read_summary(job)
            code = returncode
            if summary is not None:
                job.session_id = str(summary.get("session_id") or job.session_id)
                job.container_name = str(summary.get("container_name") or job.container_name)
                if type(summary.get("exit_code")) is int:
                    code = summary["exit_code"]
            job.exit_code = code
            job.status = status_for_exit(code)
            if job.branch in self._stop_stage and job.status in ("failed", "orphaned"):
                job.status = "interrupted"
            job.ended_at = _now()
            self._collect_feedback(job, final=True)
            # Saved first: if that fails, the next pass still has the child's
            # return code instead of finding it reaped and calling it orphaned.
            self.save(job)
            self._release_process(job.branch)
            self._stop_stage.pop(job.branch, None)
            self._stop_last.pop(job.branch, None)
            finished.append(job)
        return finished

    def mark_notified(self, job: JobRecord) -> bool:
        """Claim a notification once, even when multiple TUIs observe the exit."""
        with self._locked():
            current = self._read_record(job.branch)
            if current is None or current.started_at != job.started_at or current.notified:
                return False
            current.notified = True
            self.save(current)
            job.notified = True
            return True

    # ------------------------------------------------------------ cancelling

    STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGKILL)
    STOP_DEBOUNCE_S = 1.0

    def cancel(self, branch: str, sessions: dict[str, SessionRecord]) -> str:
        """Escalating cancel, one step per call: SIGINT → SIGTERM → SIGKILL and forget.

        SIGINT first lets project-sandbox run its ``finally`` teardown (commit
        work onto the branch, close the session record). The SIGKILL step also
        stops the container, since project-sandbox cannot clean up after it,
        and forgets the record. Finished records are forgotten at once; running
        records that cannot be signalled (interactive sessions, invalid PIDs)
        on the third call, without signalling anything. Calls within
        ``STOP_DEBOUNCE_S`` of the previous one are ignored so a double key
        press does not skip a step.
        """
        with self._locked():
            self.load()
            return self._cancel(branch, sessions)

    def cancel_started(self, branch: str) -> bool:
        """Whether ``cancel`` already signalled (or tried to) the branch's current run."""
        return branch in self._stop_stage

    def _forget(self, branch: str, *, archive: bool = True) -> None:
        if archive:
            self._archive(branch)
        self._record_path(branch).unlink(missing_ok=True)
        self.jobs.pop(branch, None)
        self._release_process(branch)
        self._stop_stage.pop(branch, None)
        self._stop_last.pop(branch, None)
        self._missed = {run: n for run, n in self._missed.items() if run[0] != branch}

    def _release_process(self, branch: str) -> None:
        # The waiter started at launch owns the child even without this handle.
        self._procs.pop(branch, None)

    def _cancel(self, branch: str, sessions: dict[str, SessionRecord]) -> str:
        job = self.jobs.get(branch)
        if job is None:
            return f"{branch}: nothing to cancel"
        if not job.running:
            self._forget(branch)
            return f"{branch}: {job.status.replace('_', ' ')} status cleared"
        now = time.time()
        last = self._stop_last.get(branch)
        if last is not None and now - last < self.STOP_DEBOUNCE_S:
            return f"{branch}: cancel already requested"
        stage = min(self._stop_stage.get(branch, 0), len(self.STOP_SIGNALS) - 1)
        if job.mode != "background" or job.pid <= 0:
            if stage == len(self.STOP_SIGNALS) - 1:
                self._forget(branch)
                return f"{branch}: record forgotten (no process was signalled)"
            self._stop_stage[branch] = stage + 1
            self._stop_last[branch] = now
            reason = (
                "interactive sessions are stopped from their terminal"
                if job.mode != "background"
                else f"invalid process id {job.pid}"
            )
            more = len(self.STOP_SIGNALS) - 1 - stage
            return f"{branch}: {reason}; x {more} more time{'s' * (more > 1)} forgets the record"
        sig = self.STOP_SIGNALS[stage]
        try:
            os.killpg(job.pid, sig)
        except ProcessLookupError:
            return f"{branch}: process already gone"
        except PermissionError:
            return f"{branch}: not permitted to signal pid {job.pid}"
        self._stop_stage[branch] = stage + 1
        self._stop_last[branch] = now
        name = signal.Signals(sig).name
        if sig != signal.SIGKILL:
            more = "harder" if stage + 2 < len(self.STOP_SIGNALS) else "with SIGKILL and forget it"
            return f"{branch}: sent {name} (press x again to cancel {more})"
        container = job.container_name or (
            sessions[branch].container_name if branch in sessions else ""
        )
        runtime = job.runtime or (sessions[branch].runtime if branch in sessions else "")
        self._forget(branch)
        cli = runtime_cli(runtime or self.cfg.sandbox.runtime)
        if container and cli:
            try:
                proc = subprocess.Popen(
                    [cli, "stop", container],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                reap_later(proc)
            except OSError as exc:
                return f"{branch}: sent SIGKILL, record forgotten; container stop failed: {exc}"
            return (
                f"{branch}: sent SIGKILL, record forgotten, "
                f"requested container stop for {container}"
            )
        return f"{branch}: sent SIGKILL, record forgotten (no container to stop)"


def _parse_ts(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    except ValueError:
        return 0.0
