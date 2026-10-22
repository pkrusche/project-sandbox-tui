from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from project_sandbox_tui.config import Config
from project_sandbox_tui.jobs import BusyError, JobManager, status_for_exit
from project_sandbox_tui.sandbox import LaunchSpec, read_sessions, running_by_branch
from tests.conftest import FAKE, wait_for


def sessions_for(repo: Path, branches: list[str]):  # type: ignore[no-untyped-def]
    return running_by_branch(read_sessions(), repo, "git", branches, None)


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


def test_background_job_lifecycle(git_repo: Path, fake_cfg: Callable[..., Config]) -> None:
    jobs = JobManager(git_repo, fake_cfg())
    job = jobs.start_background(bg("main", "sleep:0.5 exit:0"), {})
    assert job.running and jobs.running_count() == 1

    # one sandbox per branch — guarded by our record and by session records
    with pytest.raises(BusyError):
        jobs.start_background(bg("main", "again"), {})
    wait_for(lambda: "main" in sessions_for(git_repo, ["main"]) or not jobs.jobs["main"].running)

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


def test_failure_and_readoption(git_repo: Path, fake_cfg: Callable[..., Config]) -> None:
    cfg = fake_cfg()
    first = JobManager(git_repo, cfg)
    first.start_background(bg("feature/x", "sleep:0.8 exit:3"), {})

    # A fresh manager (TUI restart) adopts the still-running job from disk.
    second = JobManager(git_repo, cfg)
    assert second.jobs["feature/x"].running
    finished: list = []
    wait_for(lambda: bool(finished.extend(second.refresh({})) or finished))
    job = second.jobs["feature/x"]
    assert (job.status, job.exit_code) == ("failed", 3)  # read from --json-summary

    # Not notified yet → a third start still reports it.
    third = JobManager(git_repo, cfg)
    assert [j.branch for j in third.refresh({})] == ["feature/x"]
    assert third.clear("feature/x") and "feature/x" not in JobManager(git_repo, cfg).jobs


def test_stop_interrupts(git_repo: Path, fake_cfg: Callable[..., Config]) -> None:
    jobs = JobManager(git_repo, fake_cfg())
    jobs.start_background(bg("main", "sleep:30"), {})
    wait_for(lambda: "main" in sessions_for(git_repo, ["main"]))
    assert "sent interrupt" in jobs.stop("main", {})
    finished: list = []
    wait_for(lambda: bool(finished.extend(jobs.refresh({})) or finished))
    assert jobs.jobs["main"].status == "interrupted"


def test_busy_from_external_session(git_repo: Path, fake_cfg: Callable[..., Config]) -> None:
    """A project-sandbox run started outside the TUI also blocks the branch."""
    jobs = JobManager(git_repo, fake_cfg())
    proc = subprocess.Popen(
        [
            sys.executable,
            str(FAKE),
            str(git_repo),
            "img",
            "--branch",
            "main",
            "--prompt-text",
            "sleep:30",
        ],
        stdout=subprocess.DEVNULL,
    )
    try:
        wait_for(lambda: "main" in sessions_for(git_repo, ["main"]))
        sessions = sessions_for(git_repo, ["main"])
        assert "running project-sandbox session" in (jobs.busy_reason("main", sessions) or "")
        with pytest.raises(BusyError):
            jobs.start_background(bg("main", "x"), sessions)
    finally:
        proc.terminate()
        proc.wait()


def test_interactive_holds_branch(git_repo: Path, fake_cfg: Callable[..., Config]) -> None:
    jobs = JobManager(git_repo, fake_cfg())
    spec = LaunchSpec(branch="main", agent="codex")
    with jobs.interactive(spec, {}) as job:
        assert jobs.busy_reason("main", {})
        assert JobManager(git_repo, fake_cfg()).busy_reason("main", {})  # visible on disk
        job.exit_code = 0
    assert not jobs.busy_reason("main", {})
    assert jobs.jobs["main"].status == "completed"
    assert jobs.refresh({}) == []  # interactive runs don't notify
