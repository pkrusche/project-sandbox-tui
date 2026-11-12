"""Agent settings shared by launch forms and the saved-profile editor."""

from __future__ import annotations

import shlex

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Input, Static

from project_sandbox_tui import models as models_mod
from project_sandbox_tui.config import (
    AGENTS,
    EFFORTS,
    FAST_AGENTS,
    AgentProfile,
    Config,
    ConfigError,
    LastUsed,
    LaunchDefaults,
    resolve_fast,
)
from project_sandbox_tui.config import (
    resolve_model as _resolve_model,
)
from project_sandbox_tui.widgets import CycleField

CUSTOM_MODEL = "custom (enter model name)"


class AgentControls(Vertical):
    DEFAULT_CSS = """
    AgentControls { height: auto; }
    AgentControls Input, AgentControls Input:focus {
        border: none; height: 1; padding: 0 1; margin: 0;
    }
    AgentControls Input { background: $panel; }
    AgentControls Input:focus { background: $boost; }
    AgentControls .field { height: 1; }
    AgentControls .field-label { width: 10; color: $text-muted; padding: 0 0 0 1; }
    AgentControls .field Input { width: 1fr; padding: 0; }
    AgentControls .prompt-hint { color: $text-disabled; height: auto; padding: 0 1; }
    """

    class Changed(Message):
        pass

    def __init__(
        self,
        cfg: Config,
        last: LastUsed,
        defaults: LaunchDefaults,
        *,
        extra_args: list[str] | None = None,
    ) -> None:
        super().__init__(id="agent-controls")
        self.cfg, self.last, self.defaults = cfg, last, defaults
        self.extra_args = list(extra_args or [])
        self._fast_by_agent = {agent: resolve_fast(cfg, last, agent) for agent in FAST_AGENTS}
        self._fast_by_agent[defaults.agent] = defaults.fast
        self.model_store = models_mod.load_store()

    def compose(self) -> ComposeResult:
        yield CycleField("agent", AGENTS, self.defaults.agent, id="agent")
        yield CycleField(
            "model",
            self._model_options(self.defaults.agent, self.defaults.model),
            self.defaults.model,
            empty_label="agent default",
            id="model",
        )
        with Horizontal(classes="field", id="custom-model-row") as custom_model_row:
            custom_model_row.display = False
            yield Static("name", classes="field-label")
            yield Input(
                value=self.defaults.model,
                placeholder="model name (blank uses agent default)",
                select_on_focus=False,
                id="custom-model",
            )
        model_hint = models_mod.store_hint(self.defaults.agent, self.model_store)
        hint_widget = Static(model_hint, id="model-hint", classes="prompt-hint")
        hint_widget.display = bool(model_hint)
        yield hint_widget
        yield CycleField("effort", ("", *EFFORTS), self.defaults.effort, id="effort")
        fast = CycleField("fast", ("off", "on"), "on" if self.defaults.fast else "off", id="fast")
        fast.display = self.defaults.agent in FAST_AGENTS
        yield fast
        with Horizontal(classes="field"):
            yield Static("CLI args", classes="field-label")
            yield Input(
                value=shlex.join(self.extra_args),
                placeholder="extra project-sandbox switches (shell quoting supported)",
                select_on_focus=False,
                id="extra-args",
            )

    def current_profile(self) -> AgentProfile:
        agent = self.query_one("#agent", CycleField).value
        model = self.query_one("#model", CycleField).value
        if model == CUSTOM_MODEL:
            model = self.query_one("#custom-model", Input).value.strip()
        try:
            extra_args = shlex.split(self.query_one("#extra-args", Input).value)
        except ValueError as exc:
            raise ConfigError(f"CLI args: {exc}") from exc
        return AgentProfile(
            agent=agent,
            model=model if agent != "bash" else "",
            effort=self.query_one("#effort", CycleField).value,
            fast=agent in FAST_AGENTS and self.query_one("#fast", CycleField).value == "on",
            extra_args=extra_args,
        )

    def set_profile(self, profile: AgentProfile) -> None:
        # Setting agent/model posts asynchronous events. Suppress these so the
        # agent handler cannot replace the loaded model with a remembered one.
        with self.prevent(CycleField.Changed, Input.Changed):
            self.query_one("#agent", CycleField).value = profile.agent
            model = self.query_one("#model", CycleField)
            current = profile.model if profile.agent != "bash" else ""
            model.options = self._model_options(profile.agent, current)
            model.value = current
            model.refresh()
            self.query_one("#custom-model", Input).value = profile.model
            self.query_one("#effort", CycleField).value = profile.effort
            fast = self.query_one("#fast", CycleField)
            fast.display = profile.agent in FAST_AGENTS
            fast.value = "on" if profile.fast else "off"
            self._fast_by_agent[profile.agent] = profile.fast
            self.query_one("#extra-args", Input).value = shlex.join(profile.extra_args)
        self._update_custom_model()
        self._update_model_hint(profile.agent)
        self.post_message(self.Changed())

    def _update_model_hint(self, agent: str) -> None:
        hint = self.query_one("#model-hint", Static)
        text = models_mod.store_hint(agent, self.model_store)
        hint.update(text)
        hint.display = bool(text)

    def _model_options(self, agent: str, current: str) -> list[str]:
        if agent == "bash":
            return [""]
        models = models_mod.suggestions(agent, self.cfg.models.get(agent), self.model_store)
        return list(dict.fromkeys(["", *([current] if current else []), *models, CUSTOM_MODEL]))

    @on(CycleField.Changed, "#agent")
    def _agent_changed(self, event: CycleField.Changed) -> None:
        fast = self.query_one("#fast", CycleField)
        fast.display = event.value in FAST_AGENTS
        fast.value = "on" if self._fast_by_agent.get(event.value, False) else "off"
        model = self.query_one("#model", CycleField)
        current = "" if event.value == "bash" else _resolve_model(self.cfg, self.last, event.value)
        model.options = self._model_options(event.value, current)
        model.value = current
        model.refresh()
        self.query_one("#custom-model", Input).value = current
        self._update_custom_model()
        self._update_model_hint(event.value)
        self.post_message(self.Changed())

    def _update_custom_model(self) -> None:
        self.query_one("#custom-model-row").display = (
            self.query_one("#agent", CycleField).value != "bash"
            and self.query_one("#model", CycleField).value == CUSTOM_MODEL
        )

    @on(CycleField.Changed, "#model")
    def _model_changed(self) -> None:
        self._update_custom_model()
        self._changed()

    @on(CycleField.Changed, "#effort")
    @on(Input.Changed)
    def _changed(self) -> None:
        self.post_message(self.Changed())

    @on(CycleField.Changed, "#fast")
    def _fast_changed(self) -> None:
        agent = self.query_one("#agent", CycleField).value
        if agent in FAST_AGENTS:
            self._fast_by_agent[agent] = self.query_one("#fast", CycleField).value == "on"
        self._changed()
