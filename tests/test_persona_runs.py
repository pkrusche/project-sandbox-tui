"""Persona actions, workflow steps with providers and levels, and their dry runs."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from project_sandbox_tui.app import SandboxTUI
from project_sandbox_tui.config import Action, Config, LastUsed, Workflow
from project_sandbox_tui.sandbox import LaunchSpec
from project_sandbox_tui.screens.dialogs import ConfirmScreen, MessageScreen
from project_sandbox_tui.screens.launch import LaunchScreen
from project_sandbox_tui.screens.workflow import WorkflowProfilesScreen
from project_sandbox_tui.widgets import CycleField
from tests.test_app import run_app, wait_for_screen, wait_until

GATEWAY = "http://127.0.0.1:4000/v1"


def text(widget: Any) -> str:
    return str(widget.render())


# ---------------------------------------------------------------- action runs


def test_review_action_with_a_persona(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    cfg = fake_cfg()
    cfg.actions = {"review": Action(prompt="review {bookmark}", persona="reviewer")}
    LastUsed(selection="persona", provider="claude-code", persona="implementer").save(repo)

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j")
        await app.run_command(":review")
        await wait_until(pilot, lambda: "feature/x" in app.jobs.jobs)
        job = app.jobs.jobs["feature/x"]
        assert {"--agent=claude", "--model=claude-opus-5-5", "--effort=high"} <= set(job.argv)
        assert {"--egress=sidecar", "--vcs-access=read-only"} <= set(job.argv)
        assert (job.spec.persona, job.spec.provider, job.spec.level) == (
            "reviewer",
            "claude-code",
            4,
        )
        await wait_until(pilot, lambda: not app.jobs.running_count(), timeout=15)
        # An action run leaves the remembered selection alone.
        assert LastUsed.load(repo).persona == "implementer"

    run_app(repo, cfg, scenario)


def test_action_below_the_project_default_asks_first(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    cfg = fake_cfg()
    cfg.security.level = 3
    cfg.defaults.agent = "bash"
    cfg.actions = {"e2e": Action(prompt="e2e {bookmark}", level=1)}

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j")
        await app.run_command(":e2e")
        confirm = await wait_for_screen(app, pilot, ConfirmScreen)
        assert "level 1, below the project default 3" in confirm.title_text
        assert "This run uses level 1, below the project default 3" in confirm.body
        assert "--no-firewall" in confirm.body
        await pilot.press("n")
        await pilot.pause()
        assert not app.jobs.jobs and not app.queue.get("feature/x").tasks
        await app.run_command(":e2e")
        await wait_for_screen(app, pilot, ConfirmScreen)
        await pilot.press("y")
        await wait_until(pilot, lambda: "feature/x" in app.jobs.jobs)
        assert "--no-firewall" in app.jobs.jobs["feature/x"].argv

    run_app(repo, cfg, scenario)


def test_action_level_without_security_levels(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    cfg = fake_cfg()
    cfg.network.no_firewall = True
    cfg.actions = {"e2e": Action(prompt="e2e {bookmark}", agent="bash", level=2)}

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j")
        await app.run_command(":e2e")  # no project default: nothing is below it
        await wait_until(pilot, lambda: "feature/x" in app.jobs.jobs)
        argv = app.jobs.jobs["feature/x"].argv
        assert "--egress=firewall" in argv and "--no-firewall" not in argv

    run_app(repo, cfg, scenario)


def test_persona_action_without_the_tier_is_refused(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    cfg = fake_cfg()
    cfg.actions = {"dig": Action(prompt="dig {bookmark}", persona="debugger")}
    LastUsed(selection="persona", provider="copilot-anthropic", persona="debugger").save(repo)

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j")
        await app.run_command(":dig")
        await pilot.pause()
        assert not app.jobs.jobs
        assert any("has no frontier model" in str(n.message) for n in app._notifications)

    run_app(repo, cfg, scenario)


# ---------------------------------------------------------------- workflows


def persona_cfg(fake_cfg: Callable[..., Config]) -> Config:
    cfg = fake_cfg()
    cfg.network.agent_proxy = GATEWAY
    cfg.actions = {
        "spec-plan": Action("report:success"),
        "spec-implement": Action("report:success", persona="implementer"),
        "spec-check": Action("report:success", persona="checker"),
        "spec-chores": Action("report:success", persona="chore-runner"),
        "deep-dive": Action("report:success", persona="debugger"),
        "review": Action("report:success", persona="reviewer"),
    }
    cfg.workflows = {
        "spec": Workflow(
            "spec-plan", ["spec-implement", "spec-check"], first_step_interactive=False
        ),
        "chores": Workflow("spec-plan", ["spec-chores", "deep-dive"], first_step_interactive=False),
        "review": Workflow("review", ["spec-check"], first_step_interactive=False),
    }
    return cfg


def test_workflow_on_another_provider_is_snapshotted(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    cfg = persona_cfg(fake_cfg)
    LastUsed(profile="", selection="persona", provider="codex", persona="planner").save(repo)

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        branch = "feature/x"
        await pilot.press("j")
        app.queue.pause(branch, True)
        await app.run_command(":workflow spec")
        picker = await wait_for_screen(app, pilot, WorkflowProfilesScreen)
        assert picker.query_one("#stage-0", CycleField).value == "persona planner"  # remembered
        for index in (0, 1, 2):
            assert picker.query_one(f"#stage-provider-{index}", CycleField).value == "codex"
            picker.query_one(f"#stage-provider-{index}", CycleField).value = "gateway-gemini"
        await pilot.pause()
        assert "gemini-4-neon" in text(picker.query_one("#stage-hint-1"))
        await pilot.press("enter")
        await wait_until(pilot, lambda: bool(app.queue.get(branch).tasks))

        # Later catalogue edits and a reload (as after a restart) keep the snapshot.
        app.cfg.catalogue.models["google"] = {"medium": "gemini-changed"}
        app.queue.load()
        planner = app.queue.get(branch).tasks[0]
        assert planner.workflow is not None
        steps = {n: s for n, s in planner.workflow.next_steps if isinstance(s, LaunchSpec)}
        for name, persona in (("spec-implement", "implementer"), ("spec-check", "checker")):
            step = steps[name]
            assert (step.agent, step.model, step.gateway, step.level) == (
                "pi",
                "gemini-4-neon",
                True,
                2,
            )
            assert (step.persona, step.provider, step.effort) == (persona, "gateway-gemini", "")
        # Picker choices are not remembered.
        assert LastUsed.load(repo).provider == "codex"
        await pilot.press("r")
        form = await wait_for_screen(app, pilot, LaunchScreen)
        assert form.query_one("#provider", CycleField).value == "codex"

    run_app(repo, cfg, scenario)


def test_picker_offers_the_action_persona(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    cfg = persona_cfg(fake_cfg)
    LastUsed(profile="").save(repo)

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j")
        await app.run_command(":workflow chores")
        picker = await wait_for_screen(app, pilot, WorkflowProfilesScreen)
        chores = picker.query_one("#stage-1", CycleField)
        assert chores.options == ["persona chore-runner", "custom"]
        assert chores.value == "persona chore-runner"
        assert picker.query_one("#stage-provider-1", CycleField).value == "codex"
        assert picker.query_one("#stage-0", CycleField).options == ["custom"]
        assert not picker.query_one("#stage-provider-0", CycleField).display
        # A tier the provider lacks blocks accepting until it changes.
        picker.query_one("#stage-provider-2", CycleField).value = "copilot-anthropic"
        await pilot.pause()
        assert text(picker.query_one("#stage-hint-2")) == "Copilot/Anthropic has no frontier model"
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is picker and not app.queue.get("feature/x").tasks
        picker.query_one("#stage-provider-2", CycleField).value = "claude-code"
        await pilot.pause()
        assert "claude-fable-5-2" in text(picker.query_one("#stage-hint-2"))

    run_app(repo, cfg, scenario)


def test_reviewer_step_level(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    cfg = persona_cfg(fake_cfg)
    cfg.security.level = 3

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j")
        await app.run_command(":workflow review")
        picker = await wait_for_screen(app, pilot, WorkflowProfilesScreen)
        level = picker.query_one("#stage-level-0", CycleField)
        assert level.display and level.value == "4" and not level.has_class("below-default")
        checker = picker.query_one("#stage-level-1", CycleField)
        assert checker.value == "2" and checker.has_class("below-default")
        assert "below the project default 3" in text(picker.query_one("#stage-level-hint-1"))
        level.value = "5"
        await pilot.pause()
        assert picker.choices()[0].level == 5

    run_app(repo, cfg, scenario)


def test_dry_run_preview_of_a_persona_step(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    cfg = persona_cfg(fake_cfg)
    cfg.security.level = 3

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app.dry_run = True
        await pilot.press("j")
        await app.run_command(":workflow review")
        await wait_for_screen(app, pilot, WorkflowProfilesScreen)
        await pilot.press("enter")
        preview = await wait_for_screen(app, pilot, MessageScreen)
        assert (
            "review (background) · persona reviewer · provider Codex · codex/gpt-6.2-sol/high/L4"
            in preview.body
        )
        assert "level 4 · --egress=sidecar --vcs-access=read-only" in preview.body
        assert "level 2 · --egress=firewall" in preview.body  # the checker step
        assert not app.queue.get("feature/x").tasks and not app.jobs.jobs

    run_app(repo, cfg, scenario)
