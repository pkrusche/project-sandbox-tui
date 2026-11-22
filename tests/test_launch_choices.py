"""Launch form choices driven by discovered launcher capabilities."""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, Static, TextArea

from project_sandbox_tui import capabilities
from project_sandbox_tui.app import SandboxTUI
from project_sandbox_tui.config import AgentProfile, Config, LastUsed
from project_sandbox_tui.screens.agent import AgentControls
from project_sandbox_tui.screens.launch import LaunchScreen
from project_sandbox_tui.screens.selection import PERSONA_SETUP
from project_sandbox_tui.widgets import CycleField
from tests.test_app import run_app, wait_for_screen, wait_until


def probed(cfg: Config, agents: tuple[str, ...]) -> capabilities.Capabilities:
    return capabilities.Capabilities(
        command=tuple(cfg.sandbox_command()),
        version=capabilities.installed_version(),
        discovered_at=datetime.now(UTC),
        source="probe",
        agents=agents,
        vcs_modes=("full",),
    )


@pytest.fixture
def empty_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home without agent configuration, for project-sandbox's host rules."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


async def open_form(app: SandboxTUI, pilot: Any) -> tuple[LaunchScreen, AgentControls]:
    await pilot.press("j", "i")
    await wait_until(pilot, lambda: isinstance(app.screen, LaunchScreen))
    form = app.screen
    assert isinstance(form, LaunchScreen)
    return form, form.query_one(AgentControls)


def status(controls: AgentControls) -> str:
    widget = controls.query_one("#agent-status", Static)
    return str(widget.render()) if widget.display else ""


