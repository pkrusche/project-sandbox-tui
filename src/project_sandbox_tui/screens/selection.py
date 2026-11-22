"""Provider and security-level fields shared by the launch forms and the workflow picker."""

from __future__ import annotations

from collections.abc import Callable

from project_sandbox_tui import capabilities as caps_mod
from project_sandbox_tui import catalogue
from project_sandbox_tui.capabilities import Capabilities
from project_sandbox_tui.config import LEVELS, Config, project_level
from project_sandbox_tui.security import LEVEL_LABELS, level_hint
from project_sandbox_tui.widgets import CycleField

# The setup selector's persona option: profile names cannot have surrounding
# spaces, so this never collides with a profile called "persona".
PERSONA_SETUP = " persona"
LEVEL_OPTIONS = [str(level) for level in LEVELS]


def app_capabilities(widget: object) -> Capabilities:
    """The app's capability snapshot (kept current by its refresh worker)."""
    try:
        found = getattr(getattr(widget, "app", None), "capabilities", None)
    except Exception:  # not mounted yet
        found = None
    return found if isinstance(found, Capabilities) else caps_mod.fallback()


def provider_reason(cfg: Config, caps: Capabilities, row_id: str, tier: str = "") -> str:
    """Why a provider row cannot run (a missing tier first); "" when it can."""
    row = catalogue.ROW_BY_ID[row_id]
    if tier and (problem := catalogue.tier_problem(cfg, row, tier)):
        return problem.removeprefix(f"{row.label} ")
    result = caps_mod.provider_availability(cfg, caps, row)
    return result.reason if result.available is False else ""


def provider_labels(cfg: Config, caps: Capabilities, tier: str = "") -> dict[str, str]:
    labels = {}
    for row in catalogue.ROWS:
        reason = provider_reason(cfg, caps, row.id, tier)
        labels[row.id] = f"{row.label} — unavailable: {reason}" if reason else row.label
    return labels


def provider_available(cfg: Config, caps: Capabilities) -> Callable[[str], bool]:
    """A predicate for :func:`config.remembered_provider`."""
    return lambda row_id: not provider_reason(cfg, caps, row_id)


def level_labels(cfg: Config, caps: Capabilities) -> dict[str, str]:
    labels = {}
    for option in LEVEL_OPTIONS:
        level = int(option)
        problems = caps_mod.level_problems(caps, cfg, level)
        label = LEVEL_LABELS[level]
        labels[option] = f"{label} — unavailable" if problems else label
    return labels


def show_level(field: CycleField, cfg: Config, level: int | None) -> None:
    """Show ``level`` in ``field``, red below the project default."""
    default = project_level(cfg)
    field.set_class(level is not None and default is not None and level < default, "below-default")


def describe_level(cfg: Config, caps: Capabilities, level: int | None, agent: str) -> str:
    """The level's flags and caveats, then why the launcher cannot run it."""
    text = level_hint(cfg, level, agent)
    if level is not None:
        default = project_level(cfg)
        if default is not None and level < default:
            text = f"below the project default {default} · {text}"
        for problem in caps_mod.level_problems(caps, cfg, level):
            text += f"\nunavailable: {problem}"
    return text
