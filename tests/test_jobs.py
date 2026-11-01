from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, tzinfo
from pathlib import Path

import pytest

from project_sandbox_tui.config import Config
from project_sandbox_tui.jobs import BusyError, JobManager, JobRecord, status_for_exit
from project_sandbox_tui.sandbox import (
    LaunchSpec,
    SessionRecord,
    pid_alive,
    read_sessions,
    running_by_branch,
)
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
        (-signal.SIGINT, "interrupted"),
        (-signal.SIGTERM, "failed"),
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


def test_cancel_interrupts(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    jobs = JobManager(repo, fake_cfg())
    jobs.start_background(bg("main", "sleep:30"), {})
    wait_for(lambda: "main" in sessions_for(repo, ["main"]))
    assert "sent SIGINT" in jobs.cancel("main", {})
    finished: list = []
    wait_for(lambda: bool(finished.extend(jobs.refresh({})) or finished))
    assert jobs.jobs["main"].status == "interrupted"
    # Once finished, the next press clears the record at once.
    assert "interrupted status cleared" in jobs.cancel("main", {})
    assert "main" not in JobManager(repo, fake_cfg()).jobs
    assert "nothing to cancel" in jobs.cancel("main", {})


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


def test_cancel_escalates_one_step_per_press(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = JobManager(repo, fake_cfg())
    job = jobs.start_background(bg("main", "sleep:30 ignore-signals"), {})
    wait_for(lambda: "main" in sessions_for(repo, ["main"]))
    assert "SIGINT" in jobs.cancel("main", {})
    assert "already requested" in jobs.cancel("main", {})  # double press is debounced
    monkeypatch.setattr(JobManager, "STOP_DEBOUNCE_S", 0.0)
    assert "SIGTERM" in jobs.cancel("main", {})
    assert jobs.jobs["main"].running  # ignored
    message = jobs.cancel("main", {})
    assert "SIGKILL" in message and "record forgotten" in message
    assert "main" not in jobs.jobs and "main" not in JobManager(repo, fake_cfg()).jobs
    # The forgotten child is still reaped, so it does not linger as a zombie.
    wait_for(lambda: jobs.refresh({}) == [] and not pid_alive(job.pid))
    assert not jobs.busy_reason("main", {})


def test_cancel_forgets_unsignallable_records_on_third_press(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from project_sandbox_tui.jobs import _now

    jobs = JobManager(tmp_path, Config())
    monkeypatch.setattr(JobManager, "STOP_DEBOUNCE_S", 0.0)
    jobs.save(JobRecord("topic", LaunchSpec("topic", "claude"), os.getpid(), _now()))
    jobs.save(JobRecord("broken", bg("broken", "go"), 0, _now()))
    assert "stopped from their terminal; x 2 more times" in jobs.cancel("topic", {})
    assert "invalid process id 0; x 2 more times" in jobs.cancel("broken", {})
    assert "x 1 more time forgets" in jobs.cancel("topic", {})
    assert "no process was signalled" in jobs.cancel("topic", {})
    assert set(JobManager(tmp_path, Config()).jobs) == {"broken"}


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
    # Session gone → done, no 10-minute wait; but only once it stays gone: one
    # refresh may miss a session record that is being rewritten.
    jobs.refresh(sessions_for(repo, ["main"]))
    assert jobs.jobs["main"].running
    jobs.refresh(sessions_for(repo, ["main"]))
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
    "raw",
    [
        "{",
        "[]",
        "{}",
        '{"branch": "x", "spec": []}',
        '{"branch": "x", "spec": "bad"}',
        '{"branch": "x", "pid": true}',
        '{"branch": "x", "pid": 1e100}',
        '{"branch": "x", "pid": 1.5}',
        '{"branch": "x", "pid": -1}',
        '{"branch": "x", "pid": 2147483648}',
        '{"branch": "x", "spec": {"timeout": Infinity}}',
    ],
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
    adopted.cancel("topic", {})
    adopted.cancel("topic", {})
    message = adopted.cancel("topic", {})
    assert "sent SIGKILL" in message and "container stop failed" in message
    assert spawn.call_args.args[0] == ["podman", "stop", "sandbox-container"]


@pytest.mark.parametrize("action", ["discard", "mark_notified", "interactive-exit"])
def test_stale_job_cannot_change_replacement_run(
    tmp_path: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    from contextlib import nullcontext

    first = JobManager(tmp_path, fake_cfg())
    second = JobManager(tmp_path, fake_cfg())
    spec = LaunchSpec("topic", "claude")
    monkeypatch.setattr("project_sandbox_tui.jobs._now", lambda: "2026-01-01T00:00:00Z")
    reservation = (
        first.interactive(spec, {})
        if action == "interactive-exit"
        else nullcontext(first.record_external(spec, ["old"]))
    )
    with reservation as old:
        assert second.clear("topic", force=True)
        monkeypatch.setattr("project_sandbox_tui.jobs._now", lambda: "2026-01-01T00:00:01Z")
        new = second.record_external(spec, ["new"])
        new.notified = False
        second.save(new)
        if action == "discard":
            first.discard(old)
        elif action == "mark_notified":
            assert not first.mark_notified(old)
        else:
            old.exit_code = 0
    assert JobManager(tmp_path, fake_cfg()).jobs["topic"] == new


@pytest.mark.parametrize("raw", [None, "{", "[]", '{"exit_code": "0"}', '{"exit_code": false}'])
def test_unusable_summary_does_not_report_success(tmp_path: Path, raw: str | None) -> None:
    jobs = JobManager(tmp_path, Config())
    summary = tmp_path / "summary.json"
    if raw is not None:
        summary.write_text(raw)
    jobs.save(JobRecord("topic", bg("topic", "go"), 0, "", summary_path=str(summary)))
    [finished] = jobs.refresh({})
    assert finished.status == "orphaned" and finished.exit_code is None


def test_summary_exit_code_takes_precedence_over_child_exit(
    tmp_path: Path, fake_cfg: Callable[..., Config]
) -> None:
    cfg = fake_cfg()
    cfg.sandbox.command = "true"
    jobs = JobManager(tmp_path, cfg)
    job = jobs.start_background(bg("topic", "go"), {})
    jobs._procs["topic"].wait(timeout=5)
    Path(job.summary_path).write_text(json.dumps({"exit_code": 75}))
    [finished] = jobs.refresh({})
    assert (finished.status, finished.exit_code) == ("rate_limited", 75)


def test_failed_record_replace_preserves_previous_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = JobManager(tmp_path, Config())
    old = jobs.record_external(LaunchSpec("topic", "claude"), ["old"])

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        jobs.save(JobRecord("topic", bg("topic", "new"), 0, "later"))
    assert JobManager(tmp_path, Config()).jobs["topic"] == old
    assert list(jobs.jobs_dir.glob("*.tmp")) == []


def test_boolean_exit_code_is_not_adopted_as_success() -> None:
    job = JobRecord.from_dict({"branch": "topic", "exit_code": False})
    assert job.exit_code is None


@pytest.mark.parametrize("same_pid", [False, True])
def test_cancel_escalation_belongs_to_one_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_pid: bool
) -> None:
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    first = JobManager(tmp_path, Config())
    second = JobManager(tmp_path, Config())
    first.save(JobRecord("topic", bg("topic", "old"), 12345, "2026-01-01T00:00:00Z"))
    assert "SIGINT" in first.cancel("topic", {})
    pid = 12345 if same_pid else 12346
    second.save(JobRecord("topic", bg("topic", "new"), pid, "2026-01-01T00:01:00Z"))
    # A replacement run neither inherits the debounce nor skips its first signal.
    assert "SIGINT" in first.cancel("topic", {})
    assert signals == [(12345, signal.SIGINT), (pid, signal.SIGINT)]
    second.clear("topic", force=True)
    first.load()
    assert not first.cancel_started("topic")


def test_failed_final_save_keeps_the_return_code(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = JobManager(repo, fake_cfg())
    job = jobs.start_background(bg("main", "exit:3"), {})
    jobs._procs["main"].wait()  # the child exited and was reaped; only Popen has its code
    Path(job.summary_path).unlink(missing_ok=True)  # no summary to fall back on

    def full_disk(_job: JobRecord) -> None:
        raise OSError("disk full")

    real_save = jobs.save
    monkeypatch.setattr(jobs, "save", full_disk)
    with pytest.raises(OSError):
        jobs.refresh({})
    monkeypatch.setattr(jobs, "save", real_save)
    [done] = jobs.refresh({})
    assert (done.status, done.exit_code) == ("failed", 3)


def test_load_swaps_in_a_complete_dict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker threads load while the UI thread iterates the jobs."""
    jobs = JobManager(tmp_path, Config(), tmp_path / "state")
    for branch in ("a", "b"):
        jobs.save(JobRecord(branch, LaunchSpec(branch=branch, agent="bash"), 0, "2026-01-01Z"))
    old = jobs.jobs
    during: list[bool] = []
    real = JobRecord.from_dict

    def spy(raw: dict) -> JobRecord:
        during.append(jobs.jobs is old)
        return real(raw)

    monkeypatch.setattr(JobRecord, "from_dict", spy)
    jobs.load()
    assert during == [True, True]
    assert jobs.jobs is not old and set(jobs.jobs) == {"a", "b"}


def test_external_session_survives_one_missed_refresh(tmp_path: Path) -> None:
    jobs = JobManager(tmp_path, Config(), tmp_path / "state")
    jobs.record_external(LaunchSpec(branch="topic", agent="claude"), ["sandbox"])
    session = SessionRecord("s1", "c1", str(tmp_path), "/ws/topic", "claude", "running", 1, "")
    jobs.refresh({"topic": session})
    jobs.refresh({})  # e.g. read while project-sandbox rewrote its record
    jobs.refresh({"topic": session})
    jobs.refresh({})
    assert jobs.jobs["topic"].running  # never two misses in a row
    jobs.refresh({})
    assert jobs.jobs["topic"].status == "completed"


def test_now_always_has_microseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Records sort these strings: a missing fraction would sort after every other."""
    import project_sandbox_tui.jobs as jobs_module

    class Fixed:
        @staticmethod
        def now(tz: tzinfo | None = None) -> datetime:
            return datetime(2026, 1, 2, 3, 4, 5, 0, tzinfo=tz)

    monkeypatch.setattr(jobs_module, "datetime", Fixed)
    assert jobs_module._now() == "2026-01-02T03:04:05.000000Z"


def test_replaced_and_forgotten_records_move_to_history(tmp_path: Path) -> None:
    jobs = JobManager(tmp_path, Config())
    first = jobs.record_external(LaunchSpec("topic", "codex"), ["first"])
    first.status, first.session_id = "completed", "session-1"
    jobs.save(first)
    second = jobs.record_external(LaunchSpec("topic", "codex"), ["second"])
    second.status = "completed"
    jobs.save(second)
    assert [j.session_id for j in jobs.history("topic")] == ["session-1"]
    assert jobs.cancel("topic", {}).endswith("status cleared")  # x on a finished run
    fresh = JobManager(tmp_path, Config())
    assert "topic" not in fresh.jobs  # history is not adopted as current records
    assert [j.argv for j in fresh.history("topic")] == [["second"], ["first"]]
    assert fresh.history("other") == []


def test_history_keeps_newest_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import project_sandbox_tui.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "HISTORY_PER_BRANCH", 2)
    jobs = JobManager(tmp_path, Config())
    for n in range(4):
        started = f"2026-01-0{n + 1}T00:00:00Z"
        jobs.save(JobRecord("topic", bg("topic", str(n)), 0, started, status="completed"))
        assert jobs.clear("topic")
    assert [j.spec.prompt_text for j in jobs.history("topic")] == ["3", "2"]
