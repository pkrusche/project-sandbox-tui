from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from project_sandbox_tui.config import Config
from project_sandbox_tui.jobs import BusyError, JobManager, status_for_exit
from project_sandbox_tui.sandbox import LaunchSpec, SessionRecord, read_sessions, running_by_branch
from tests.conftest import FAKE, wait_for


def sessions_for(repo: Path, branches: list[str]) -> dict[str, SessionRecord]:
    return running_by_branch(read_sessions(), repo, branches, None)


def bg(branch: str, prompt: str) -> LaunchSpec:
    return LaunchSpec(branch=branch, agent="claude", mode="background", prompt_text=prompt)


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (0, "completed"),
        (1, "failed"),
        (124, "timeout"),
        (75, "rate_limited"),
        (130, "interrupted"),
        (None, "orphaned"),
    ],
)
def test_status_for_exit(code: int | None, status: str) -> None:
    assert status_for_exit(code) == status


def test_background_job_lifecycle(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    jobs = JobManager(repo, fake_cfg())
    job = jobs.start_background(bg("main", "sleep:0.5 exit:0"), {})
    assert job.running and jobs.running_count() == 1

    # one sandbox per branch — guarded by our record and by session records
    with pytest.raises(BusyError):
        jobs.start_background(bg("main", "again"), {})
    wait_for(lambda: "main" in sessions_for(repo, ["main"]) or not jobs.jobs["main"].running)

    finished: list = []
    wait_for(lambda: bool(finished.extend(jobs.refresh({})) or finished))
    assert [j.branch for j in finished] == ["main"]
    done = jobs.jobs["main"]
    assert (done.status, done.exit_code) == ("completed", 0)
    assert done.session_id and done.container_name.startswith("project-sandbox-")
    assert Path(done.log_path).read_text() == "log for main\n"
    assert done.output_files()[0].name.endswith(".md")
    assert done.output_files()[0].exists()

    jobs.mark_notified(done)
    assert jobs.refresh({}) == []  # notified once only


def test_failure_and_readoption(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    cfg = fake_cfg()
    first = JobManager(repo, cfg)
    first.start_background(bg("feature/x", "sleep:0.8 exit:3"), {})

    # A fresh manager (TUI restart) adopts the still-running job from disk.
    second = JobManager(repo, cfg)
    assert second.jobs["feature/x"].running
    finished: list = []
    wait_for(lambda: bool(finished.extend(second.refresh({})) or finished))
    job = second.jobs["feature/x"]
    assert (job.status, job.exit_code) == ("failed", 3)  # read from --json-summary

    # Not notified yet → a third start still reports it.
    third = JobManager(repo, cfg)
    assert [j.branch for j in third.refresh({})] == ["feature/x"]
    assert third.clear("feature/x") and "feature/x" not in JobManager(repo, cfg).jobs


def test_stop_interrupts(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    jobs = JobManager(repo, fake_cfg())
    jobs.start_background(bg("main", "sleep:30"), {})
    wait_for(lambda: "main" in sessions_for(repo, ["main"]))
    assert "sent SIGINT" in jobs.stop("main", {})
    finished: list = []
    wait_for(lambda: bool(finished.extend(jobs.refresh({})) or finished))
    assert jobs.jobs["main"].status == "interrupted"


def test_busy_from_external_session(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    """A project-sandbox run started outside the TUI also blocks the branch."""
    jobs = JobManager(repo, fake_cfg())
    proc = subprocess.Popen(
        [
            sys.executable,
            str(FAKE),
            str(repo),
            "img",
            "--branch",
            "main",
            "--prompt-text",
            "sleep:30",
        ],
        stdout=subprocess.DEVNULL,
    )
    try:
        wait_for(lambda: "main" in sessions_for(repo, ["main"]))
        sessions = sessions_for(repo, ["main"])
        assert "running project-sandbox session" in (jobs.busy_reason("main", sessions) or "")
        with pytest.raises(BusyError):
            jobs.start_background(bg("main", "x"), sessions)
    finally:
        proc.terminate()
        proc.wait()


def test_interactive_holds_branch(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    jobs = JobManager(repo, fake_cfg())
    spec = LaunchSpec(branch="main", agent="codex")
    with jobs.interactive(spec, {}) as job:
        assert jobs.busy_reason("main", {})
        assert JobManager(repo, fake_cfg()).busy_reason("main", {})  # visible on disk
        job.exit_code = 0
    assert not jobs.busy_reason("main", {})
    assert jobs.jobs["main"].status == "completed"
    assert jobs.refresh({}) == []  # interactive runs don't notify


def test_stop_escalates_one_step_per_press(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = JobManager(repo, fake_cfg())
    jobs.start_background(bg("main", "sleep:30 ignore-signals"), {})
    wait_for(lambda: "main" in sessions_for(repo, ["main"]))
    assert "SIGINT" in jobs.stop("main", {})
    assert "already requested" in jobs.stop("main", {})  # double press is debounced
    monkeypatch.setattr(JobManager, "STOP_DEBOUNCE_S", 0.0)
    assert "SIGTERM" in jobs.stop("main", {})
    assert jobs.jobs["main"].running  # ignored
    assert "SIGKILL" in jobs.stop("main", {})
    finished: list = []
    wait_for(lambda: bool(finished.extend(jobs.refresh({})) or finished))
    assert jobs.jobs["main"].status == "interrupted"


def test_external_session_closes_when_it_ends(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    jobs = JobManager(repo, fake_cfg())
    spec = LaunchSpec(branch="main", agent="claude")
    jobs.record_external(spec, ["project-sandbox"])
    assert jobs.refresh({}) == [] and jobs.jobs["main"].running  # still building: no session yet

    proc = subprocess.Popen(
        [
            sys.executable,
            str(FAKE),
            str(repo),
            "img",
            "--branch",
            "main",
            "--prompt-text",
            "sleep:0.5",
        ],
        stdout=subprocess.DEVNULL,
    )
    wait_for(lambda: "main" in sessions_for(repo, ["main"]))
    jobs.refresh(sessions_for(repo, ["main"]))
    assert jobs.jobs["main"].session_seen and jobs.jobs["main"].running
    proc.wait()
    jobs.refresh(sessions_for(repo, ["main"]))  # session gone → done, no 10-minute wait
    assert jobs.jobs["main"].status == "completed"

    # an external launch whose session never shows up closes after the grace period
    jobs.clear("main")
    jobs.record_external(spec, ["project-sandbox"])
    jobs.refresh({}, external_grace_s=-1)
    assert jobs.jobs["main"].status == "completed"


def test_force_clear_drops_stuck_record(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    jobs = JobManager(repo, fake_cfg())
    jobs.record_external(LaunchSpec(branch="main", agent="claude"), ["x"])
    assert not jobs.clear("main")
    assert jobs.clear("main", force=True)
    assert "main" not in JobManager(repo, fake_cfg()).jobs


def test_running_guard_reads_other_instances(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    first = JobManager(repo, fake_cfg())
    second = JobManager(repo, fake_cfg())  # already open before the reservation
    spec = LaunchSpec(branch="main", agent="claude")
    job = first.record_external(spec, ["project-sandbox"])
    assert second.busy_reason("main", {})
    with pytest.raises(BusyError):
        second.start_background(bg("main", "sleep:30"), {})
    with pytest.raises(BusyError), second.interactive(spec, {}):
        pass
    with pytest.raises(BusyError):
        second.record_external(spec, ["project-sandbox"])
    assert second.jobs["main"].started_at == job.started_at


def test_simultaneous_reservations_are_serialized(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)
    managers = [JobManager(tmp_path, Config()) for _ in range(2)]

    def reserve(manager: JobManager) -> bool:
        barrier.wait(timeout=5)
        try:
            manager.record_external(LaunchSpec(branch="topic", agent="claude"), ["sandbox"])
        except BusyError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, managers)) == [False, True]


def test_refresh_syncs_records_and_notification_claims(tmp_path: Path) -> None:
    first = JobManager(tmp_path, Config())
    second = JobManager(tmp_path, Config())
    job = first.record_external(LaunchSpec(branch="topic", agent="claude"), ["sandbox"])
    second.refresh({})
    observed = second.jobs["topic"]
    job.status = "completed"
    job.notified = False
    first.save(job)
    assert second.refresh({}) == [observed]
    assert observed.status == "completed"  # reference held by a log view stays current
    assert first.mark_notified(job)
    assert not second.mark_notified(observed)
    assert second.refresh({}) == []
    first.clear("topic")
    second.refresh({})
    assert "topic" not in second.jobs


def test_run_paths_preserve_dotted_branches_and_are_unique(tmp_path: Path) -> None:
    jobs = JobManager(tmp_path, Config())
    paths = [jobs.new_run_paths(name) for name in ("release.v1", "release.v2", "release.v1")]
    assert len({p.log for p in paths}) == 3
    assert all("release.v" in p.log.name for p in paths)
    assert all(p.log.with_suffix(".md") == p.transcript() for p in paths)
    assert len({str(p) for run in paths for p in (run.log, run.summary, run.stdout)}) == 9


def test_summary_does_not_finish_a_live_adopted_process(tmp_path: Path) -> None:
    import json

    from project_sandbox_tui.jobs import JobRecord, _now

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        summary = tmp_path / "summary.json"
        summary.write_text(json.dumps({"exit_code": 0}))
        first = JobManager(tmp_path, Config())
        first.save(
            JobRecord("topic", bg("topic", "go"), proc.pid, _now(), summary_path=str(summary))
        )
        second = JobManager(tmp_path, Config())
        assert second.refresh({}) == []
        assert second.jobs["topic"].running
    finally:
        proc.terminate()
        proc.wait()
    assert second.refresh({})[0].status == "completed"


@pytest.mark.parametrize(
    "raw", ["[]", '{"branch": "x", "spec": []}', '{"branch": "x", "spec": "bad"}']
)
def test_malformed_records_are_ignored(tmp_path: Path, raw: str) -> None:
    jobs = JobManager(tmp_path, Config())
    jobs.jobs_dir.mkdir(parents=True)
    (jobs.jobs_dir / "broken.json").write_text(raw)
    jobs.load()
    assert jobs.jobs == {}


def test_failed_record_write_stops_detached_child(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = JobManager(repo, fake_cfg())
    children = []
    real_popen = subprocess.Popen

    def spawn(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        children.append(proc)
        return proc

    def fail_save(job):
        raise OSError("disk full")

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(jobs, "save", fail_save)
    with pytest.raises(OSError, match="disk full"):
        jobs.start_background(bg("main", "sleep:30"), {})
    assert len(children) == 1 and children[0].poll() is not None
    assert not jobs.jobs and not jobs._procs


def test_parse_session_timestamps() -> None:
    from project_sandbox_tui.jobs import _parse_ts

    assert _parse_ts("2026-10-22T12:00:00.123456+00:00") == pytest.approx(1792670400.123456)
    assert _parse_ts("2026-10-22T14:00:00+02:00") == _parse_ts("2026-10-22T12:00:00Z")
    assert _parse_ts("invalid") == 0


def test_stop_uses_observed_runtime_and_handles_missing_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock

    from project_sandbox_tui.jobs import JobRecord, _now
    from project_sandbox_tui.sandbox import SessionRecord

    jobs = JobManager(tmp_path, Config())
    job = JobRecord("topic", bg("topic", "go"), 12345, _now())
    jobs.save(job)
    session = SessionRecord(
        "sid", "sandbox-container", "", "", "claude", "running", 12345, _now(), "podman"
    )
    monkeypatch.setattr("project_sandbox_tui.jobs.os.killpg", lambda *args: None)
    monkeypatch.setattr("project_sandbox_tui.jobs.pid_alive", lambda pid: True)
    # Observe the actual runtime, then adopt without a session mapping at stop time.
    jobs.refresh({"topic": session})
    adopted = JobManager(tmp_path, Config())
    monkeypatch.setattr(JobManager, "STOP_DEBOUNCE_S", 0)
    spawn = Mock(side_effect=FileNotFoundError("podman missing"))
    monkeypatch.setattr(subprocess, "Popen", spawn)
    adopted.stop("topic", {})
    adopted.stop("topic", {})
    message = adopted.stop("topic", {})
    assert "sent SIGKILL" in message and "container stop failed" in message
    assert spawn.call_args.args[0] == ["podman", "stop", "sandbox-container"]
