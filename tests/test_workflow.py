from __future__ import annotations

import contextlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from project_sandbox_tui import feedback
from project_sandbox_tui.app import SandboxTUI
from project_sandbox_tui.config import Action, Config, ConfigError, Workflow, from_dict
from project_sandbox_tui.jobs import BusyError, JobManager, JobRecord
from project_sandbox_tui.queue import TaskQueue, WorkflowRun
from project_sandbox_tui.sandbox import LaunchSpec
from project_sandbox_tui.screens.dialogs import ConfirmScreen, MessageScreen
from project_sandbox_tui.screens.launch import LaunchScreen
from project_sandbox_tui.screens.queue import QueueScreen
from tests.test_app import run_app, wait_for_screen, wait_until
from tests.test_feedback import finish, spec


@pytest.mark.parametrize("mode", ["inline", "external", "queued"])
def test_workflow_command_runs_plan_then_four_steps(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    cfg = fake_cfg()
    cfg.actions = {
        "plan": Action('report:success report-state:{"spec_path":"plan.md","private":1}'),
        "check": Action("report:success"),
        "implement": Action("report:success"),
        "chores": Action("report:success"),
        "commits": Action('report:success report-state:{"commits":[{"subject":"Add CSV"}]}'),
    }
    stages = ["check", "implement", "chores", "commits"]
    cfg.workflows = {"spec": Workflow("plan", stages, ["spec_path"], ["commits"])}
    if mode == "external":
        cfg.ui.terminal_command = "sh -c {cmd}"
    else:
        monkeypatch.setattr(SandboxTUI, "_suspended", lambda _app: contextlib.nullcontext())
    if mode == "queued":
        cfg.workflows["spec"].first_step_interactive = False

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j")
        assert app.selected_branch_name() == "feature/x"
        if mode == "queued":
            app.queue.pause("feature/x", True)
            ordinary = app.queue.add(
                LaunchSpec("feature/x", "claude", mode="background", prompt_text="first")
            )
        await app.run_command(":workflow spec CSV export")
        if mode == "queued":
            pending = app.queue.get("feature/x").tasks
            assert [task.workflow is not None for task in pending] == [False, True]
            assert pending[0].id == ordinary.id and not app.jobs.jobs
            app.queue.delete("feature/x", [ordinary.id])
            app.queue.pause("feature/x", False)

        def complete() -> bool:
            history = app.queue.get("feature/x").history
            return bool(history and history[-1].decision == "workflow complete")

        await wait_until(pilot, complete)
        history = app.queue.get("feature/x").history
        assert [t.workflow.action for t in history if t.workflow] == ["plan", *stages]
        for index, task in enumerate(history):
            assert task.job and task.job.reported_success
            context = feedback.read_json(Path(task.job.feedback_path).parents[1] / "context.json")
            assert context["task_id"] == task.id
            assert context["interactive"] is (index == 0 and mode != "queued")
            if index == 0:
                if mode == "queued":
                    assert any(
                        arg.startswith("--prompt-text=") and "CSV export" in arg
                        for arg in task.job.argv
                    )
                else:
                    assert "CSV export" in context["planning_prompt"]
                    assert not any(arg.startswith("--prompt-text=") for arg in task.job.argv)
            else:
                assert context["previous_state"] == {"spec_path": "plan.md"}
                assert task.parent_task == history[index - 1].id
        await pilot.press("L")
        await wait_for_screen(app, pilot, QueueScreen)
        await pilot.press("P")
        details = await wait_for_screen(app, pilot, MessageScreen)
        assert "spec / commits" in details.body and '"subject": "Add CSV"' in details.body

    run_app(repo, cfg, scenario)


def test_failed_external_planning_launch_preserves_the_queue(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    cfg = fake_cfg()
    cfg.ui.terminal_command = "definitely-missing-terminal {cmd}"
    cfg.actions = {"plan": Action("plan"), "check": Action("check")}
    cfg.workflows = {"spec": Workflow("plan", ["check"])}

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j")
        branch = "feature/x"
        app.queue.pause(branch, True)
        pending = app.queue.add(LaunchSpec(branch, "codex", mode="background", prompt_text="work"))
        await app.run_command(":workflow spec")
        await wait_until(
            pilot,
            lambda: any(
                "definitely-missing-terminal" in str(n.message) for n in app._notifications
            ),
        )
        app.queue.load()
        queue = app.queue.get(branch)
        assert queue.paused and not queue.history
        assert [task.id for task in queue.tasks] == [pending.id]
        assert not app.jobs.busy_reason(branch, {})

    run_app(repo, cfg, scenario)


def test_blocked_queued_planner_guides_interactive_repair_and_editor(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = fake_cfg()
    cfg.actions = {
        "plan": Action('report:blocked report-state:{"spec_path":"plan.md"}'),
        "check": Action("report:success"),
    }
    cfg.workflows = {
        "spec": Workflow("plan", ["check"], ["spec_path"], first_step_interactive=False)
    }
    opened: list[str] = []
    monkeypatch.setattr(SandboxTUI, "_suspended", lambda _app: contextlib.nullcontext())
    monkeypatch.setattr(SandboxTUI, "_open_editor", lambda _app, branch: opened.append(branch))

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        branch = "feature/x"
        await pilot.press("j")
        await app.run_command(":workflow spec")
        await wait_until(pilot, lambda: app.queue.get(branch).paused)
        blocked = app.queue.get(branch).history[-1]
        assert blocked.job and blocked.job.blocked_summary == "Fake task outcome"

        await pilot.press("L")
        await wait_for_screen(app, pilot, QueueScreen)
        await pilot.press("o")
        editor = await wait_for_screen(app, pilot, ConfirmScreen)
        assert "plan.md" in editor.body and "Fake task outcome" in editor.body
        await pilot.press("y")
        await wait_until(pilot, lambda: opened == [branch])

        await pilot.press("i")
        form = await wait_for_screen(app, pilot, LaunchScreen)
        assert "spec / plan" in form.blocked_guidance and "plan.md" in form.blocked_guidance
        assert form.fixed_branch
        await pilot.press("ctrl+s")
        await wait_until(pilot, lambda: app.jobs.jobs[branch].mode == "interactive")
        repair = app.jobs.jobs[branch]
        context = feedback.read_json(Path(repair.feedback_path).parents[1] / "context.json")
        assert "Fake task outcome" in context["blocked_guidance"]
        assert not repair.queue_id and app.queue.get(branch).paused

        preview = LaunchSpec(
            branch, "codex", mode="background", prompt_text="preview", extra_args=["--dry-run"]
        )
        app.queue.start(preview, app.jobs, {})
        finish(app.jobs, branch)
        await pilot.press("q", "i")
        again = await wait_for_screen(app, pilot, LaunchScreen)
        assert again.blocked_guidance == form.blocked_guidance
        await pilot.press("escape", "L", "p")
        await wait_until(pilot, lambda: len(app.queue.get(branch).history) == 3)
        retry = app.queue.get(branch).history[-1]
        assert retry.parent_task == blocked.id and retry.attempt == 2 and retry.step == 1

    run_app(repo, cfg, scenario)


def test_workflow_persists_progress_and_retries_only_the_blocked_stage(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    cfg = fake_cfg()
    cfg.feedback.pass_state_keys = ["ordinary_only"]
    jobs = JobManager(repo, cfg)
    queue = TaskQueue(jobs.state_dir)
    branch = "feature/x"
    plan = LaunchSpec(branch, "codex", prompt_text="Write a plan")
    implement = LaunchSpec(branch, "codex", mode="background", prompt_text="report:blocked")
    commits = LaunchSpec(branch, "codex", mode="background", prompt_text="report:success")
    workflow = WorkflowRun(
        "spec", "plan", [("implement", implement), ("commits", commits)], ["spec_path"], ["commits"]
    )

    def dispatch(manager: JobManager, store: TaskQueue) -> list[JobRecord]:
        return store.dispatch(manager, lambda: {}, lambda _: None)

    ordinary = queue.add(commits)
    queue.pause(branch, True)
    with contextlib.ExitStack() as session:
        with queue.start_workflow(plan, workflow, jobs) as task:
            planner = session.enter_context(jobs.interactive(plan, {}, queue_id=task.id))
            task.job = planner
        planner_root = Path(planner.feedback_path).parents[1]
        feedback.report(planner_root, "ready", "success", {"spec_path": "plan.md"})
        queue.pause(branch, False)
        assert not dispatch(jobs, queue)  # the final report comes before process exit
        planner.exit_code = 0

    def adopt_and_dispatch() -> list[JobRecord]:
        manager = JobManager(repo, cfg)
        return dispatch(manager, TaskQueue(manager.state_dir))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: adopt_and_dispatch(), range(2)))
    launched = [job for group in results for job in group]
    assert len(launched) == 1 and launched[0].spec == implement
    finish(jobs, branch)
    assert not dispatch(jobs, queue)
    stopped = queue.get(branch)
    assert stopped.paused and "reported blocked" in stopped.error
    assert [task.id for task in stopped.tasks] == [ordinary.id]

    preview = LaunchSpec(
        branch, "codex", mode="background", prompt_text="preview", extra_args=["--dry-run"]
    )
    queue.start(preview, jobs, {})
    finish(jobs, branch)
    with pytest.raises(BusyError), queue.start_workflow(plan, workflow, jobs):
        pytest.fail("a preview must not hide the stopped workflow")
    queue.pause(branch, False)
    retry = queue.get(branch).tasks[0]
    assert retry.spec == implement and retry.attempt == 2 and retry.step == 2
    queue.edit(
        branch,
        retry.id,
        LaunchSpec(branch, "codex", mode="background", prompt_text="report:success"),
    )
    retried = dispatch(jobs, queue)[0]
    assert retried.queue_id == retry.id
    context = feedback.read_json(Path(retried.feedback_path).parents[1] / "context.json")
    assert context["previous_state"] == {"spec_path": "plan.md"}
    finish(jobs, branch)
    assert dispatch(jobs, queue)[0].spec == commits
    finish(jobs, branch)
    assert not dispatch(jobs, queue)
    assert queue.get(branch).error.endswith("missing report state: commits")
    assert [task.id for task in queue.get(branch).tasks] == [ordinary.id]


def test_fifo_state_survives_a_workflow_and_an_intervening_preview(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    cfg = fake_cfg()
    cfg.feedback.pass_state_keys = ["source"]
    jobs = JobManager(repo, cfg)
    queue = TaskQueue(jobs.state_dir)

    def dispatch() -> JobRecord:
        [job] = queue.dispatch(jobs, lambda: {}, lambda _: None)
        return job

    queue.add(spec('report:success report-state:{"source":"fifo"}'))
    dispatch()
    finish(jobs)
    check = spec("report:success")
    workflow = WorkflowRun("spec", "plan", [("check", check)], ["source"], [])
    queue.add(spec('report:success report-state:{"source":"plan"}'), workflow=workflow)
    ordinary = queue.add(spec("next ordinary task"))
    dispatch()
    finish(jobs)
    preview = spec("preview")
    preview.extra_args.append("--dry-run")
    queue.start(preview, jobs, {})
    finish(jobs)

    stage = dispatch()
    assert stage.spec == check
    context = feedback.read_json(Path(stage.feedback_path).parents[1] / "context.json")
    assert context["previous_state"] == {"source": "plan"}
    finish(jobs)
    following = dispatch()
    assert following.queue_id == ordinary.id
    context = feedback.read_json(Path(following.feedback_path).parents[1] / "context.json")
    assert context["previous_state"] == {"source": "fifo"}
    finish(jobs)


def test_workflow_config_requires_existing_actions_and_feedback() -> None:
    cfg = from_dict(
        {
            "actions": {"plan": {"prompt": "Write a spec"}, "commits": {"prompt": "Summarize"}},
            "workflows": {
                "spec": {
                    "planning_action": "plan",
                    "steps": ["commits"],
                    "pass_state_keys": ["spec_path"],
                    "result_state_keys": ["commits"],
                    "first_step_interactive": False,
                }
            },
        }
    )
    cfg.validate()
    assert cfg.workflows["spec"].result_state_keys == ["commits"]
    assert Workflow().first_step_interactive
    assert not cfg.workflows["spec"].first_step_interactive
    cfg.feedback.enabled = False
    with pytest.raises(ConfigError, match="workflows require feedback.enabled"):
        cfg.validate()
    cfg.feedback.enabled = True
    del cfg.actions["commits"]
    with pytest.raises(ConfigError, match="unknown action 'commits'"):
        cfg.validate()
