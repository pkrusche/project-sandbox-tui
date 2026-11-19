from __future__ import annotations

import copy
import os
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from project_sandbox_tui import feedback, feedback_transport, initialization
from project_sandbox_tui.config import Action, Config, ConfigError
from project_sandbox_tui.jobs import JobManager, JobRecord
from project_sandbox_tui.queue import TaskQueue
from project_sandbox_tui.sandbox import LaunchSpec, build_argv
from tests.conftest import wait_for


@pytest.fixture
def reporting_dir(tmp_path: Path) -> Path:
    root = tmp_path / "runs" / "abc123"
    feedback_transport.prepare(root, "abc123", "task", {"input": 42})
    return root


def spec(prompt: str = "report:success", branch: str = "topic") -> LaunchSpec:
    return LaunchSpec(branch, "claude", mode="background", prompt_text=prompt)


def finish(jobs: JobManager, branch: str = "topic") -> JobRecord:
    def done() -> bool:
        jobs.refresh({})
        return not jobs.jobs[branch].running

    wait_for(done)
    return jobs.jobs[branch]


def test_helper_progress_final_and_standalone_execution(
    reporting_dir: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state.json"
    state.write_text('{"next":"review"}')
    helper = reporting_dir / "sbx-report"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(helper),
            "--context-dir",
            str(reporting_dir),
            "update",
            "--summary",
            "working",
            "--state-file",
            str(state),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    first = feedback.read_json(reporting_dir / "output/feedback.json")
    assert first["phase"] == "running" and first["sequence"] == 1
    assert first["state"] == {"next": "review"}
    final = feedback.report(reporting_dir, "done", "success")
    assert final["sequence"] == 2 and final["state"] == first["state"]
    with pytest.raises(feedback.FeedbackError, match="sealed"):
        feedback.report(reporting_dir, "changed", "failure")
    assert feedback.read_json(reporting_dir / "output/feedback.json") == final


def test_parallel_helper_updates_do_not_lose_sequences(reporting_dir: Path) -> None:
    def update(index: int) -> int:
        return feedback.report(reporting_dir, f"step {index}")["sequence"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(update, range(12))) == list(range(1, 13))
    assert feedback.read_json(reporting_dir / "output/feedback.json")["sequence"] == 12


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"run_id": "stale"},
        {"sequence": 0},
        {"sequence": True},
        {"sequence": 1.2},
        {"phase": "unknown"},
        {"outcome": "success"},
        {"summary": ""},
        {"state": []},
        {"extra": 1},
        {"checks": [{"name": "tests", "outcome": "maybe"}]},
        {"state": {"value": float("nan")}},
        {"state": {"value": float("inf")}},
    ],
)
def test_reject_invalid_reports(reporting_dir: Path, change: dict) -> None:
    raw = feedback.report(reporting_dir, "working")
    raw.update(change)
    with pytest.raises(feedback.FeedbackError):
        feedback.validate(raw, "abc123")


