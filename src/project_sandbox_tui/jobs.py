"""Background / interactive job tracking — at most one sandbox per branch.

Every job has a JSON record in ``<state>/<repo>/jobs/<branch-slug>.json`` so a
restarted TUI re-adopts running background sessions and still notifies about
ones that finished while it was closed.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from project_sandbox_tui.config import Config, repo_state_dir
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


class BusyError(RuntimeError):
    """The branch already has a sandbox session."""


def branch_slug(branch: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in branch)
    return f"{safe}-{hashlib.sha256(branch.encode()).hexdigest()[:6]}"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        if paths is None:
            return []
        return [paths.transcript(), paths.log, paths.stdout]

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
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> JobRecord:
        status = raw.get("status", "orphaned")
        return cls(
            branch=str(raw["branch"]),
            spec=LaunchSpec.from_dict(raw.get("spec") or {}),
            pid=int(raw.get("pid") or 0),
            started_at=str(raw.get("started_at") or ""),
            argv=[str(a) for a in raw.get("argv") or []],
            log_path=str(raw.get("log_path") or ""),
            summary_path=str(raw.get("summary_path") or ""),
            stdout_path=str(raw.get("stdout_path") or ""),
            status=status if status in (*TERMINAL, "running") else "orphaned",
            exit_code=raw.get("exit_code") if isinstance(raw.get("exit_code"), int) else None,
            ended_at=str(raw.get("ended_at") or ""),
            session_id=str(raw.get("session_id") or ""),
            container_name=str(raw.get("container_name") or ""),
            notified=bool(raw.get("notified")),
            external=bool(raw.get("external")),
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
        self._stop_requested: dict[str, float] = {}
        self.load()

    # ------------------------------------------------------------ persistence

    def _record_path(self, branch: str) -> Path:
        return self.jobs_dir / f"{branch_slug(branch)}.json"

    def save(self, job: JobRecord) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        path = self._record_path(job.branch)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(job.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(path)

    def load(self) -> None:
        self.jobs = {}
        if not self.jobs_dir.is_dir():
            return
        for path in sorted(self.jobs_dir.glob("*.json")):
            try:
                job = JobRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError, TypeError):
                continue
            self.jobs[job.branch] = job

    def clear(self, branch: str) -> bool:
        """Forget a finished job. Returns False if it is still running."""
        job = self.jobs.get(branch)
        if job is None:
            return True
        if job.running:
            return False
        with contextlib.suppress(OSError):
            self._record_path(branch).unlink()
        del self.jobs[branch]
        return True

    # ------------------------------------------------------------ queries

    def busy_reason(self, branch: str, sessions: dict[str, SessionRecord]) -> str | None:
        job = self.jobs.get(branch)
        if job is not None and job.running:
            return f"{branch} already has a {job.mode} session (pid {job.pid})"
        if branch in sessions:
            rec = sessions[branch]
            return f"{branch} already has a running project-sandbox session {rec.session_id}"
        return None

    def running_count(self) -> int:
        return sum(1 for j in self.jobs.values() if j.running)

    # ------------------------------------------------------------ launching

    def new_run_paths(self, branch: str) -> RunPaths:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        base = self.logs_dir / f"{branch_slug(branch)}-{stamp}"
        return RunPaths(
            log=base.with_suffix(".log"),
            summary=base.with_suffix(".summary.json"),
            stdout=base.with_suffix(".out"),
        )

    def start_background(self, spec: LaunchSpec, sessions: dict[str, SessionRecord]) -> JobRecord:
        reason = self.busy_reason(spec.branch, sessions)
        if reason:
            raise BusyError(reason)
        paths = self.new_run_paths(spec.branch)
        argv = build_argv(self.cfg, self.repo, spec, paths)
        paths.log.parent.mkdir(parents=True, exist_ok=True)
        with paths.stdout.open("wb") as out:
            proc = subprocess.Popen(
                argv,
                cwd=self.repo,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # survive the TUI; own process group for stop()
            )
        self._procs[spec.branch] = proc
        job = JobRecord(
            branch=spec.branch,
            spec=spec,
            pid=proc.pid,
            started_at=_now(),
            argv=argv,
            log_path=str(paths.log),
            summary_path=str(paths.summary),
            stdout_path=str(paths.stdout),
        )
        self.jobs[spec.branch] = job
        self.save(job)
        return job

    @contextlib.contextmanager
    def interactive(
        self, spec: LaunchSpec, sessions: dict[str, SessionRecord]
    ) -> Iterator[JobRecord]:
        """Hold the branch for the duration of an in-terminal session."""
        reason = self.busy_reason(spec.branch, sessions)
        if reason:
            raise BusyError(reason)
        job = JobRecord(
            branch=spec.branch,
            spec=spec,
            pid=os.getpid(),  # the TUI itself holds the terminal
            started_at=_now(),
            argv=build_argv(self.cfg, self.repo, spec),
            notified=True,  # nothing to notify: the user is watching
        )
        self.jobs[spec.branch] = job
        self.save(job)
        try:
            yield job
        finally:
            job.ended_at = _now()
            job.status = status_for_exit(job.exit_code)
            self.save(job)

    def record_external(self, spec: LaunchSpec, argv: list[str]) -> JobRecord:
        """Note an interactive session started in another terminal window.

        Its liveness comes from project-sandbox session records afterwards, so
        the record is closed on the next refresh in which no session is seen
        for the branch (after a grace period covering the image build).
        """
        job = JobRecord(
            branch=spec.branch,
            spec=spec,
            pid=0,
            started_at=_now(),
            argv=argv,
            notified=True,
            external=True,
        )
        self.jobs[spec.branch] = job
        self.save(job)
        return job

    # ------------------------------------------------------------ polling

    def _process_exit(self, job: JobRecord) -> tuple[bool, int | None]:
        """Return (finished, returncode-if-known) for a background job."""
        proc = self._procs.get(job.branch)
        if proc is not None and proc.pid == job.pid:
            code = proc.poll()
            return code is not None, code
        # Adopted from an earlier TUI run: not our child, so no returncode.
        summary = Path(job.summary_path) if job.summary_path else None
        if summary is not None and summary.is_file():
            return True, None
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
        finished: list[JobRecord] = []
        now = time.time()
        for job in list(self.jobs.values()):
            if not job.running:
                if not job.notified:
                    finished.append(job)  # finished while the TUI was closed
                continue
            rec = sessions.get(job.branch)
            if rec is not None:
                job.session_id = job.session_id or rec.session_id
                job.container_name = job.container_name or rec.container_name

            if job.external:
                started = _parse_ts(job.started_at)
                if rec is None and now - started > external_grace_s:
                    job.status = "completed"
                    job.ended_at = _now()
                    self.save(job)
                continue

            if job.mode == "interactive":
                if not pid_alive(job.pid):
                    # The TUI that held this branch died mid-session.
                    job.status = "interrupted"
                    job.ended_at = _now()
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
                if isinstance(summary.get("exit_code"), int):
                    code = summary["exit_code"]
            job.exit_code = code
            job.status = status_for_exit(code)
            if job.branch in self._stop_requested and job.status in ("failed", "orphaned"):
                job.status = "interrupted"
            job.ended_at = _now()
            self._procs.pop(job.branch, None)
            self._stop_requested.pop(job.branch, None)
            self.save(job)
            finished.append(job)
        return finished

    def mark_notified(self, job: JobRecord) -> None:
        job.notified = True
        self.save(job)

    # ------------------------------------------------------------ stopping

    def stop(self, branch: str, sessions: dict[str, SessionRecord]) -> str:
        """Escalating stop: SIGINT → SIGTERM → SIGKILL + container stop.

        SIGINT first lets project-sandbox run its ``finally`` teardown (commit
        work onto the branch, close the session record). Repeated calls escalate.
        """
        job = self.jobs.get(branch)
        if job is None or not job.running:
            return f"{branch}: nothing running"
        if job.mode != "background":
            return f"{branch}: interactive sessions are stopped from their terminal"
        attempts = self._stop_requested.get(branch)
        self._stop_requested[branch] = time.time()
        if attempts is None:
            sig, label = signal.SIGINT, "interrupt"
        elif attempts and time.time() - attempts < 2:
            return f"{branch}: stop already requested"
        else:
            sig, label = signal.SIGKILL, "kill"
        try:
            os.killpg(job.pid, sig)
        except ProcessLookupError:
            return f"{branch}: process already gone"
        except PermissionError:
            return f"{branch}: not permitted to signal pid {job.pid}"
        if sig == signal.SIGKILL:
            name = job.container_name or (
                sessions[branch].container_name if branch in sessions else ""
            )
            cli = runtime_cli(self.cfg.sandbox.runtime)
            if name and cli:
                subprocess.run(
                    [cli, "stop", name],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                return f"{branch}: killed, stopped container {name}"
        return f"{branch}: sent {label}" + (" (press x again to force)" if attempts is None else "")


def _parse_ts(value: str) -> float:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return 0.0
