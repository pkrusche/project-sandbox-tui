"""The model catalogue, personas and their resolution into launch settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from project_sandbox_tui import catalogue as cat
from project_sandbox_tui import config as c
from project_sandbox_tui.sandbox import LaunchSpec, build_argv


def cfg_with(**data: object) -> c.Config:
    cfg = c.from_dict(data)  # type: ignore[arg-type]
    cfg.validate()
    return cfg


def row(name: str) -> cat.ProviderRow:
    return cat.ROW_BY_ID[name]


# ---------------------------------------------------------------- catalogue


def test_missing_copilot_tiers_are_none_with_a_reason() -> None:
    cfg = c.Config()
    assert cat.model_for(cfg, row("copilot-anthropic"), "frontier") is None
    assert cat.tier_problem(cfg, row("copilot-anthropic"), "frontier") == (
        "Copilot/Anthropic has no frontier model"
    )
    assert cat.tier_problem(cfg, row("copilot-gemini"), "fast") == ""


def test_catalogue_overrides() -> None:
    cfg = cfg_with(
        catalogue={
            "models": {
                "gateway-anthropic": {"high": "claude-opus-5"},
                "openai": {"fast": "gpt-6-mini", "frontier": ""},
                "codex": {"fast": "gpt-6-luna-codex"},
            }
        }
    )
    assert cat.model_for(cfg, row("gateway-anthropic"), "high") == "claude-opus-5"
    assert cat.model_for(cfg, row("claude-code"), "high") == "claude-opus-5-5"
    assert cat.model_for(cfg, row("gateway-openai"), "fast") == "gpt-6-mini"
    assert cat.model_for(cfg, row("codex"), "fast") == "gpt-6-luna-codex"  # row beats vendor
    assert cat.model_for(cfg, row("codex"), "frontier") is None  # empty disables


@pytest.mark.parametrize(
    ("catalogue", "message"),
    [
        ({"models": {"gateway-mistral": {"high": "x"}}}, "catalogue.models.gateway-mistral"),
        ({"models": {"codex": {"ultra": "x"}}}, "catalogue.models.codex.ultra"),
        ({"models": {"codex": {"high": "a b"}}}, "invalid model name"),
        ({"gateway_agent": "codex"}, "catalogue.gateway_agent must be one of pi, opencode"),
        ({"default_provider": "mistral"}, "catalogue.default_provider"),
        ({"colour": "x"}, r"\[catalogue\] has unknown key"),
    ],
)
def test_invalid_catalogue(catalogue: dict[str, object], message: str) -> None:
    with pytest.raises(c.ConfigError, match=message):
        cfg_with(catalogue=catalogue)


# ---------------------------------------------------------------- personas


def test_internal_personas_are_excluded_from_launch_choices() -> None:
    personas = c.Config().personas
    assert {name for name, p in personas.items() if not p.internal} == set(
        cat.offered_personas(personas)
    )
    assert "commit-reviewer" not in cat.offered_personas(personas)


def test_persona_override_is_field_wise(tmp_path: Path) -> None:
    global_path = tmp_path / "g.toml"
    global_path.write_text(
        '[personas.reviewer]\ntier = "frontier"\n[personas.triage]\ntier = "fast"\n'
    )
    cfg = c.load(tmp_path, global_path)
    assert cfg.personas["reviewer"] == cat.Persona("frontier", "high", False, 4)
    assert cfg.personas["triage"] == cat.Persona("fast")
    assert "triage" in cat.offered_personas(cfg.personas)


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({"triage": {"effort": "low"}}, "personas.triage.tier"),
        ({"reviewer": {"level": 6}}, "personas.reviewer.level"),
        ({"reviewer": {"level": 0}}, "personas.reviewer.level"),
        ({"reviewer": {"tier": "huge"}}, "personas.reviewer.tier"),
        ({"reviewer": {"effort": "extreme"}}, "personas.reviewer.effort"),
        ({"reviewer": {"fast": "yes"}}, "personas.reviewer.fast must be true or false"),
        ({"Bad": {"tier": "fast"}}, "persona names"),
        ({"reviewer": {"mood": "grumpy"}}, r"\[personas.reviewer\] has unknown key"),
    ],
)
def test_invalid_personas(table: dict[str, object], message: str) -> None:
    with pytest.raises(c.ConfigError, match=message):
        cfg_with(personas=table)


# ---------------------------------------------------------------- resolution


def resolve(
    cfg: c.Config,
    persona: str,
    provider: str,
    last: c.LastUsed | None = None,
    action: c.Action | None = None,
    level: int | None = None,
) -> c.ResolvedSetup:
    choice = c.StepChoice("persona", persona=persona)
    return c.resolve_selection(
        cfg, last or c.LastUsed(), c.Selection(choice, provider, level), action
    )


def argv_for(cfg: c.Config, setup: c.ResolvedSetup, tmp_path: Path) -> list[str]:
    cfg.project.base_image = "python:3.12-slim"
    spec = LaunchSpec(
        "feat",
        setup.agent,
        setup.model,
        setup.effort,
        fast=setup.fast,
        gateway=setup.gateway,
        extra_args=setup.extra_args,
        level=setup.level,
    )
    return build_argv(cfg, tmp_path, spec)


GATEWAY = {"network": {"agent_proxy": "http://127.0.0.1:4000/v1"}}


@pytest.mark.parametrize(
    ("persona", "provider", "agent", "model", "effort", "fast", "level"),
    [
        ("reviewer", "codex", "codex", "gpt-6.2-sol", "high", False, 4),
        ("checker", "claude-code", "claude", "claude-sonnet-5-5", "low", True, 2),
        ("implementer", "copilot-anthropic", "copilot", "claude-sonnet-5.5", "medium", False, 2),
        ("chore-runner", "gateway-openai", "pi", "gpt-6-luna", "medium", False, 2),
        ("debugger", "gateway-gemini", "pi", "gemini-4-carbon", "", False, 2),
        (
            "planner",
            "gateway-anthropic",
            "opencode",
            "agent-proxy/claude-opus-5-5",
            "high",
            False,
            3,
        ),
    ],
)
def test_persona_launch_arguments(
    tmp_path: Path,
    persona: str,
    provider: str,
    agent: str,
    model: str,
    effort: str,
    fast: bool,
    level: int,
) -> None:
    cfg = cfg_with(**GATEWAY, catalogue={"gateway_agent": agent if agent == "opencode" else "pi"})
    setup = resolve(cfg, persona, provider)
    argv = argv_for(cfg, setup, tmp_path)
    assert setup.level == level
    assert f"--agent={agent}" in argv and f"--model={model}" in argv
    assert [arg for arg in argv if arg.startswith("--effort=")] == (
        [f"--effort={effort}"] if effort else []
    )
    assert ("--fast" in argv) == fast
    assert ("--agent-proxy=http://127.0.0.1:4000/v1" in argv) == provider.startswith("gateway-")


def test_missing_tier_is_a_problem_not_a_substitute() -> None:
    setup = resolve(c.Config(), "debugger", "copilot-anthropic")
    assert setup.problem == "Copilot/Anthropic has no frontier model"
    assert setup.model == ""


def test_gateway_row_without_gateway_is_a_problem() -> None:
    assert (
        "no agent gateway is configured" in resolve(c.Config(), "planner", "gateway-openai").problem
    )


def test_overridden_persona_on_claude_code() -> None:
    cfg = cfg_with(personas={"reviewer": {"tier": "frontier"}})
    setup = resolve(cfg, "reviewer", "claude-code")
    assert (setup.model, setup.effort, setup.level) == ("claude-fable-5-2", "high", 4)


def test_persona_launch_adds_no_profile_arguments(tmp_path: Path) -> None:
    cfg = cfg_with(sandbox={"extra_args": ["--memory=12g"]})
    last = c.LastUsed(extra_args=["--cpus=8"], profile="")
    setup = resolve(cfg, "implementer", "codex", last)
    argv = argv_for(cfg, setup, tmp_path)
    assert "--memory=12g" in argv and "--cpus=8" not in argv


def test_starting_levels() -> None:
    cfg = cfg_with(security={"level": 3})
    assert resolve(cfg, "reviewer", "codex").start_level == 4
    action = c.Action(prompt="x", persona="reviewer", level=5)
    assert resolve(cfg, "reviewer", "codex", action=action).level == 5
    assert resolve(cfg, "reviewer", "codex", level=1).level == 1
    profile = c.resolve_selection(cfg, c.LastUsed(), c.Selection(c.CUSTOM))
    assert (profile.level, profile.start_level) == (3, 3)
    plain = c.resolve_selection(c.Config(), c.LastUsed(), c.Selection(c.CUSTOM))
    assert plain.level is None  # no security level: the earlier behaviour


def test_other_kinds_reuse_action_settings() -> None:
    cfg = cfg_with(profiles={"deep": {"agent": "codex", "effort": "high", "extra_args": ["--x"]}})
    last = c.LastUsed()
    action = c.Action(prompt="x", effort="low", level=2)
    for choice in (c.StepChoice("profile", "deep"), c.ACTION_SETTINGS, c.CUSTOM):
        setup = c.resolve_selection(cfg, last, c.Selection(choice), action)
        expected = c.resolve_action_settings(cfg, last, action, choice)
        assert (setup.agent, setup.model, setup.effort, setup.extra_args) == (
            expected.agent,
            expected.model,
            expected.effort,
            expected.extra_args,
        )
        assert setup.level == 2
