from __future__ import annotations

import contextlib
import shlex
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from project_sandbox_tui import feedback
from project_sandbox_tui import queue as queue_module
from project_sandbox_tui.app import SandboxTUI
from project_sandbox_tui.config import Action, Config, ConfigError, Workflow, from_dict
from project_sandbox_tui.jobs import BusyError, JobManager, JobRecord
from project_sandbox_tui.queue import TaskQueue, WorkflowRun
from project_sandbox_tui.review import SeenFiles
from project_sandbox_tui.sandbox import LaunchSpec
from project_sandbox_tui.screens.dialogs import ConfirmScreen, MessageScreen
from project_sandbox_tui.screens.launch import LaunchScreen
from project_sandbox_tui.screens.queue import QueueScreen
from project_sandbox_tui.screens.review import CommitReviewScreen
from project_sandbox_tui.screens.workflow import WorkflowProfilesScreen
from project_sandbox_tui.vcs import JjBackend, workspace_path
from tests.conftest import jj, wait_for
from tests.test_app import run_app, wait_for_screen, wait_until
from tests.test_feedback import finish, spec


async def start_workflow(app: SandboxTUI, pilot: Any, line: str) -> WorkflowProfilesScreen:
    """Run ``:workflow ...`` and accept the stage profile picker as shown."""
    await app.run_command(line)
    picker = await wait_for_screen(app, pilot, WorkflowProfilesScreen)
    await pilot.press("enter")
    return picker


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
        starting_commit = app.backend._description_commit("feature/x")
        await start_workflow(app, pilot, ":workflow spec CSV export")
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
                assert task.base_commit == task.job.base_commit == starting_commit
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
        screen = await wait_for_screen(app, pilot, QueueScreen)
        assert [str(screen.table.get_cell(t.id, "flow")) for t in history] == ["→"] * 4 + ["■"]
        assert all(str(screen.table.get_cell(t.id, "outcome")) == "success" for t in history)
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
        await start_workflow(app, pilot, ":workflow spec")
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
        await start_workflow(app, pilot, ":workflow spec")
        await wait_until(pilot, lambda: app.queue.get(branch).paused)
        blocked = app.queue.get(branch).history[-1]
        assert blocked.job and blocked.job.blocked_summary == "Fake task outcome"

        await pilot.press("L")
        screen = await wait_for_screen(app, pilot, QueueScreen)
        assert str(screen.table.get_cell(blocked.id, "status")) == "completed"
        assert str(screen.table.get_cell(blocked.id, "outcome")) == "blocked"
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
        # A planner can exit between refreshing queue history and reserving jobs.lock.
        # The queue must wait for its decision before yielding a new reservation.
        with pytest.raises(BusyError), queue.start_workflow(plan, workflow, jobs):
            pytest.fail("a second planner replaced a workflow awaiting its decision")
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


def test_workflow_config_inserts_a_review_before_each_host_command() -> None:
    cfg = from_dict(
        {
            "actions": {"plan": {"prompt": "Plan"}, "implement": {"prompt": "Implement"}},
            "commands": {"test": "scripts/test.sh", "e2e": "scripts/e2e-test.sh"},
            "workflows": {
                "spec": {
                    "planning_action": "plan",
                    "steps": ["human-review", "host:test", "host:e2e", "implement", "host:test"],
                },
            },
        }
    )
    cfg.validate()
    cfg.validate()
    assert cfg.workflows["spec"].steps == [
        "human-review",
        "host:test",
        "human-review",
        "host:e2e",
        "implement",
        "human-review",
        "host:test",
    ]
    del cfg.commands["e2e"]
    with pytest.raises(ConfigError, match="unknown host command 'e2e'"):
        cfg.validate()


