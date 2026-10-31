"""Opt-in integration with the real project-sandbox CLI and container runtime.

Run with scripts/test.sh -n 0 tests/test_container_smoke.py --container-smoke.
No fake agent, API keys, or calls to a model provider are involved.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from project_sandbox_tui import doctor
from project_sandbox_tui.config import Config
from project_sandbox_tui.jobs import BusyError, JobManager, JobRecord
from project_sandbox_tui.sandbox import LaunchSpec, read_sessions, running_by_branch, runtime_cli
from project_sandbox_tui.vcs import workspace_path
from tests.conftest import run

pytestmark = pytest.mark.container_smoke

BRANCH = "smoke/bash"
BUILD_DEADLINE = 20 * 60  # first run downloads and builds the sandbox image


def _tail(path: str) -> str:
    try:
        with Path(path).open("rb") as stream:
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - 16_384))
            return stream.read().decode("utf-8", errors="replace")
    except OSError:
        return "(not written)"


def _diagnostics(job: JobRecord) -> str:
    return (
        f"status={job.status}, exit={job.exit_code}\n"
        f"CLI output ({job.stdout_path}):\n{_tail(job.stdout_path)}\n"
        f"Agent log ({job.log_path}):\n{_tail(job.log_path)}"
    )


def _wait(manager: JobManager, ready: Callable[[], bool], timeout: float) -> JobRecord:
    deadline = time.monotonic() + timeout
    while True:
        sessions = running_by_branch(read_sessions(), manager.repo, [BRANCH], None)
        manager.refresh(sessions)
        job = manager.jobs[BRANCH]
        if ready():
            return job
        if not job.running:
            pytest.fail(f"Container exited before the expected event.\n{_diagnostics(job)}")
        if time.monotonic() >= deadline:
            pytest.fail(f"Container smoke deadline exceeded.\n{_diagnostics(job)}")
        time.sleep(0.2)


def _runtime(cli: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([cli, *args], capture_output=True, text=True, timeout=30)


def _running_containers(cli: str) -> set[str]:
    args = ("list", "--quiet") if cli == "container" else ("ps", "--format", "{{.Names}}")
    result = _runtime(cli, *args)
    assert result.returncode == 0, result.stderr
    return set(result.stdout.splitlines())


def _check_finished(manager: JobManager, cli: str, code: int, status: str) -> JobRecord:
    job = _wait(manager, lambda: not manager.jobs[BRANCH].running, 180)
    detail = _diagnostics(job)
    assert (job.exit_code, job.status) == (code, status), detail
    summary = json.loads(Path(job.summary_path).read_text())
    assert summary["exit_code"] == code, detail
    assert summary["agent"] == "bash", detail
    assert summary["bookmark"] == BRANCH, detail
    assert Path(summary["workspace_path"]).resolve() == workspace_path(manager.repo, BRANCH, None)
    assert job.session_id == summary["session_id"] and job.session_id, detail
    assert job.container_name == summary["container_name"] and job.container_name, detail
    records = [record for record in read_sessions() if record.session_id == job.session_id]
    assert len(records) == 1 and not records[0].running, detail
    assert job.container_name not in _running_containers(cli), detail
    assert Path(job.log_path).is_file(), detail
    # Bash emits plain text; unlike JSON agent logs, it need not produce a .md transcript.
    assert manager.mark_notified(job)
    assert JobManager(manager.repo, manager.cfg).refresh({}) == []
    return job


def _cleanup(manager: JobManager, cli: str, image: str) -> None:
    # Only our child processes, isolated session records, and unique image tag.
    # Never prune a runtime or act on the user's global session records.
    for proc in manager._procs.values():
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
    for record in read_sessions():
        if (
            Path(record.project_path).resolve() != manager.repo.resolve()
            or not record.container_name
        ):
            continue
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            if cli == "container":
                _runtime(cli, "stop", record.container_name)
                _runtime(cli, "rm", record.container_name)
            else:
                _runtime(cli, "rm", "--force", record.container_name)
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        result = _runtime(cli, "image", "rm", image)
        if result.returncode:
            print(f"Smoke image cleanup ({image}): {result.stderr.strip()}")


def test_real_bash_container_lifecycle(
    jj_repo: Path, pytestconfig: pytest.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config()
    cfg.sandbox.command = pytestconfig.getoption("--container-command")
    cfg.sandbox.runtime = pytestconfig.getoption("--container-runtime")
    cfg.sandbox.no_forward_credentials = True
    cfg.project.base_image = pytestconfig.getoption("--container-base-image")
    cfg.project.keep_workspace = True
    cfg.validate()
    # Opting in makes missing prerequisites failures, never a misleading green skip.
    for check in (
        doctor.check_jj(doctor.run),
        doctor.check_project_sandbox(cfg, doctor.run),
        doctor.check_runtime(cfg, doctor.run, shutil.which),
    ):
        assert check.status == "ok", check.render()
    cli = runtime_cli(cfg.sandbox.runtime)
    assert cli is not None
    cfg.sandbox.runtime = "apple-container" if cli == "container" else cli
    image = f"sbx-tui-smoke-{uuid.uuid4().hex}:test"
    cfg.sandbox.extra_args = [f"--image-tag={image}"]
    monkeypatch.setenv("JJ_USER", "Container Smoke")
    monkeypatch.setenv("JJ_EMAIL", "smoke@example.invalid")
    run("jj", "bookmark", "create", BRANCH, "-r", "main", cwd=jj_repo)
    main_before = run("jj", "log", "--no-graph", "-r", "main", "-T", "commit_id", cwd=jj_repo)
    workspace = workspace_path(jj_repo, BRANCH, None)
    manager = JobManager(jj_repo, cfg)
    marker = f"bash-smoke-{uuid.uuid4().hex}"
    script = (
        "set -euo pipefail\n"
        'test "$PWD" = /workspace\n'
        "jj root\n"
        f"printf '%s\\n' '{marker}' | tee smoke-proof.txt\n"
        "while [ ! -f smoke-release ]; do sleep 0.2; done\n"
        "rm smoke-release\n"
        "jj describe -m 'bash container smoke'\n"
    )
    print(f"Container smoke: runtime={cfg.sandbox.runtime}, image={image}, repo={jj_repo}")
    try:
        manager.start_background(
            LaunchSpec(BRANCH, "bash", mode="background", prompt_text=script, timeout=120), {}
        )
        _wait(manager, lambda: (workspace / "smoke-proof.txt").exists(), BUILD_DEADLINE)
        sessions = running_by_branch(read_sessions(), jj_repo, [BRANCH], None)
        assert BRANCH in sessions
        assert sessions[BRANCH].container_name in _running_containers(cli)
        # Restart the TUI while Bash is waiting in the actual container.
        adopted = JobManager(jj_repo, cfg)
        assert adopted.busy_reason(BRANCH, sessions)
        with pytest.raises(BusyError):
            adopted.start_background(
                LaunchSpec(BRANCH, "bash", mode="background", prompt_text="exit 99"), sessions
            )
        (workspace / "smoke-release").write_text("continue\n")
        success = _check_finished(adopted, cli, 0, "completed")
        assert marker in Path(success.log_path).read_text(), _diagnostics(success)
        assert (
            run("jj", "file", "show", "-r", BRANCH, "smoke-proof.txt", cwd=jj_repo).strip()
            == marker
        )
        assert not (jj_repo / "smoke-proof.txt").exists()
        assert main_before == run(
            "jj", "log", "--no-graph", "-r", "main", "-T", "commit_id", cwd=jj_repo
        )
        # Reuse the same built image and workspace for failure and timeout propagation.
        cfg.sandbox.extra_args.append("--no-build")
        for prompt, timeout, code, status in (
            ("printf 'expected failure\\n'; exit 7", 120, 7, "failed"),
            ("printf 'waiting for timeout\\n'; sleep 60", 5, 124, "timeout"),
        ):
            manager.start_background(
                LaunchSpec(BRANCH, "bash", mode="background", prompt_text=prompt, timeout=timeout),
                {},
            )
            done = _check_finished(manager, cli, code, status)
            if code == 7:
                assert "expected failure" in Path(done.log_path).read_text(), _diagnostics(done)
    finally:
        _cleanup(manager, cli, image)
