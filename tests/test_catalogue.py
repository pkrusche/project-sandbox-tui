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


def test_provider_rows_in_order() -> None:
    assert [r.label for r in cat.ROWS] == [
        "Codex",
        "Claude Code",
        "Gateway/OpenAI",
        "Gateway/Anthropic",
        "Gateway/Gemini",
        "Copilot/OpenAI",
        "Copilot/Anthropic",
        "Copilot/Gemini",
    ]
    assert [r.id for r in cat.ROWS] == [
        "codex",
        "claude-code",
        "gateway-openai",
        "gateway-anthropic",
        "gateway-gemini",
        "copilot-openai",
        "copilot-anthropic",
        "copilot-gemini",
    ]


BUILT_IN = {
    "codex": ("gpt-6-luna", "gpt-6.1-terra", "gpt-6.2-sol", "gpt-6.2-astra"),
    "gateway-openai": ("gpt-6-luna", "gpt-6.1-terra", "gpt-6.2-sol", "gpt-6.2-astra"),
    "copilot-openai": ("gpt-6-luna", "gpt-6.1-terra", "gpt-6.2-sol", "gpt-6.2-astra"),
    "claude-code": ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-2"),
    "gateway-anthropic": (
        "claude-haiku-5-5",
        "claude-sonnet-5-5",
        "claude-opus-5-5",
        "claude-fable-5-2",
    ),
    "copilot-anthropic": ("claude-haiku-5.5", "claude-sonnet-5.5", "claude-opus-5.5", None),
    "gateway-gemini": ("gemini-4-hydrogen", "gemini-4-argon", "gemini-4-neon", "gemini-4-carbon"),
    "copilot-gemini": ("gemini-4-hydrogen", None, None, None),
}


@pytest.mark.parametrize("row_id", BUILT_IN)
def test_every_built_in_cell(row_id: str) -> None:
    cfg = c.Config()
    got = tuple(cat.model_for(cfg, row(row_id), tier) for tier in cat.TIERS)
    assert got == BUILT_IN[row_id]


def test_copilot_dotted_ids_never_reach_claude_code() -> None:
    cfg = c.Config()
    for tier in cat.TIERS:
        model = cat.model_for(cfg, row("claude-code"), tier)
        assert model is not None and "." not in model
    assert cat.models_for_agent(cfg, "claude") == list(BUILT_IN["claude-code"])


def test_missing_copilot_tiers_are_none_with_a_reason() -> None:
    cfg = c.Config()
    assert cat.model_for(cfg, row("copilot-anthropic"), "frontier") is None
    assert cat.tier_problem(cfg, row("copilot-anthropic"), "frontier") == (
        "Copilot/Anthropic has no frontier model"
    )
    assert cat.tier_problem(cfg, row("copilot-gemini"), "fast") == ""


def test_row_rules() -> None:
    assert [r.id for r in cat.ROWS if r.fast] == ["codex", "claude-code"]
    assert [r.id for r in cat.ROWS if not r.effort] == ["gateway-gemini"]
    assert [r.id for r in cat.ROWS if r.gateway] == [
        "gateway-openai",
        "gateway-anthropic",
        "gateway-gemini",
    ]


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


def test_built_in_personas() -> None:
    p = c.Config().personas
    assert (p["planner"].tier, p["planner"].effort, p["planner"].fast, p["planner"].level) == (
        "high",
        "high",
        False,
        3,
    )
    assert (p["checker"].tier, p["checker"].effort, p["checker"].fast, p["checker"].level) == (
        "medium",
        "low",
        True,
        2,
    )
    assert p["implementer"] == cat.Persona("medium", "medium", False, 2)
    assert p["debugger"] == cat.Persona("frontier", "xhigh", False, 2)
    assert p["chore-runner"] == cat.Persona("fast", "medium", True, 2)
    assert p["reviewer"] == cat.Persona("high", "high", False, 4)
    assert p["commit-reviewer"].internal
    assert cat.offered_personas(p) == [
        "planner",
        "checker",
        "implementer",
        "debugger",
        "chore-runner",
        "reviewer",
    ]


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