@pytest.mark.parametrize("terminate", ["", "review", "host"])
def test_workflow_review_gate_from_the_task_list(
    repo: Path, fake_cfg: Callable[..., Config], tmp_path: Path, terminate: str
) -> None:
    branch = "feature/x"
    jj(repo, "new", branch)
    (repo / "c.txt").write_text("c\n")
    jj(repo, "bookmark", "move", branch, "--to", "@")
    jj(repo, "new", "main")
    workspace = workspace_path(repo, branch, None)
    JjBackend(repo).add_workspace(branch, workspace)
    (workspace / "agent-only.txt").write_text("workspace-only content\n")
    cfg = fake_cfg(deploy_config_skill=False)
    cfg.actions = {
        "plan": Action('report:success report-state:{"plan":"ready"}'),
        "finish": Action("report:success"),
    }
    script = (
        'set -e; test "$PWD" = "$SBX_WORKSPACE"; test "$SBX_BOOKMARK" = feature/x; '
        "test ! -e agent-only.txt; jj root; cat b.txt c.txt"
    )
    release = tmp_path / "release-host-command"
    if terminate == "host":
        script += f"; while test ! -e {shlex.quote(str(release))}; do sleep 0.05; done"
    cfg.commands = {"test": script}
    cfg.workflows = {
        "spec": Workflow("plan", ["host:test", "finish"], ["plan"], first_step_interactive=False),
    }
    cfg.validate()

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j")
        await start_workflow(app, pilot, ":workflow spec")
        await wait_until(pilot, lambda: bool(app.queue.get(branch).tasks))
        await wait_until(pilot, lambda: app.queue.get(branch).tasks[0].human_review)
        review_task = app.queue.get(branch).tasks[0]
        await pilot.press("L")
        screen = await wait_for_screen(app, pilot, QueueScreen)
        assert str(screen.table.get_cell(review_task.id, "status")) == "awaiting review"
        await pilot.press("enter")
        await wait_for_screen(app, pilot, CommitReviewScreen)
        await pilot.press("space", "q")
        await app.refresh_data()
        assert not app.tasks.for_bookmark(branch)
        assert app.queue.get(branch).tasks[0].id == review_task.id

        if terminate == "review":
            await pilot.press("X")
            assert app.queue.get(branch).history[-1].status == "terminated"
            assert not app.queue.get(branch).workflow_in_progress()
            await pilot.press("u", "p")
            backend = app.backend
            SeenFiles.for_repo(repo).mark(
                backend.review_file_keys(
                    backend._bookmark_commit("main"), backend._bookmark_commit(branch)
                ),
                True,
            )
            await app.refresh_data()
            assert not app.queue.get(branch).tasks and not app.tasks.for_bookmark(branch)
            return

        await pilot.press("enter")
        await wait_for_screen(app, pilot, CommitReviewScreen)
        await pilot.press("right_square_bracket", "space", "q")
        if terminate == "host":
            await wait_until(pilot, lambda: app.queue.get(branch).history[-1].status == "running")
            host = app.queue.get(branch).history[-1]
            assert host.host_task
            await wait_until(pilot, lambda: host.id in screen.visible_ids())
            screen.table.move_cursor(row=screen.visible_ids().index(host.id))
            await pilot.press("X")
            assert app.queue.get(branch).history[-1].terminated
            assert str(screen.table.get_cell(host.id, "flow")) == "×"
            assert app.queue.get(branch).history[-1].status == "running"
            release.touch()
            await wait_until(pilot, lambda: app.queue.get(branch).history[-1].status == "completed")
            assert app.queue.get(branch).history[-1].id == host.id
            assert not app.queue.get(branch).tasks
            return

        await wait_until(
            pilot, lambda: app.queue.get(branch).history[-1].decision == "workflow complete"
        )
        plan, review, host, last = app.queue.get(branch).history
        assert review.status == "reviewed" and review.parent_task == plan.id
        assert host.host_task and host.host_task.exit_code == 0 and host.parent_task == review.id
        assert Path(host.host_task.out_path).read_text().endswith("b\nc\n")
        assert not Path(host.host_task.cwd).exists()
        assert last.job and last.parent_task == host.id
        context = feedback.read_json(Path(last.job.feedback_path).parents[1] / "context.json")
        assert context["previous_state"] == {"plan": "ready"}

    run_app(repo, cfg, scenario)


