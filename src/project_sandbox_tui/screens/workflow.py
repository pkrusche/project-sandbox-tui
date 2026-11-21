"""Choose the agent profile of each workflow stage before the workflow starts."""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from project_sandbox_tui.config import (
    ACTION_SETTINGS,
    CUSTOM,
    Action,
    Config,
    LastUsed,
    StepChoice,
    resolve_action_settings,
)
from project_sandbox_tui.widgets import CycleField, KeyHints


@dataclass
class Stage:
    """A workflow stage with profile controls for agent actions."""

    name: str  # the action name
    action: Action | None
    mode: str  # shown next to the name, e.g. "interactive" or "queued"
    initial: StepChoice | None
    detail: str = ""


def stage_choices(cfg: Config, action: Action) -> list[StepChoice]:
    """The choices a stage offers, in display order."""
    choices = [StepChoice("profile", name) for name in cfg.profiles]
    if action.has_own_settings:
        choices.append(ACTION_SETTINGS)
    return [*choices, CUSTOM]


def describe(cfg: Config, last: LastUsed, action: Action, choice: StepChoice) -> Text:
    settings = resolve_action_settings(cfg, last, action, choice)
    text = settings.label()
    if settings.extra_args:
        text += f" · {shlex.join(settings.extra_args)}"
    return Text(text)  # plain: CLI args may contain markup characters


class WorkflowProfilesScreen(ModalScreen[list[StepChoice] | None]):
    """Dismisses with one choice per agent action, or None when cancelled."""

    DEFAULT_CSS = """
    WorkflowProfilesScreen { align: center middle; }
    WorkflowProfilesScreen > Vertical {
        width: 90; max-width: 100%; height: auto; max-height: 100%; overflow-y: auto;
        scrollbar-gutter: stable; scrollbar-size-vertical: 1;
        background: $panel; padding: 1 1 1 2;
    }
    WorkflowProfilesScreen .title { text-style: bold; margin-bottom: 1; }
    WorkflowProfilesScreen .stage { padding-left: 1; }
    WorkflowProfilesScreen .stage-hint { color: $text-muted; padding-left: 12; margin-bottom: 1; }
    WorkflowProfilesScreen #start-row { height: auto; }
    WorkflowProfilesScreen .hint { width: 1fr; color: $text-disabled; }
    """

    BINDINGS = [
        Binding("escape", "cancel", show=False),
        Binding("enter,ctrl+s", "start", show=False, priority=True),
    ]

    def __init__(self, title: str, cfg: Config, last: LastUsed, stages: list[Stage]) -> None:
        super().__init__()
        self.title_text, self.cfg, self.last, self.stages = title, cfg, last, stages
        self.options = [
            stage_choices(cfg, stage.action) if stage.action else [] for stage in stages
        ]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text(self.title_text), classes="title")
            for index, stage in enumerate(self.stages):
                options = self.options[index]
                yield Static(Text(f"{index + 1}. {stage.name} · {stage.mode}"), classes="stage")
                if stage.action is None:
                    if stage.detail:
                        yield Static(Text(stage.detail), classes="stage-hint")
                    continue
                field = CycleField(
                    "profile", [choice.label() for choice in options], id=f"stage-{index}"
                )
                # Set by position: a profile may share a label with "action settings".
                initial = stage.initial if stage.initial in options else options[-1]
                field.set_reactive(CycleField.index, options.index(initial))
                yield field
                yield Static(
                    describe(self.cfg, self.last, stage.action, initial),
                    classes="stage-hint",
                    id=f"stage-hint-{index}",
                )
            with Horizontal(id="start-row"):
                yield Static(
                    KeyHints("tab/j/k stages · h/l change · enter start · esc cancel"),
                    classes="hint",
                )
                yield Button("start", variant="primary", compact=True, id="start")

    def on_mount(self) -> None:
        if self.stages:
            self.query_one("#stage-0", CycleField).focus()

    def choices(self) -> list[StepChoice]:
        return [
            self.options[index][self.query_one(f"#stage-{index}", CycleField).index]
            for index, stage in enumerate(self.stages)
            if stage.action
        ]

    @on(CycleField.Changed)
    def _changed(self, event: CycleField.Changed) -> None:
        field_id = event.field.id or ""
        if not field_id.startswith("stage-"):
            return
        index = int(field_id.removeprefix("stage-"))
        choice = self.options[index][event.field.index]
        hint = self.query_one(f"#stage-hint-{index}", Static)
        action = self.stages[index].action
        assert action is not None
        hint.update(describe(self.cfg, self.last, action, choice))

    @on(Button.Pressed, "#start")
    def action_start(self) -> None:
        self.dismiss(self.choices())

    def action_cancel(self) -> None:
        self.dismiss(None)
