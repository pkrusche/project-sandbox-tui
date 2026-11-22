"""The built-in model catalogue and task personas.

Users pick a *provider row* (a route plus a vendor, with a fixed agent) and a
*persona* (a task role with a tier, effort, fast mode and security level);
sbx-tui turns that pair into launcher settings. The catalogue is static: config
overrides (``[catalogue.models.*]``, ``[personas.*]``) cover local drift.

This module holds data and pure lookups only; config.py validates the
configured parts and capabilities.py decides what is available on this host.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from project_sandbox_tui.config import Config

TIERS: tuple[str, ...] = ("fast", "medium", "high", "frontier")
VENDORS: tuple[str, ...] = ("openai", "anthropic", "google")
GATEWAY_CHOICES: tuple[str, ...] = ("pi", "opencode")
GATEWAY = "gateway"  # ProviderRow.agent of gateway rows: catalogue.gateway_agent decides

VENDOR_TIERS: dict[str, dict[str, str]] = {
    "openai": {
        "fast": "gpt-6-luna",
        "medium": "gpt-6.1-terra",
        "high": "gpt-6.2-sol",
        "frontier": "gpt-6.2-astra",
    },
    "anthropic": {
        "fast": "claude-haiku-5-5",
        "medium": "claude-sonnet-5-5",
        "high": "claude-opus-5-5",
        "frontier": "claude-fable-5-2",
    },
    "google": {
        "fast": "gemini-4-hydrogen",
        "medium": "gemini-4-argon",
        "high": "gemini-4-neon",
        "frontier": "gemini-4-carbon",
    },
}


@dataclass(frozen=True)
class ProviderRow:
    id: str
    label: str
    vendor: str
    agent: str  # GATEWAY for gateway rows
    gateway: bool = False
    effort: bool = True  # False: --effort is omitted and the provider default applies
    fast: bool = False  # only Codex and Claude Code have a fast mode
    # Per-row tier models over the vendor map; "" = the tier is unavailable.
    # Listed explicitly: Copilot's IDs are not derived from the vendor IDs.
    models: Mapping[str, str] = field(default_factory=dict)


ROWS: tuple[ProviderRow, ...] = (
    ProviderRow("codex", "Codex", "openai", "codex", fast=True),
    ProviderRow("claude-code", "Claude Code", "anthropic", "claude", fast=True),
    ProviderRow("gateway-openai", "Gateway/OpenAI", "openai", GATEWAY, gateway=True),
    ProviderRow("gateway-anthropic", "Gateway/Anthropic", "anthropic", GATEWAY, gateway=True),
    ProviderRow("gateway-gemini", "Gateway/Gemini", "google", GATEWAY, gateway=True, effort=False),
    ProviderRow("copilot-openai", "Copilot/OpenAI", "openai", "copilot"),
    ProviderRow(
        "copilot-anthropic",
        "Copilot/Anthropic",
        "anthropic",
        "copilot",
        models={
            "fast": "claude-haiku-5.5",
            "medium": "claude-sonnet-5.5",
            "high": "claude-opus-5.5",
            "frontier": "",
        },
    ),
    ProviderRow(
        "copilot-gemini",
        "Copilot/Gemini",
        "google",
        "copilot",
        models={"fast": "gemini-4-hydrogen", "medium": "", "high": "", "frontier": ""},
    ),
)
ROW_BY_ID: dict[str, ProviderRow] = {row.id: row for row in ROWS}


def row_agent(cfg: Config, row: ProviderRow) -> str:
    """The ``--agent`` a launch from ``row`` uses."""
    return cfg.catalogue.gateway_agent if row.gateway else row.agent


def model_for(cfg: Config, row: ProviderRow, tier: str) -> str | None:
    """The model ``row`` runs for ``tier``, or None when the tier is unavailable.

    Built-in vendor and row entries, then ``[catalogue.models.<vendor>]``, then
    ``[catalogue.models.<row>]``; an empty value disables the tier. Never falls
    back to another tier or row.
    """
    model = row.models.get(tier, VENDOR_TIERS[row.vendor].get(tier, ""))
    overrides = cfg.catalogue.models
    for key in (row.vendor, row.id):
        if tier in overrides.get(key, {}):
            model = overrides[key][tier]
    return model or None


def tier_problem(cfg: Config, row: ProviderRow, tier: str) -> str:
    """Why ``row`` cannot serve ``tier``; "" when it can."""
    if model_for(cfg, row, tier) is None:
        return f"{row.label} has no {tier} model"
    return ""


def models_for_agent(cfg: Config, agent: str) -> list[str]:
    """Catalogue models of the rows that use ``agent``, in row and tier order."""
    out: list[str] = []
    for row in ROWS:
        if row_agent(cfg, row) != agent:
            continue
        out += [m for tier in TIERS if (m := model_for(cfg, row, tier))]
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- personas


@dataclass(frozen=True)
class Persona:
    tier: str
    effort: str = ""  # "" = the agent's own default
    fast: bool = False
    level: int | None = None  # None = the project default
    internal: bool = False  # used by sbx-tui itself, never offered in forms

    def label(self) -> str:
        parts = [self.tier, self.effort, "fast" if self.fast else ""]
        if self.level is not None:
            parts.append(f"level {self.level}")
        return " / ".join(part for part in parts if part)


COMMIT_REVIEWER = "commit-reviewer"
IMPLEMENTER = "implementer"

BUILTIN_PERSONAS: dict[str, Persona] = {
    "planner": Persona("high", "high", False, 3),
    "checker": Persona("medium", "low", True, 2),
    IMPLEMENTER: Persona("medium", "medium", False, 2),
    "debugger": Persona("frontier", "xhigh", False, 2),
    "chore-runner": Persona("fast", "medium", True, 2),
    "reviewer": Persona("high", "high", False, 4),
    COMMIT_REVIEWER: Persona("fast", "low", False, 3, internal=True),
}
PERSONA_KEYS: dict[str, object] = {"tier": "", "effort": "", "fast": False, "level": 0}


def offered_personas(personas: Mapping[str, Persona]) -> list[str]:
    """Persona names launch forms and the workflow picker offer."""
    return [name for name, persona in personas.items() if not persona.internal]
