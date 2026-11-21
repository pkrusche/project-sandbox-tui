"""Launch form choices driven by discovered launcher capabilities."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Static

from project_sandbox_tui import capabilities
from project_sandbox_tui.app import SandboxTUI
from project_sandbox_tui.config import AgentProfile, Config
from project_sandbox_tui.screens.agent import AgentControls
from project_sandbox_tui.screens.launch import LaunchScreen
from project_sandbox_tui.widgets import CycleField
from tests.test_app import run_app, wait_until


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
