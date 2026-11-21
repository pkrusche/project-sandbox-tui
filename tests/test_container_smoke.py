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

from project_sandbox_tui import doctor, feedback
from project_sandbox_tui.config import Config, WorkspaceSkill
from project_sandbox_tui.jobs import BusyError, JobManager, JobRecord
from project_sandbox_tui.sandbox import LaunchSpec, read_sessions, running_by_branch, runtime_cli
from project_sandbox_tui.vcs import JjBackend, workspace_path
from project_sandbox_tui.workspace_skills import WorkspacePreparer
from tests.conftest import run

pytestmark = pytest.mark.container_smoke

BRANCH = "smoke/bash"
SKILL = ".agents/skills/smoke-skill"  # ignored by BRANCH's .gitignore
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


def _wait(
    manager: JobManager, ready: Callable[[], bool], timeout: float, branch: str = BRANCH
) -> JobRecord:
    deadline = time.monotonic() + timeout
    while True:
        sessions = running_by_branch(read_sessions(), manager.repo, [branch], None)
        manager.refresh(sessions)
        job = manager.jobs[branch]
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
    jj_repo: Path, pytestconfig: pytest.Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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
    main_before = run("jj", "log", "--no-graph", "-r", "main", "-T", "commit_id", cwd=jj_repo)
    # The bookmark ignores the skill; a deployed copy is read and run from /workspace.
    run("jj", "new", "main", cwd=jj_repo)
    (jj_repo / ".gitignore").write_text(f"/{SKILL}/\n")
    run("jj", "commit", "-m", "ignore the smoke skill", cwd=jj_repo)
    run("jj", "bookmark", "create", BRANCH, "-r", "@-", cwd=jj_repo)
    run("jj", "new", "main", cwd=jj_repo)
    skill_marker = f"skill-smoke-{uuid.uuid4().hex}"
    source = tmp_path / "smoke-skill"
    (source / "bin").mkdir(parents=True)
    (source / "SKILL.md").write_text(f"---\nname: smoke-skill\n---\n{skill_marker}\n")
    helper = source / "bin" / "helper.sh"
    helper.write_text("#!/bin/sh\nprintf 'helper ran\\n'\n")
    helper.chmod(0o755)
    cfg.workspace_skills = [WorkspaceSkill(str(source), [SKILL])]
    cfg.validate()
    workspace = workspace_path(jj_repo, BRANCH, None)
    manager = JobManager(jj_repo, cfg)
    preparer = WorkspacePreparer(jj_repo, JjBackend(jj_repo))

    def prepare(current: Config, spec: LaunchSpec, sandbox: bool) -> None:  # as the TUI does
        preparer.prepare(current, spec.branch, sandbox=sandbox, extra_args=spec.extra_args)

    manager.prepare = prepare
    marker = f"bash-smoke-{uuid.uuid4().hex}"
    script = (
        "set -euo pipefail\n"
        'test "$PWD" = /workspace\n'
        "jj root\n"
        "test -f /home/agent/.agents/skills/sbx-run-feedback/SKILL.md\n"
        "test -f /home/agent/.claude/skills/sbx-run-feedback/SKILL.md\n"
        "if touch /run/sbx-feedback/context.json; then exit 91; fi\n"
        "python3 /run/sbx-feedback/sbx-report update --summary 'smoke running'\n"
        f"grep -q '{skill_marker}' {SKILL}/SKILL.md\n"
        f"{SKILL}/bin/helper.sh\n"
        f"printf '%s\\n' '{marker}' | tee smoke-proof.txt\n"
        "while [ ! -f smoke-release ]; do sleep 0.2; done\n"
        "rm smoke-release\n"
        "jj describe -m 'bash container smoke'\n"
        "python3 /run/sbx-feedback/sbx-report finish --outcome success --summary 'smoke done'\n"
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
        assert success.reported_success, _diagnostics(success)
        assert feedback.read_json(Path(success.feedback_path))["outcome"] == "success"
        assert "helper ran" in Path(success.log_path).read_text(), _diagnostics(success)
        assert (
            run("jj", "file", "show", "-r", BRANCH, "smoke-proof.txt", cwd=jj_repo).strip()
            == marker
        )
        # The session change is captured; the deployed skills are not.
        tracked = run("jj", "file", "list", "-r", BRANCH, cwd=jj_repo).splitlines()
        assert "smoke-proof.txt" in tracked
        assert not [path for path in tracked if "/skills/" in path], tracked
        assert not (jj_repo / "smoke-proof.txt").exists()
        assert main_before == run(
            "jj", "log", "--no-graph", "-r", "main", "-T", "commit_id", cwd=jj_repo
        )
        # Reuse the same built image and workspace for failure and timeout propagation.
        cfg.sandbox.extra_args.append("--no-build")
        for prompt, timeout, code, status in (
            (
                "python3 /run/sbx-feedback/sbx-report finish --outcome failure --summary failed; "
                "printf 'expected failure\\n'; exit 7",
                120,
                7,
                "failed",
            ),
            ("printf 'waiting for timeout\\n'; sleep 60", 5, 124, "timeout"),
        ):
            manager.start_background(
                LaunchSpec(BRANCH, "bash", mode="background", prompt_text=prompt, timeout=timeout),
                {},
            )
            done = _check_finished(manager, cli, code, status)
            if code == 7:
                assert "expected failure" in Path(done.log_path).read_text(), _diagnostics(done)
                assert done.reported_outcome == "failure" and not done.reported_success
            else:
                assert done.reported_outcome == "unknown"
        # Feedback survives automatic workspace removal.
        cfg.project.keep_workspace = False
        report = "python3 /run/sbx-feedback/sbx-report finish --outcome success --summary done"
        manager.start_background(
            LaunchSpec(BRANCH, "bash", mode="background", prompt_text=report), {}
        )
        removed = _check_finished(manager, cli, 0, "completed")
        assert not workspace.exists()
        assert removed.reported_success and Path(removed.feedback_path).exists()
        # Main checkout uses the identical transport, without a branch workspace.
        manager.start_background(LaunchSpec("@", "bash", mode="background", prompt_text=report), {})
        main = _wait(manager, lambda: not manager.jobs["@"].running, 180, branch="@")
        assert main.reported_success, _diagnostics(main)
    finally:
        _cleanup(manager, cli, image)


def test_real_commit_summary_mount_boundary(
    repo: Path, pytestconfig: pytest.Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import asyncio
    import tempfile

    from project_sandbox_tui import commit_summary

    cfg = Config()
    cfg.sandbox.command = pytestconfig.getoption("--container-command")
    cfg.sandbox.runtime = pytestconfig.getoption("--container-runtime")
    cfg.sandbox.no_forward_credentials = True
    prerequisites = (
        doctor.check_jj(doctor.run),
        doctor.check_project_sandbox(cfg, doctor.run),
        doctor.check_runtime(cfg, doctor.run, shutil.which),
    )
    errors = [check.render() for check in prerequisites if check.status != "ok"]
    assert not errors, "\n".join(errors)
    cli = runtime_cli(cfg.sandbox.runtime)
    assert cli is not None
    cfg.sandbox.runtime = "apple-container" if cli == "container" else cli

    backend = JjBackend(repo)
    commits = backend.review_commits("main", "feature/x")
    assert len(commits) == 1
    sentinel = repo / "a.txt"
    store = repo / ".jj/repo/store"
    assert sentinel.is_file() and store.is_dir()
    original_contents = sentinel.read_bytes()
    image = f"sbx-tui-review-smoke-{uuid.uuid4().hex}:test"
    cfg, spec = commit_summary.resolve_launch(cfg, repo)
    cfg.project.base_image = pytestconfig.getoption("--container-base-image")
    spec.agent = "bash"
    spec.extra_args = [f"--image-tag={image}"]
    spec.timeout = 120
    spec.prompt_text = f"""set -euo pipefail
test "$PWD" = /workspace
test ! -e /workspace/a.txt
test ! -e /workspace/.jj
test ! -e /workspace/.git
if jj root >/dev/null 2>&1; then exit 90; fi
python3 - <<'PYTHON'
import errno
import json
import subprocess
from pathlib import Path

for host_path in ({str(sentinel)!r}, {str(store)!r}):
    assert not Path(host_path).exists(), host_path
root = Path({commit_summary.INPUT!r})
entries = json.loads((root / "commits.json").read_text())
assert len(entries) == 1
entry = entries[0]
assert entry["commit_id"] == {commits[0].commit!r}
patch = root / entry["patch"]
assert "diff --git a/b.txt b/b.txt" in patch.read_text()
try:
    patch.open("a").close()
except OSError as error:
    assert error.errno == errno.EROFS, error
else:
    raise AssertionError("review patch is writable")
state = Path("/tmp/review-state.json")
state.write_text(json.dumps({{"commits": [{{
    "commit_id": entry["commit_id"], "summary": "Mount boundary verified",
    "complexity": "l", "harmful": False,
}}]}}))
subprocess.run([
    "python3", "/run/sbx-feedback/sbx-report", "finish", "--outcome", "success",
    "--summary", "Smoke complete", "--state-file", str(state),
], check=True)
PYTHON
"""
    scratch = tmp_path / "summaries"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    async def exercise() -> dict[str, commit_summary.AssessmentResult]:
        async with asyncio.timeout(BUILD_DEADLINE + spec.timeout):
            return await commit_summary.run(cfg, spec, backend, commits)

    print(f"Commit summary mount smoke: runtime={cfg.sandbox.runtime}, image={image}")
    try:
        results = asyncio.run(exercise())
        assert results == {
            commits[0].commit: commit_summary.Assessment("Mount boundary verified", "L", False, "")
        }
        assert sentinel.read_bytes() == original_contents
        assert backend._bookmark_commit("feature/x") == commits[0].commit
        assert not list(scratch.iterdir())
    finally:
        for record in read_sessions():
            if Path(record.project_path).is_relative_to(scratch) and record.container_name:
                if cli == "container":
                    _runtime(cli, "stop", record.container_name)
                    _runtime(cli, "rm", record.container_name)
                else:
                    _runtime(cli, "rm", "--force", record.container_name)
        _runtime(cli, "image", "rm", image)
