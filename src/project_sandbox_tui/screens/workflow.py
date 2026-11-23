"""Choose the agent setup, provider and security level of each workflow stage
before the workflow starts."""

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

from project_sandbox_tui import capabilities as caps_mod
from project_sandbox_tui import catalogue
from project_sandbox_tui.config import (
    ACTION_SETTINGS,
    CUSTOM,
    Action,
    Config,
    LastUsed,
    ResolvedSetup,
    Selection,
    StepChoice,
    resolve_selection,
)
from project_sandbox_tui.screens.selection import (
    LEVEL_OPTIONS,
    app_capabilities,
    describe_level,
    level_labels,
    provider_labels,
    show_level,
)
from project_sandbox_tui.widgets import CycleField, KeyHints


@dataclass
class Stage:
    """A workflow stage with setup controls for agent actions."""

    name: str  # the action name
    action: Action | None
    mode: str  # shown next to the name, e.g. "interactive" or "queued"
    initial: Selection | None
    detail: str = ""


def stage_choices(
    cfg: Config, action: Action, initial: StepChoice | None = None
) -> list[StepChoice]:
    """The choices a stage offers, in display order: its persona (the action's, or a
    remembered persona selection), the profiles, action settings, Custom."""
    choices: list[StepChoice] = []
    if action.persona:
        choices.append(StepChoice("persona", persona=action.persona))
    elif initial is not None and initial.kind == "persona":
        choices.append(initial)
    choices += [StepChoice("profile", name) for name in cfg.profiles]
    if action.has_own_settings:
        choices.append(ACTION_SETTINGS)
    return [*choices, CUSTOM]


def describe(cfg: Config, last: LastUsed, action: Action, selection: Selection) -> Text:
    setup = resolve_selection(cfg, last, selection, action)
    if setup.problem:
        return Text(setup.problem, "red")
    text = setup.label()
    if setup.extra_args:
        text += f" · {shlex.join(setup.extra_args)}"
    return Text(text)  # plain: CLI args may contain markup characters