def test_form_opens_without_cache_and_updates_after_refresh(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = fake_cfg()
    release = threading.Event()

    def slow_discovery(self: SandboxTUI, command: list[str]) -> capabilities.Capabilities:
        release.wait(10)
        return probed(cfg, ("claude", "newagent", "bash"))

    monkeypatch.setattr(SandboxTUI, "_discover_capabilities", slow_discovery)

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        _form, controls = await open_form(app, pilot)  # opens while discovery is blocked
        agent = controls.query_one("#agent", CycleField)
        assert "newagent" not in agent.options and "copilot" in agent.options
        release.set()
        await wait_until(pilot, lambda: "newagent" in agent.options)
        assert agent.value == "claude"
        assert app.capabilities.discovered

    try:
        run_app(repo, cfg, scenario)
    finally:
        release.set()


def test_unavailable_agent_is_marked_with_reason(
    repo: Path, fake_cfg: Callable[..., Config], empty_home: Path
) -> None:
    cfg = fake_cfg()
    capabilities.save(probed(cfg, ("claude", "codex", "bash")), capabilities.cache_path(repo))

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        _form, controls = await open_form(app, pilot)
        controls.query_one("#agent", CycleField).value = "codex"
        await pilot.pause()
        assert "codex: unavailable — no ~/.codex on this host" in status(controls)
        assert ".codex" in status(controls)
        controls.query_one("#agent", CycleField).value = "bash"
        await pilot.pause()
        assert status(controls) == ""

    run_app(repo, cfg, scenario)


def test_unsupported_configured_agent_stays_shown(
    repo: Path, fake_cfg: Callable[..., Config], empty_home: Path
) -> None:
    cfg = fake_cfg()
    cfg.defaults.agent = "opencode"
    capabilities.save(probed(cfg, ("claude", "bash")), capabilities.cache_path(repo))

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        _form, controls = await open_form(app, pilot)
        agent = controls.query_one("#agent", CycleField)
        assert agent.value == "opencode" and "opencode" in agent.options
        assert "opencode: not supported by" in status(controls)

    run_app(repo, cfg, scenario)


def test_profile_hint_marks_unsupported_agent(
    repo: Path, fake_cfg: Callable[..., Config], empty_home: Path
) -> None:
    cfg = fake_cfg()
    cfg.profiles = {"old": AgentProfile(agent="opencode")}
    capabilities.save(probed(cfg, ("claude", "bash")), capabilities.cache_path(repo))

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        form, _controls = await open_form(app, pilot)
        hint = str(form.query_one("#profile-hint", Static).render())
        assert "opencode: not supported by" in hint

    run_app(repo, cfg, scenario)


def test_effort_fast_and_gateway_fields_follow_agent(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    cfg = fake_cfg()
    cfg.network.agent_proxy = "http://127.0.0.1:4000/v1"
    cfg.defaults.agent = "pi"

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        _form, controls = await open_form(app, pilot)
        agent = controls.query_one("#agent", CycleField)
        gateway = controls.query_one("#gateway", CycleField)
        effort = controls.query_one("#effort", CycleField)
        fast = controls.query_one("#fast", CycleField)
        assert gateway.display and gateway.value == "off" and not fast.display
        gateway.value = "on"
        await pilot.pause()
        assert controls.current_profile().gateway
        agent.value = "codex"
        await pilot.pause()
        assert not gateway.display and fast.display and effort.display
        assert not controls.current_profile().gateway
        agent.value = "bash"
        await pilot.pause()
        assert gateway.display and not effort.display and not fast.display
        assert gateway.value == "off"
        agent.value = "pi"
        await pilot.pause()
        assert gateway.value == "on"  # remembered per agent

    run_app(repo, cfg, scenario)


def test_gateway_field_hidden_without_gateway(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    cfg = fake_cfg()
    cfg.defaults.agent = "pi"

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        _form, controls = await open_form(app, pilot)
        assert not controls.query_one("#gateway", CycleField).display
        assert not controls.current_profile().gateway

    run_app(repo, cfg, scenario)


def test_bash_gateway_keeps_model_and_launch_requires_one(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    LastUsed(profile="").save(repo)  # custom settings, chosen before
    cfg = fake_cfg()
    cfg.network.agent_proxy = "http://127.0.0.1:4000/v1"
    cfg.defaults.agent = "bash"
    cfg.models = {"bash": ["gpt-5-mini"]}

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        form, controls = await open_form(app, pilot)
        controls.query_one("#gateway", CycleField).value = "on"
        await pilot.pause()
        model = controls.query_one("#model", CycleField)
        assert "gpt-5-mini" in model.options
        form.action_launch()  # no model yet: stays open and starts nothing
        await pilot.pause()
        assert app.screen is form and not app.jobs.jobs
        model.value = "gpt-5-mini"
        await pilot.pause()
        spec = form.current_spec()
        assert (spec.agent, spec.model, spec.gateway) == ("bash", "gpt-5-mini", True)

    run_app(repo, cfg, scenario)


# ---------------------------------------------------------------- persona selection


def hint_text(form: LaunchScreen, selector: str = "#profile-hint") -> str:
    return str(form.query_one(selector, Static).render())


def field(form: LaunchScreen, name: str) -> CycleField:
    return form.query_one(f"#{name}", CycleField)


def test_persona_mode_without_profiles(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    cfg = fake_cfg()

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        form, controls = await open_form(app, pilot)
        assert field(form, "profile").render().plain.startswith("setup")
        assert "persona" in field(form, "profile").render().plain
        assert field(form, "provider").display and field(form, "persona").display
        assert not controls.display
        assert (field(form, "provider").value, field(form, "persona").value) == (
            "codex",
            "implementer",
        )
        assert field(form, "persona").options == [
            "planner",
            "checker",
            "implementer",
            "debugger",
            "chore-runner",
            "reviewer",
        ]
        assert field(form, "provider").options == [
            "codex",
            "claude-code",
            "gateway-openai",
            "gateway-anthropic",
            "gateway-gemini",
            "copilot-openai",
            "copilot-anthropic",
            "copilot-gemini",
        ]
        labels = field(form, "provider").labels
        assert labels["claude-code"] == "Claude Code"
        assert labels["gateway-anthropic"] == (
            "Gateway/Anthropic — unavailable: no agent gateway is configured (network.agent_proxy)"
        )
        spec = form.current_spec()
        assert (spec.agent, spec.model, spec.effort, spec.level) == (
            "codex",
            "gpt-6.1-terra",
            "medium",
            2,
        )
        assert (spec.persona, spec.provider, spec.extra_args) == ("implementer", "codex", [])
        assert field(form, "level").display and field(form, "level").value == "2"

    run_app(repo, cfg, scenario)


def test_profile_mode_unchanged(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    cfg = fake_cfg()
    cfg.profiles = {"deep": AgentProfile("codex", "m", "high", True, ["--memory=16g"])}

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        form, controls = await open_form(app, pilot)
        assert field(form, "profile").value == "deep"
        assert not field(form, "provider").display and not controls.display
        assert not field(form, "level").display  # no security level configured
        spec = form.current_spec()
        assert (spec.agent, spec.model, spec.effort, spec.fast, spec.extra_args) == (
            "codex",
            "m",
            "high",
            True,
            ["--memory=16g"],
        )
        assert spec.level is None and spec.persona == ""

    run_app(repo, cfg, scenario)


def test_remembered_persona_selection(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    LastUsed(selection="persona", provider="claude-code", persona="debugger").save(repo)
    cfg = fake_cfg()
    cfg.profiles = {"deep": AgentProfile("codex")}

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        form, _controls = await open_form(app, pilot)
        assert (field(form, "provider").value, field(form, "persona").value) == (
            "claude-code",
            "debugger",
        )
        assert form.current_spec().model == "claude-fable-5-2"

    run_app(repo, cfg, scenario)


def test_default_provider_starts_the_form(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    cfg = fake_cfg()
    cfg.catalogue.default_provider = "claude-code"

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        form, _controls = await open_form(app, pilot)
        assert field(form, "provider").value == "claude-code"

    run_app(repo, cfg, scenario)


def test_persona_launch_remembers_selection_not_custom_settings(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    LastUsed(profile="", extra_args=["--memory=12g"], gateway_by_agent={"pi": False}).save(repo)
    cfg = fake_cfg()
    cfg.network.agent_proxy = "http://127.0.0.1:4000/v1"

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j", "r")
        form = await wait_for_screen(app, pilot, LaunchScreen)
        field(form, "profile").value = PERSONA_SETUP
        await pilot.pause()
        field(form, "provider").value = "gateway-openai"
        await pilot.pause()
        form.query_one("#prompt", TextArea).text = "exit:0"
        spec = form.current_spec()
        assert (spec.agent, spec.gateway, spec.extra_args) == ("pi", True, [])
        await pilot.press("ctrl+s")
        await wait_until(pilot, lambda: "feature/x" in app.jobs.jobs)
        last = LastUsed.load(repo)
        assert (last.selection, last.provider, last.persona) == (
            "persona",
            "gateway-openai",
            "implementer",
        )
        assert last.extra_args == ["--memory=12g"] and last.gateway_by_agent == {"pi": False}
        await wait_until(pilot, lambda: not app.jobs.jobs["feature/x"].running)
        await pilot.press("r")
        form = await wait_for_screen(app, pilot, LaunchScreen)
        assert field(form, "persona").value == "implementer"
        field(form, "profile").value = "custom"
        await pilot.pause()
        assert form.query_one("#extra-args", Input).value == "--memory=12g"
        form.query_one("#agent", CycleField).value = "pi"
        await pilot.pause()
        assert form.query_one("#gateway", CycleField).value == "off"

    run_app(repo, cfg, scenario)


def test_frontier_persona_on_copilot_anthropic_is_refused(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    cfg = fake_cfg()

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        form, _controls = await open_form(app, pilot)
        field(form, "persona").value = "debugger"
        await pilot.pause()
        assert field(form, "provider").labels["copilot-anthropic"] == (
            "Copilot/Anthropic — unavailable: has no frontier model"
        )
        field(form, "provider").value = "copilot-anthropic"
        await pilot.pause()
        assert hint_text(form) == "Copilot/Anthropic has no frontier model"
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is form and not app.jobs.jobs

    run_app(repo, cfg, scenario)


def test_planner_hint_on_gateway_anthropic(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    cfg = fake_cfg()
    cfg.network.agent_proxy = "http://127.0.0.1:4000/v1"

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        form, _controls = await open_form(app, pilot)
        field(form, "persona").value = "planner"
        field(form, "provider").value = "gateway-anthropic"
        await pilot.pause()
        assert hint_text(form) == "pi/claude-opus-5-5/high/gateway · level 3"
        field(form, "provider").value = "gateway-gemini"
        await pilot.pause()
        assert "provider default effort" in hint_text(form)
        assert "--effort" not in str(form.query_one("#preview", Static).render())

    run_app(repo, cfg, scenario)


def test_codex_without_host_configuration_is_unavailable(
    repo: Path, fake_cfg: Callable[..., Config], empty_home: Path
) -> None:
    cfg = fake_cfg()
    capabilities.save(probed(cfg, ("claude", "codex", "bash")), capabilities.cache_path(repo))

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        form, _controls = await open_form(app, pilot)
        label = field(form, "provider").labels["codex"]
        assert label.startswith("Codex — unavailable: no ~/.codex on this host")
        assert "not supported" in field(form, "provider").labels["copilot-openai"]

    run_app(repo, cfg, scenario)


# ---------------------------------------------------------------- level field


def test_level_field_follows_project_default(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    LastUsed(profile="").save(repo)
    cfg = fake_cfg()
    cfg.security.level = 3

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        form, _controls = await open_form(app, pilot)
        level = field(form, "level")
        assert level.display and level.value == "3" and not level.has_class("below-default")
        assert "--egress=sidecar" in hint_text(form, "#level-hint")
        level.value = "2"
        await pilot.pause()
        assert level.has_class("below-default")
        assert "below the project default 3" in hint_text(form, "#level-hint")
        assert form.current_spec().level == 2
        assert "--egress=firewall" in str(form.query_one("#preview", Static).render())
        field(form, "profile").value = PERSONA_SETUP
        await pilot.pause()
        field(form, "persona").value = "reviewer"
        await pilot.pause()
        assert level.value == "4" and not level.has_class("below-default")
        field(form, "provider").value = "copilot-openai"
        await pilot.pause()
        assert "Copilot also allows GitHub hosts" in hint_text(form, "#level-hint")

    run_app(repo, cfg, scenario)


def test_level_is_not_remembered(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    LastUsed(profile="").save(repo)
    cfg = fake_cfg()
    cfg.security.level = 3
    cfg.security.egress = "firewall"

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j", "r")
        form = await wait_for_screen(app, pilot, LaunchScreen)
        field(form, "level").value = "1"
        form.query_one("#prompt", TextArea).text = "exit:0"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await wait_until(pilot, lambda: "feature/x" in app.jobs.jobs)
        assert "--no-firewall" in app.jobs.jobs["feature/x"].argv
        await wait_until(pilot, lambda: not app.jobs.jobs["feature/x"].running)
        await pilot.press("r")
        form = await wait_for_screen(app, pilot, LaunchScreen)
        assert field(form, "level").value == "3"

    run_app(repo, cfg, scenario)


def test_unavailable_levels_are_marked(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    LastUsed(profile="").save(repo)
    cfg = fake_cfg()
    cfg.security.level = 3
    caps = probed(cfg, ("claude", "codex", "bash"))
    caps = dataclasses.replace(caps, vcs_modes=(), egress_modes=("firewall", "sidecar"))
    capabilities.save(caps, capabilities.cache_path(repo))

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        form, _controls = await open_form(app, pilot)
        labels = field(form, "level").labels
        assert labels["3"] == "3 · agent API only"
        assert labels["4"].endswith("— unavailable") and labels["5"].endswith("— unavailable")
        field(form, "level").value = "4"
        await pilot.pause()
        assert "has no --vcs-access option" in hint_text(form, "#level-hint")
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is form and not app.jobs.jobs

    run_app(repo, cfg, scenario)