def test_host_command_adoption_and_retry_recheck_the_bookmark(
    repo: Path, fake_cfg: Callable[..., Config], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = "feature/x"
    cfg = fake_cfg()
    jobs = JobManager(repo, cfg)
    queue = TaskQueue(jobs.state_dir)
    backend = JjBackend(repo)
    counter = tmp_path / "host-runs"
    path = shlex.quote(str(counter))
    script = f"if test -e {path}; then echo again >> {path}; else echo first > {path}; exit 7; fi"
    base = backend._bookmark_commit("main")
    workflow = WorkflowRun(
        "spec", "plan", [("human-review", None), ("host:test", script)], [], [], review_base=base
    )
    queue.add(spec(branch=branch), workflow=workflow)

    def dispatch() -> list[JobRecord]:
        return queue.dispatch(jobs, lambda: {}, lambda _: None)

    def mark_reviewed() -> None:
        tip = backend._bookmark_commit(branch)
        SeenFiles.for_repo(repo).mark(backend.review_file_keys(base, tip), True)

    assert len(dispatch()) == 1
    finish(jobs, branch)
    assert not dispatch()
    assert queue.get(branch).tasks[0].human_review
    jj(repo, "bookmark", "move", "main", "--to", branch)
    assert not dispatch()
    assert queue.get(branch).tasks[0].human_review and not counter.exists()
    mark_reviewed()
    with jobs.interactive(LaunchSpec(branch, "claude"), {}) as agent:
        assert not dispatch()
        assert not counter.exists()
        agent.exit_code = 0

    real_write = queue_module.write_state

    def fail_queue_write(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == queue.journal:
            raise OSError("lost queue transaction")
        real_write(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(queue_module, "write_state", fail_queue_write)
        with pytest.raises(OSError, match="lost queue transaction"):
            dispatch()

    queue = TaskQueue(jobs.state_dir)
    assert not dispatch()

    def stopped() -> bool:
        dispatch()
        return queue.get(branch).paused

    wait_for(stopped)
    failed = queue.get(branch).history[-1]
    assert failed.host_task and failed.host_task.exit_code == 7
    assert counter.read_text() == "first\n"

    # A new commit after the failed command must be reviewed before retry.
    jj(repo, "new", branch)
    (repo / "c.txt").write_text("new since the review\n")
    jj(repo, "bookmark", "move", branch, "--to", "@")
    jj(repo, "new", "main")
    queue.pause(branch, False)
    assert not dispatch()
    assert queue.get(branch).tasks[0].human_review
    assert counter.read_text() == "first\n"
    mark_reviewed()

    def complete() -> bool:
        dispatch()
        return queue.get(branch).history[-1].decision == "workflow complete"

    wait_for(complete)
    assert queue.get(branch).history[-1].attempt == 2
    assert counter.read_text() == "first\nagain\n"


@pytest.mark.parametrize("interactive", [False, True])
def test_invalid_rendered_step_leaves_the_queue_usable(
    repo: Path, fake_cfg: Callable[..., Config], interactive: bool
) -> None:
    jobs = JobManager(repo, fake_cfg())
    queue = TaskQueue(jobs.state_dir)
    branch = "feature/x"
    planner = LaunchSpec(
        branch, "codex", mode="interactive" if interactive else "background", prompt_text="plan"
    )
    empty = LaunchSpec(
        branch, "codex", mode="background", prompt_text=Action("{main}").render(branch, "")
    )
    workflow = WorkflowRun("spec", "plan", [("check", empty)], [], [])
    with pytest.raises(ValueError, match="invalid workflow step"):
        if interactive:
            with queue.start_workflow(planner, workflow, jobs):
                pytest.fail("invalid workflow reached launch")
        else:
            queue.add(planner, workflow=workflow)
    pending = queue.add(LaunchSpec(branch, "codex", mode="background", prompt_text="work"))
    assert queue.get(branch).tasks == [pending]


@pytest.mark.parametrize("second", ["queued", "interactive"])
def test_one_workflow_at_a_time_in_either_start_mode(
    repo: Path, fake_cfg: Callable[..., Config], second: str
) -> None:
    jobs = JobManager(repo, fake_cfg())
    queue = TaskQueue(jobs.state_dir)
    branch = "topic"

    def workflow() -> WorkflowRun:
        return WorkflowRun("spec", "plan", [("check", spec("report:success"))], [], [])

    def start_second() -> None:
        if second == "queued":
            queue.add(spec("report:success"), workflow=workflow())
            return
        planner = LaunchSpec(branch, "codex", mode="interactive", prompt_text="plan")
        with queue.start_workflow(planner, workflow(), jobs):
            pytest.fail("a second workflow reached launch")

    def dispatch() -> None:
        queue.dispatch(jobs, lambda: {}, lambda _: None)
        finish(jobs)
        queue.dispatch(jobs, lambda: {}, lambda _: None)  # record the decision

    queue.add(spec("report:blocked"), workflow=workflow())
    with pytest.raises(BusyError, match="workflow is already in progress"):
        start_second()  # pending planner

    dispatch()
    stopped = queue.get(branch)
    assert stopped.paused and not stopped.tasks
    with pytest.raises(BusyError, match="workflow is already in progress"):
        start_second()  # stopped step awaiting retry or deletion

    queue.delete(branch, [stopped.history[-1].id])
    queue.pause(branch, False)
    if second == "queued":
        start_second()
        assert [t.workflow is not None for t in queue.get(branch).tasks] == [True]


def profile_workflow_cfg(fake_cfg: Callable[..., Config]) -> Config:
    from project_sandbox_tui.config import AgentProfile

    cfg = fake_cfg()
    cfg.profiles = {
        "fast": AgentProfile(agent="bash", extra_args=["--fast-flag"]),
        "deep": AgentProfile(agent="bash", effort="high", extra_args=["--memory=16g"]),
    }
    cfg.actions = {
        "plan": Action("report:success"),
        "check": Action("report:success", agent="bash", effort="low"),
        "implement": Action("report:success", profile="deep"),
        "broken": Action("report:success", profile="gone"),
    }
    cfg.workflows = {
        "spec": Workflow("plan", ["check", "implement", "check"], first_step_interactive=False),
        "odd": Workflow("plan", ["broken"], first_step_interactive=False),
    }
    return cfg


def test_workflow_picker_choices_initial_values_and_cancel(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    from project_sandbox_tui.config import LastUsed
    from project_sandbox_tui.widgets import CycleField

    cfg = profile_workflow_cfg(fake_cfg)
    LastUsed(profile="fast").save(repo)

    def fields(picker: WorkflowProfilesScreen) -> list[tuple[list[str], str]]:
        return [
            (field.options, field.value)
            for field in picker.query(CycleField)
            if (field.id or "").removeprefix("stage-").isdigit()  # the setup fields
        ]

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        branch = "feature/x"
        await pilot.press("j")
        await app.run_command(":workflow spec")
        picker = await wait_for_screen(app, pilot, WorkflowProfilesScreen)
        own = ["fast", "deep", "action settings", "custom"]
        assert fields(picker) == [
            (["fast", "deep", "custom"], "fast"),  # remembered launch profile
            (own, "action settings"),
            (["fast", "deep", "custom"], "deep"),  # the action's profile
            (own, "action settings"),  # a repeated action gets its own row
        ]
        hint = picker.query_one("#stage-hint-2").render()
        assert str(hint) == "bash/high · --memory=16g"
        assert "1. plan · queued" in str(picker.query_one(".stage").render())
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.main
        assert not app.queue.get(branch).tasks and not app.jobs.jobs
        assert LastUsed.load(repo).profile == "fast"

        await app.run_command(":workflow odd")  # not refused after a cancel
        picker = await wait_for_screen(app, pilot, WorkflowProfilesScreen)
        assert fields(picker)[1] == (["fast", "deep", "custom"], "fast")
        assert any(
            "actions.broken refers to unknown profile 'gone'; using fast" in str(n.message)
            for n in app._notifications
        )
        await pilot.press("escape")

        # A workflow in progress is refused before the picker opens.
        app.queue.pause(branch, True)
        await start_workflow(app, pilot, ":workflow spec")
        await wait_until(pilot, lambda: bool(app.queue.get(branch).tasks))
        await app.run_command(":workflow spec")
        await pilot.pause()
        assert not isinstance(app.screen, WorkflowProfilesScreen)
        assert any(
            "a workflow is already in progress" in str(n.message) for n in app._notifications
        )

    run_app(repo, cfg, scenario)


def test_workflow_picker_overrides_are_snapshotted_and_not_remembered(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    from project_sandbox_tui.config import AgentProfile, LastUsed
    from project_sandbox_tui.widgets import CycleField

    cfg = profile_workflow_cfg(fake_cfg)
    LastUsed(profile="fast").save(repo)

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        branch = "feature/x"
        await pilot.press("j")
        app.queue.pause(branch, True)
        await app.run_command(":workflow spec")
        picker = await wait_for_screen(app, pilot, WorkflowProfilesScreen)
        picker.query_one("#stage-0", CycleField).focus()
        await pilot.press("l")  # plan: fast -> deep
        picker.query_one("#stage-2", CycleField).focus()
        await pilot.press("h")  # implement: deep -> fast
        await pilot.pause()
        assert str(picker.query_one("#stage-hint-2").render()) == "bash · --fast-flag"
        await pilot.press("enter")
        await wait_until(pilot, lambda: bool(app.queue.get(branch).tasks))

        app.queue.load()
        planner = app.queue.get(branch).tasks[0]
        assert planner.spec is not None
        assert planner.spec.profile == "deep" and planner.spec.extra_args == ["--memory=16g"]
        assert planner.workflow is not None
        steps = {
            name: step for name, step in planner.workflow.next_steps if isinstance(step, LaunchSpec)
        }
        assert steps["implement"].profile == "fast"
        assert steps["check"].profile == "" and steps["check"].effort == "low"

        # Later profile edits don't reach a started workflow.
        app.cfg.profiles["fast"] = AgentProfile(agent="bash", extra_args=["--changed"])
        app.queue.pause(branch, False)
        app._dispatch_queue()

        def complete() -> bool:
            history = app.queue.get(branch).history
            return bool(history and history[-1].decision == "workflow complete")

        await wait_until(pilot, complete, timeout=30)
        history = app.queue.get(branch).history
        implement = next(t for t in history if t.workflow and t.workflow.action == "implement")
        assert implement.job is not None
        assert "--fast-flag" in implement.job.argv and "--changed" not in implement.job.argv
        planned = history[0].job
        assert planned is not None and "--memory=16g" in planned.argv

        # Picks are not remembered for the launch forms or the next workflow.
        assert LastUsed.load(repo).profile == "fast"
        await pilot.press("r")
        form = await wait_for_screen(app, pilot, LaunchScreen)
        assert form.query_one("#profile", CycleField).value == "fast"
        await pilot.press("escape")
        await app.run_command(":workflow spec")
        picker = await wait_for_screen(app, pilot, WorkflowProfilesScreen)
        assert picker.query_one("#stage-0", CycleField).value == "fast"
        assert picker.query_one("#stage-2", CycleField).value == "deep"

    run_app(repo, cfg, scenario)


def test_workflow_dry_run_preview_shows_stage_profiles(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    from project_sandbox_tui.config import LastUsed

    cfg = profile_workflow_cfg(fake_cfg)
    LastUsed(profile="").save(repo)  # Custom

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app.dry_run = True
        await pilot.press("j")
        await start_workflow(app, pilot, ":workflow spec")
        preview = await wait_for_screen(app, pilot, MessageScreen)
        body = preview.body
        assert "plan (background) · profile custom · claude" in body
        assert "check (background) · profile action settings · bash/low" in body
        assert "implement (background) · profile deep · bash/high" in body
        assert not app.queue.get("feature/x").tasks and not app.jobs.jobs

    run_app(repo, cfg, scenario)


def test_workflow_file_and_inline_previews(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    from project_sandbox_tui import config
    from project_sandbox_tui.cli import main

    root = Path(__file__).resolve().parents[1]
    (repo / config.PROJECT_CONFIG_NAME).write_text((root / config.PROJECT_CONFIG_NAME).read_text())
    target = config.workflow_dir(repo) / "spec.toml"
    target.parent.mkdir(parents=True)
    target.write_text((root / ".project-sandbox-tui/workflows/spec.toml").read_text())
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert main([str(repo), "--trust-config"]) == 0
    loaded = config.load(repo)
    cfg = fake_cfg()
    cfg.actions, cfg.commands, cfg.workflows = loaded.actions, loaded.commands, loaded.workflows

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app.dry_run = True
        await pilot.press("j")
        await app.run_command(":workflow")
        await pilot.pause()
        assert any(
            "pre-release" in str(n.message) and "spec" in str(n.message) for n in app._notifications
        )
        await app.run_command(":workflow spec CSV export")
        picker = await wait_for_screen(app, pilot, WorkflowProfilesScreen)
        stages = " ".join(str(item.render()) for item in picker.query(".stage"))
        for name in ("spec/plan", "spec/check", "spec/implement", "spec/chores"):
            assert name in stages
        await pilot.press("enter")
        preview = await wait_for_screen(app, pilot, MessageScreen)
        assert all(
            name in preview.body
            for name in ("spec/plan", "spec/check", "spec/implement", "spec/chores")
        )
        await pilot.press("escape")
        await start_workflow(app, pilot, ":workflow pre-release")
        preview = await wait_for_screen(app, pilot, MessageScreen)
        assert all(
            name in preview.body for name in ("release-docs", "check", "Human review", "smoke")
        ), preview.body

    run_app(repo, cfg, scenario)