class WorkflowProfilesScreen(ModalScreen[list[Selection] | None]):
    """Dismisses with one selection per agent action, or None when cancelled.

    Choices made here are never remembered for later launches."""

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
    WorkflowProfilesScreen .level-hint { color: $text-disabled; padding-left: 12; }
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
            stage_choices(cfg, stage.action, stage.initial.choice if stage.initial else None)
            if stage.action
            else []
            for stage in stages
        ]

    def compose(self) -> ComposeResult:
        caps = app_capabilities(self)
        default_provider = next(
            (s.initial.provider for s in self.stages if s.initial and s.initial.provider),
            caps_mod.default_provider(self.cfg, self.last, caps),
        )
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
                    "setup", [choice.label() for choice in options], id=f"stage-{index}"
                )
                initial = stage.initial or Selection(options[-1])
                choice = initial.choice if initial.choice in options else options[-1]
                # Set by position: a profile may share a label with "action settings".
                field.set_reactive(CycleField.index, options.index(choice))
                yield field
                provider = CycleField(
                    "provider",
                    [row.id for row in catalogue.ROWS],
                    initial.provider or default_provider,
                    labels=provider_labels(self.cfg, caps, self._tier(choice)),
                    id=f"stage-provider-{index}",
                )
                provider.display = choice.kind == "persona"
                yield provider
                level = CycleField(
                    "level",
                    LEVEL_OPTIONS,
                    labels=level_labels(self.cfg, caps),
                    id=f"stage-level-{index}",
                )
                yield level
                yield Static("", classes="level-hint", id=f"stage-level-hint-{index}")
                yield Static("", classes="stage-hint", id=f"stage-hint-{index}")
            with Horizontal(id="start-row"):
                yield Static(
                    KeyHints("tab/j/k stages · h/l change · enter start · esc cancel"),
                    classes="hint",
                )
                yield Button("start", variant="primary", compact=True, id="start")

    def on_mount(self) -> None:
        for index, stage in enumerate(self.stages):
            if stage.action is not None:
                self._reset_level(index)
        if self.stages and self.query(f"#stage-{self._first_agent()}"):
            self.query_one(f"#stage-{self._first_agent()}", CycleField).focus()

    def capabilities_changed(self) -> None:
        for index, stage in enumerate(self.stages):
            if stage.action is not None:
                self._update(index)

    def _first_agent(self) -> int:
        return next((i for i, stage in enumerate(self.stages) if stage.action), 0)

    def _tier(self, choice: StepChoice) -> str:
        persona = self.cfg.personas.get(choice.persona) if choice.kind == "persona" else None
        return persona.tier if persona else ""

    def _choice(self, index: int) -> StepChoice:
        return self.options[index][self.query_one(f"#stage-{index}", CycleField).index]

    def _selection(self, index: int, *, level: bool = True) -> Selection:
        choice = self._choice(index)
        provider = self.query_one(f"#stage-provider-{index}", CycleField).value
        field = self.query_one(f"#stage-level-{index}", CycleField)
        chosen = int(field.value) if level and field.display else None
        return Selection(choice, provider if choice.kind == "persona" else "", chosen)

    def _resolved(self, index: int) -> ResolvedSetup:
        action = self.stages[index].action
        assert action is not None
        return resolve_selection(self.cfg, self.last, self._selection(index), action)

    def choices(self) -> list[Selection]:
        return [self._selection(index) for index, stage in enumerate(self.stages) if stage.action]

    def _reset_level(self, index: int) -> None:
        """Start at the action's level, else the persona's, else the project default."""
        action = self.stages[index].action
        assert action is not None
        selection = self._selection(index, level=False)
        start = resolve_selection(self.cfg, self.last, selection, action).start_level
        field = self.query_one(f"#stage-level-{index}", CycleField)
        with self.prevent(CycleField.Changed):
            field.display = start is not None
            if start is not None:
                field.value = str(start)
        self._update(index)

    def _update(self, index: int) -> None:
        action = self.stages[index].action
        assert action is not None
        choice = self._choice(index)
        provider = self.query_one(f"#stage-provider-{index}", CycleField)
        provider.display = choice.kind == "persona"
        provider.labels = provider_labels(self.cfg, app_capabilities(self), self._tier(choice))
        provider.refresh()
        selection = self._selection(index)
        level_field = self.query_one(f"#stage-level-{index}", CycleField)
        level_hint = self.query_one(f"#stage-level-hint-{index}", Static)
        level_hint.display = level_field.display
        level_field.labels = level_labels(self.cfg, app_capabilities(self))
        level_field.refresh()
        show_level(level_field, self.cfg, selection.level)
        setup = resolve_selection(self.cfg, self.last, selection, action)
        if level_field.display:
            text = describe_level(self.cfg, app_capabilities(self), selection.level, setup.agent)
            level_hint.update(Text(text))
        self.query_one(f"#stage-hint-{index}", Static).update(
            describe(self.cfg, self.last, action, selection)
        )

    @on(CycleField.Changed)
    def _changed(self, event: CycleField.Changed) -> None:
        field_id = event.field.id or ""
        prefix, _, number = field_id.rpartition("-")
        if not number.isdigit():
            return
        index = int(number)
        if prefix == "stage":
            self._reset_level(index)
        elif prefix in ("stage-provider", "stage-level"):
            self._update(index)

    @on(Button.Pressed, "#start")
    def action_start(self) -> None:
        for index, stage in enumerate(self.stages):
            if stage.action is None:
                continue
            if problem := self._resolved(index).problem:
                self.notify(f"{stage.name}: {problem}", severity="warning")
                field = "stage-provider" if self._choice(index).kind == "persona" else "stage"
                self.query_one(f"#{field}-{index}", CycleField).focus()
                return
            level = self._selection(index).level
            if problems := caps_mod.level_problems(app_capabilities(self), self.cfg, level):
                self.notify(f"{stage.name}: {'; '.join(problems)}", severity="warning")
                self.query_one(f"#stage-level-{index}", CycleField).focus()
                return
        self.dismiss(self.choices())

    def action_cancel(self) -> None:
        self.dismiss(None)