@pytest.mark.parametrize(
    "content",
    [
        b'{"key":1,"key":2}',
        b'{"key":NaN}',
        b'{"key":1e999}',
        b"\xff",
        b'{"partial":',
        b"[" * 1000 + b"]" * 1000,
        b'"' + b"x" * feedback.MAX_BYTES + b'"',
    ],
)
def test_reject_bad_json(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "bad.json"
    path.write_bytes(content)
    with pytest.raises(feedback.FeedbackError):
        feedback.read_json(path)


def test_reject_symlinks_and_fifos(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("{}")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        feedback.read_json(link)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(feedback.FeedbackError, match="regular"):
        feedback.read_json(fifo)


def test_sequence_and_missing_fields(reporting_dir: Path) -> None:
    first = feedback.report(reporting_dir, "first")
    assert feedback.accept(first, "abc123", first) == first
    changed = dict(first, summary="different")
    with pytest.raises(feedback.FeedbackError, match="sequence"):
        feedback.accept(changed, "abc123", first)
    del changed["phase"]
    with pytest.raises(feedback.FeedbackError, match="fields"):
        feedback.validate(changed, "abc123")


@pytest.mark.parametrize("outcome", [None, "success"])
def test_state_type_changes_require_a_new_sequence(
    reporting_dir: Path, outcome: str | None
) -> None:
    previous = feedback.report(reporting_dir, "snapshot", outcome, {"nested": [1]})
    # JSON booleans and numbers differ even though Python considers True == 1.
    changed = dict(previous, state={"nested": [True]})
    with pytest.raises(feedback.FeedbackError, match="sealed" if outcome else "sequence"):
        feedback.accept(changed, "abc123", previous)
    reordered = dict(reversed(list(previous.items())))
    assert feedback.accept(reordered, "abc123", previous) == previous


@pytest.mark.parametrize(
    ("prompt", "status", "outcome", "success"),
    [
        ("report:success", "completed", "success", True),
        ("report:failure", "completed", "failure", False),
        ("report:blocked", "completed", "blocked", False),
        ("report:success exit:7", "failed", "success", False),
        ("report:success exit:124", "timeout", "success", False),
        ("report:success exit:130", "interrupted", "success", False),
        ("no report", "completed", "unknown", False),
    ],
)
def test_process_status_is_independent(
    repo: Path,
    fake_cfg: Callable[..., Config],
    prompt: str,
    status: str,
    outcome: str,
    success: bool,
) -> None:
    jobs = JobManager(repo, fake_cfg())
    original = jobs.start_background(spec(prompt), {})
    adopted = JobManager(repo, jobs.cfg)
    job = finish(adopted)
    assert job.run_id == original.run_id
    assert (job.status, job.reported_outcome, job.reported_success) == (status, outcome, success)
    assert job.feedback_frozen
    # A replacement file after completion cannot change the persisted result.
    Path(job.feedback_path).write_text('{"broken":')
    restarted = JobManager(repo, jobs.cfg)
    restarted.refresh({})
    assert restarted.jobs["topic"].reported_outcome == outcome
    assert restarted.jobs["topic"].feedback_status == job.feedback_status


def test_early_report_keeps_busy_and_invalid_final_blocks_success(
    repo: Path,
    fake_cfg: Callable[..., Config],
) -> None:
    jobs = JobManager(repo, fake_cfg())
    job = jobs.start_background(spec("sleep:0.5 report:success"), {})
    wait_for(lambda: Path(job.feedback_path).exists())
    jobs.refresh({})
    assert jobs.jobs["topic"].running and jobs.busy_reason("topic", {})
    assert jobs.jobs["topic"].reported_outcome == "success"
    Path(job.feedback_path).write_text('{"partial":')
    done = finish(jobs)
    assert done.feedback_snapshot and done.feedback_snapshot["outcome"] == "success"
    assert done.feedback_status == "invalid" and not done.reported_success


def test_unsupported_and_required_capability(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feedback_transport, "supported", lambda *_, **__: False)
    cfg = fake_cfg()
    jobs = JobManager(repo, cfg)
    job = jobs.start_background(spec(), {})
    assert job.feedback_status == "unsupported" and not job.feedback_path
    finish(jobs)
    cfg.feedback.require_reported_success = True
    with pytest.raises(ConfigError, match="--mount"):
        jobs.start_background(spec(), {})


def test_mounts_and_prompt_do_not_change_original_spec(reporting_dir: Path, tmp_path: Path) -> None:
    cfg = Config()
    cfg.project.base_image = "img"
    from project_sandbox_tui.sandbox import RunPaths

    paths = RunPaths(tmp_path / "log", tmp_path / "summary", tmp_path / "stdout")
    launch = spec("original")
    before = copy.deepcopy(launch)
    argv = build_argv(cfg, tmp_path, launch, paths, reporting_dir)
    assert launch == before
    assert any(a.startswith("--prompt-text=original\n\nRun reporting:") for a in argv)
    assert feedback_transport.mounts(reporting_dir)[0].endswith(",readonly")
    assert not feedback_transport.mounts(reporting_dir)[1].endswith(",readonly")
    bash = LaunchSpec("topic", "bash", mode="background", prompt_text="exit 0")
    assert "--prompt-text=exit 0" in build_argv(cfg, tmp_path, bash, paths, reporting_dir)
    with pytest.raises(ValueError, match="commas"):
        feedback_transport.mounts(tmp_path / "bad,path")


def test_required_success_pauses_once_and_resume_is_explicit_override(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    cfg = fake_cfg()
    cfg.feedback.require_reported_success = True
    jobs = JobManager(repo, cfg)
    queue = TaskQueue(jobs.state_dir)
    first = queue.add(spec("report:failure"))
    second = queue.add(spec())
    assert queue.dispatch(jobs, lambda: {}, lambda _: None)[0].queue_id == first.id
    finish(jobs)
    assert queue.dispatch(jobs, lambda: {}, lambda _: None) == []
    assert queue.get("topic").paused and "failure" in queue.get("topic").error
    queue.pause("topic", False)
    assert queue.dispatch(jobs, lambda: {}, lambda _: None)[0].queue_id == second.id
    finish(jobs)


def test_state_handoff_is_selected_and_missing_keys_pause(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    cfg = fake_cfg()
    cfg.feedback.pass_state_keys = ["chosen"]
    jobs = JobManager(repo, cfg)
    queue = TaskQueue(jobs.state_dir)
    queue.add(spec("sleep:0.3"))
    queue.add(spec("sleep:0.1"))
    first = queue.dispatch(jobs, lambda: {}, lambda _: None)[0]
    feedback.report(
        Path(first.feedback_path).parent.parent, "done", "success", {"chosen": 7, "private": 8}
    )
    assert queue.dispatch(jobs, lambda: {}, lambda _: None) == []
    assert not queue.get("topic").paused  # defer while predecessor remains alive
    finish(jobs)
    second = queue.dispatch(jobs, lambda: {}, lambda _: None)[0]
    context = feedback.read_json(Path(second.feedback_path).parent.parent / "context.json")
    assert context["previous_state"] == {"chosen": 7}
    finish(jobs)
    queue.add(spec())
    assert queue.dispatch(jobs, lambda: {}, lambda _: None) == []
    assert queue.get("topic").paused and "state handoff" in queue.get("topic").error


def test_retry_decision_survives_restart_and_is_bounded(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    cfg = fake_cfg()
    cfg.feedback.retry_outcomes = ["failure"]
    cfg.feedback.max_attempts = 2
    jobs = JobManager(repo, cfg)
    queue = TaskQueue(jobs.state_dir)
    task = queue.add(spec("report:failure"))
    queue.dispatch(jobs, lambda: {}, lambda _: None)
    first = finish(jobs)
    # Persist the decision, then restart before launching its successor.
    queue._decide(jobs)
    successor = queue.get("topic").tasks[0]
    assert successor.parent_task == task.id and successor.attempt == 2
    queue = TaskQueue(jobs.state_dir)
    second = queue.dispatch(jobs, lambda: {}, lambda _: None)[0]
    assert second.queue_id == successor.id and second.run_id != first.run_id
    finish(jobs)
    assert queue.dispatch(jobs, lambda: {}, lambda _: None) == []
    assert queue.get("topic").paused and "limit" in queue.get("topic").error
    queue.load()
    assert len(queue.get("topic").history) == 2


def test_branch_action_and_concurrent_dispatch(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    cfg = fake_cfg()
    cfg.actions["review"] = Action(prompt="report:success review {bookmark}")
    cfg.feedback.on_success = "review"
    cfg.feedback.max_steps = 2
    cfg.validate()
    jobs = JobManager(repo, cfg)
    queue = TaskQueue(jobs.state_dir)
    queue.add(spec())
    queue.dispatch(jobs, lambda: {}, lambda _: None)
    finish(jobs)

    def dispatch() -> list[JobRecord]:
        manager = JobManager(repo, cfg)
        return TaskQueue(manager.state_dir).dispatch(manager, lambda: {}, lambda _: None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: dispatch(), range(2)))
    assert sum(map(len, results)) == 1
    finish(jobs)
    queue.dispatch(jobs, lambda: {}, lambda _: None)
    assert queue.get("topic").paused and "step limit" in queue.get("topic").error
    assert queue.get("topic").history[-1].spec.prompt_text.strip() == "report:success review topic"


def test_init_preserves_user_guidance_and_skill_edits(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text("User guidance with no final newline")
    initialization.install_feedback(tmp_path)
    first = agents.read_text()
    assert first.startswith("User guidance with no final newline")
    initialization.install_feedback(tmp_path)
    assert agents.read_text() == first
    agents.write_text(first.replace("publish progress", "custom guidance"))
    skill = tmp_path / initialization.FEEDBACK_DIRS[0] / "SKILL.md"
    skill.write_text("custom skill")
    messages = initialization.install_feedback(tmp_path)
    assert "custom guidance" in agents.read_text()
    assert skill.read_text() == "custom skill"
    assert len([message for message in messages if "preserved" in message]) == 2


@pytest.mark.parametrize("state_name", ["state", "state,feedback", "state\nfeedback"])
@pytest.mark.parametrize("sandbox_dry_run", [False, True])
def test_host_helper_and_frozen_inline_result(
    tmp_path: Path, state_name: str, sandbox_dry_run: bool
) -> None:
    cfg = Config()
    if sandbox_dry_run:
        cfg.sandbox.extra_args.append("--dry-run")
    jobs = JobManager(tmp_path, cfg, state_dir=tmp_path / state_name)
    argv = [
        sys.executable,
        "-c",
        "import os, subprocess; subprocess.run(['python3', os.environ['SBX_FEEDBACK_DIR'] + "
        "'/sbx-report', 'finish', '--outcome', 'success', '--summary', 'host done'], check=True)",
    ]
    with jobs.interactive(LaunchSpec("@", "bash"), {}, argv=argv, host=True) as job:
        job.exit_code = subprocess.run(job.launch_argv(), cwd=tmp_path).returncode
    assert job.reported_success and job.feedback_frozen
    assert job.argv == argv
    assert (tmp_path / ".agents/skills/sbx-run-feedback/SKILL.md").is_file()


def test_old_job_records_have_unknown_outcome() -> None:
    job = JobRecord.from_dict({"branch": "topic", "spec": {"agent": "claude"}})
    assert job.reported_outcome == "unknown" and job.feedback_status == "unsupported"


def test_dry_run_does_not_create_reporting_files(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    cfg = fake_cfg()
    cfg.sandbox.extra_args.append("--dry-run")
    jobs = JobManager(repo, cfg)
    jobs.start_background(spec(), {})
    finish(jobs)
    assert not (jobs.state_dir / "runs").exists()


def test_policy_cannot_be_bypassed_by_completion_between_transactions(
    repo: Path,
    fake_cfg: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = fake_cfg()
    cfg.feedback.require_reported_success = True
    jobs = JobManager(repo, cfg)
    queue = TaskQueue(jobs.state_dir)
    queue.add(spec("sleep:0.3 report:failure"))
    queue.add(spec())
    queue.dispatch(jobs, lambda: {}, lambda _: None)
    decide = queue._decide

    def race(manager: JobManager) -> None:
        decide(manager)  # predecessor still running
        finish(JobManager(repo, cfg))  # another TUI finalizes it before dispatch

    monkeypatch.setattr(queue, "_decide", race)
    assert queue.dispatch(jobs, lambda: {}, lambda _: None) == []
    monkeypatch.setattr(queue, "_decide", decide)
    assert queue.dispatch(jobs, lambda: {}, lambda _: None) == []
    assert queue.get("topic").paused


def test_failed_successor_queue_save_recovers_same_run(
    repo: Path,
    fake_cfg: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = fake_cfg()
    cfg.feedback.retry_outcomes = ["failure"]
    cfg.feedback.max_attempts = 2
    jobs = JobManager(repo, cfg)
    queue = TaskQueue(jobs.state_dir)
    queue.add(spec("report:failure"))
    queue.dispatch(jobs, lambda: {}, lambda _: None)
    finish(jobs)
    queue._decide(jobs)
    successor_id = queue.get("topic").tasks[0].id
    persist = queue._persist

    def fail(_data: dict) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(queue, "_persist", fail)
    with pytest.raises(OSError, match="disk full"):
        queue.dispatch(jobs, lambda: {}, lambda _: None)
    run_id = jobs.jobs["topic"].run_id
    monkeypatch.setattr(queue, "_persist", persist)
    recovered = TaskQueue(jobs.state_dir)
    recovered.load()
    assert recovered.get("topic").history[-1].id == successor_id
    retained = recovered.get("topic").history[-1].job
    assert retained and retained.run_id == run_id
    assert recovered.dispatch(jobs, lambda: {}, lambda _: None) == []
    finish(jobs)


def test_feedback_doctor_is_read_only_and_reports_drift(tmp_path: Path) -> None:
    from project_sandbox_tui.doctor import check_feedback

    cfg = Config()
    checks = check_feedback(cfg, tmp_path, lambda *_: (0, "--mount MOUNT"))
    assert [c.status for c in checks] == ["ok", "warn"]
    assert not (tmp_path / "AGENTS.md").exists()
    initialization.install_feedback(tmp_path)
    assert all(
        c.status == "ok" for c in check_feedback(cfg, tmp_path, lambda *_: (0, "--mount MOUNT"))
    )
    (tmp_path / initialization.FEEDBACK_DIRS[0] / "SKILL.md").write_text("customized")
    assert check_feedback(cfg, tmp_path, lambda *_: (0, "--mount MOUNT"))[-1].status == "warn"
    cfg.feedback.require_reported_success = True
    assert check_feedback(cfg, tmp_path, lambda *_: (0, "old CLI"))[0].status == "fail"


def test_reporting_state_is_not_workspace_state(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    import shutil

    jobs = JobManager(repo, fake_cfg())
    job = jobs.start_background(spec(), {})
    finish(jobs)
    shutil.rmtree(repo)
    assert Path(job.feedback_path).is_file()
    assert JobManager(repo, jobs.cfg).jobs["topic"].reported_success


def test_feedback_config_validation() -> None:
    from project_sandbox_tui.config import from_dict

    for data in (
        {"max_steps": 0},
        {"max_attempts": 101},
        {"max_attempts": True},
        {"retry_outcomes": ["success"]},
        {"on_success": "missing"},
        {"enabled": False, "require_reported_success": True},
    ):
        with pytest.raises(ConfigError):
            from_dict({"feedback": data}).validate()


def test_tui_dispatches_successor_when_no_manual_tasks_remain(
    repo: Path,
    fake_cfg: Callable[..., Config],
) -> None:
    from project_sandbox_tui.app import SandboxTUI
    from tests.test_app import run_app

    cfg = fake_cfg()
    cfg.feedback.retry_outcomes = ["failure"]
    cfg.feedback.max_attempts = 2

    async def scenario(app: SandboxTUI, _pilot: object) -> None:
        app._refresh_timer.pause()
        await app.refresh_data()
        app.queue.add(spec("report:failure", "feature/x"))
        first = app._dispatch_queue()[0]
        finish(app.jobs, "feature/x")
        assert not app.queue.get("feature/x").tasks
        await app.refresh_data()
        history = app.queue.get("feature/x").history
        assert len(history) == 2
        assert history[-1].job and history[-1].job.run_id != first.run_id
        finish(app.jobs, "feature/x")
        await app.refresh_data()
        assert app.queue.get("feature/x").paused

    run_app(repo, cfg, scenario)


@pytest.mark.parametrize("option", ["--state-file", "--checks-file"])
def test_helper_rejects_explicit_null(reporting_dir: Path, tmp_path: Path, option: str) -> None:
    data = tmp_path / "null.json"
    data.write_text("null")
    args = ["--context-dir", str(reporting_dir), "finish", "--outcome", "success"]
    assert feedback.main([*args, "--summary", "done", option, str(data)]) == 1
    assert not (reporting_dir / "output/feedback.json").exists()


def test_preview_preserves_real_predecessor_for_policy_and_state(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    cfg = fake_cfg()
    cfg.feedback.require_reported_success = True
    cfg.feedback.pass_state_keys = ["chosen"]
    jobs = JobManager(repo, cfg)
    queue = TaskQueue(jobs.state_dir)
    queue.add(spec("sleep:0.1"))
    first = queue.dispatch(jobs, lambda: {}, lambda _: None)[0]
    feedback.report(Path(first.feedback_path).parent.parent, "done", "success", {"chosen": 7})
    finish(jobs)
    preview = spec("preview")
    preview.extra_args.append("--dry-run")
    queue.start(preview, jobs, {})
    finish(jobs)
    queue = TaskQueue(jobs.state_dir)
    queued = queue.add(spec())
    [second] = queue.dispatch(jobs, lambda: {}, lambda _: None)
    assert second.queue_id == queued.id
    context = feedback.read_json(Path(second.feedback_path).parent.parent / "context.json")
    assert context["previous_state"] == {"chosen": 7}
    finish(jobs)