def test_reviewer_on_codex(tmp_path: Path) -> None:
    setup = resolve(c.Config(), "reviewer", "codex")
    argv = argv_for(c.Config(), setup, tmp_path)
    assert {"--agent=codex", "--model=gpt-6.2-sol", "--effort=high"} <= set(argv)
    assert setup.level == 4 and "--fast" not in argv


def test_checker_on_claude_code() -> None:
    setup = resolve(c.Config(), "checker", "claude-code")
    assert (setup.agent, setup.model, setup.effort, setup.fast, setup.level) == (
        "claude",
        "claude-sonnet-5-5",
        "low",
        True,
        2,
    )


def test_agent_follows_from_the_row(tmp_path: Path) -> None:
    cfg = cfg_with(**GATEWAY)
    argv = argv_for(cfg, resolve(cfg, "implementer", "claude-code"), tmp_path)
    assert "--agent=claude" in argv and not any(a.startswith("--agent-proxy") for a in argv)


def test_codex_high_tier(tmp_path: Path) -> None:
    cfg = cfg_with(personas={"deep": {"tier": "high"}})
    argv = argv_for(cfg, resolve(cfg, "deep", "codex"), tmp_path)
    assert "--agent=codex" in argv and "--model=gpt-6.2-sol" in argv


def test_copilot_uses_its_own_ids(tmp_path: Path) -> None:
    setup = resolve(c.Config(), "implementer", "copilot-anthropic")
    argv = argv_for(c.Config(), setup, tmp_path)
    assert "--agent=copilot" in argv and "--model=claude-sonnet-5.5" in argv
    claude = argv_for(c.Config(), resolve(c.Config(), "implementer", "claude-code"), tmp_path)
    assert "--model=claude-sonnet-5-5" in claude


def test_gateway_row_with_opencode(tmp_path: Path) -> None:
    cfg = cfg_with(**GATEWAY, catalogue={"gateway_agent": "opencode"})
    setup = resolve(cfg, "reviewer", "gateway-anthropic")
    argv = argv_for(cfg, setup, tmp_path)
    assert "--agent=opencode" in argv
    assert "--agent-proxy=http://127.0.0.1:4000/v1" in argv
    assert "--model=agent-proxy/claude-opus-5-5" in argv


def test_pi_gateway_row(tmp_path: Path) -> None:
    cfg = cfg_with(**GATEWAY)
    argv = argv_for(cfg, resolve(cfg, "chore-runner", "gateway-openai"), tmp_path)
    assert {"--agent=pi", "--agent-proxy=http://127.0.0.1:4000/v1", "--model=gpt-6-luna"} <= set(
        argv
    )
    assert "--fast" not in argv  # fast mode only on Codex and Claude Code


def test_gemini_frontier_through_the_gateway() -> None:
    cfg = cfg_with(**GATEWAY)
    setup = resolve(cfg, "debugger", "gateway-gemini")
    assert (setup.model, setup.gateway, setup.agent) == ("gemini-4-carbon", True, "pi")


def test_gemini_gateway_omits_effort(tmp_path: Path) -> None:
    cfg = cfg_with(**GATEWAY)
    setup = resolve(cfg, "planner", "gateway-gemini")
    assert setup.effort == "" and "provider default effort" in setup.label()
    assert not any(a.startswith("--effort") for a in argv_for(cfg, setup, tmp_path))


def test_planner_preview_on_gateway_anthropic() -> None:
    cfg = cfg_with(**GATEWAY)
    label = resolve(cfg, "planner", "gateway-anthropic").label()
    assert label == "pi/claude-opus-5-5/high/gateway · level 3"


def test_fast_mode_only_on_codex_and_claude_code() -> None:
    cfg = cfg_with(**GATEWAY)
    fast = {r.id: resolve(cfg, "checker", r.id).fast for r in cat.ROWS}
    assert [name for name, on in fast.items() if on] == ["codex", "claude-code"]


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
