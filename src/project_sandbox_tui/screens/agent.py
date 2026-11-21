"""Agent settings shared by launch forms and the saved-profile editor."""

from __future__ import annotations

import shlex

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Input, Static

from project_sandbox_tui import capabilities as caps_mod
from project_sandbox_tui import models as models_mod
from project_sandbox_tui.capabilities import Capabilities
from project_sandbox_tui.config import (
    EFFORTS,
    FAST_AGENTS,
    GATEWAY_AGENTS,
    AgentProfile,
    Config,
    ConfigError,
    LastUsed,
    LaunchDefaults,
    resolve_fast,
    resolve_gateway,
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
        caps: Capabilities | None = None,
    ) -> None:
        super().__init__(id="agent-controls")
        self.cfg, self.last, self.defaults = cfg, last, defaults
        self.extra_args = list(extra_args or [])
        self._fast_by_agent = {agent: resolve_fast(cfg, last, agent) for agent in FAST_AGENTS}
        self._fast_by_agent[defaults.agent] = defaults.fast
        self._gateway_by_agent = {
            agent: resolve_gateway(cfg, last, agent) for agent in GATEWAY_AGENTS
        }
        if defaults.agent in GATEWAY_AGENTS:
            self._gateway_by_agent[defaults.agent] = defaults.gateway
        self._caps = caps
        self.model_store = models_mod.load_store()

    @property
    def caps(self) -> Capabilities:
        """The given snapshot, else the app's (kept current by its refresh worker)."""
        if self._caps is not None:
            return self._caps
        try:
            found = getattr(self.app, "capabilities", None)
        except Exception:  # not mounted yet
            found = None
        return found if isinstance(found, Capabilities) else caps_mod.fallback()

    @property
    def gateway_configured(self) -> bool:
        return bool(self.cfg.network.agent_proxy)

    def _uses_gateway(self, agent: str) -> bool:
        return (
            self.gateway_configured
            and agent in GATEWAY_AGENTS
            and self._gateway_by_agent.get(agent, False)
        )

    def compose(self) -> ComposeResult:
        agent = self.defaults.agent
        yield CycleField("agent", self._agent_options(agent), agent, id="agent")
        status = Static(self.agent_status(agent), id="agent-status", classes="prompt-hint")
        status.display = bool(self.agent_status(agent))
        yield status
        gateway = CycleField(
            "gateway", ("off", "on"), "on" if self._uses_gateway(agent) else "off", id="gateway"
        )
        gateway.display = self.gateway_configured and agent in GATEWAY_AGENTS
        yield gateway
        yield CycleField(
            "model",
            self._model_options(agent, self.defaults.model),
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
        effort = CycleField("effort", ("", *EFFORTS), self.defaults.effort, id="effort")
        effort.display = agent != "bash"
        yield effort
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
        gateway = self._uses_gateway(agent)
        return AgentProfile(
            agent=agent,
            model=model if agent != "bash" or gateway else "",
            effort=self.query_one("#effort", CycleField).value if agent != "bash" else "",
            fast=agent in FAST_AGENTS and self.query_one("#fast", CycleField).value == "on",
            extra_args=extra_args,
            gateway=gateway,
        )

    def set_profile(self, profile: AgentProfile) -> None:
        # Setting agent/model posts asynchronous events. Suppress these so the
        # agent handler cannot replace the loaded model with a remembered one.
        with self.prevent(CycleField.Changed, Input.Changed):
            agent_field = self.query_one("#agent", CycleField)
            agent_field.options = self._agent_options(profile.agent)
            agent_field.value = profile.agent
            agent_field.refresh()
            if profile.agent in GATEWAY_AGENTS:
                self._gateway_by_agent[profile.agent] = profile.gateway
            self._show_gateway(profile.agent)
            model = self.query_one("#model", CycleField)
            keep = profile.agent != "bash" or self._uses_gateway(profile.agent)
            current = profile.model if keep else ""
            model.options = self._model_options(profile.agent, current)
            model.value = current
            model.refresh()
            self.query_one("#custom-model", Input).value = profile.model
            effort = self.query_one("#effort", CycleField)
            effort.value = profile.effort
            effort.display = profile.agent != "bash"
            fast = self.query_one("#fast", CycleField)
            fast.display = profile.agent in FAST_AGENTS
            fast.value = "on" if profile.fast else "off"
            self._fast_by_agent[profile.agent] = profile.fast
            self.query_one("#extra-args", Input).value = shlex.join(profile.extra_args)
        self._update_custom_model()
        self._update_model_hint(profile.agent)
        self._update_status(profile.agent)
        self.post_message(self.Changed())

    def set_capabilities(self, caps: Capabilities) -> None:
        """Adopt a refreshed snapshot, keeping the current choices."""
        self._caps = caps
        agent_field = self.query_one("#agent", CycleField)
        current = agent_field.value
        with self.prevent(CycleField.Changed):
            agent_field.options = self._agent_options(current)
            agent_field.value = current
        agent_field.refresh()
        self._update_status(current)

    def _agent_options(self, current: str) -> list[str]:
        """The launcher's agents; a configured or remembered one stays visible."""
        return list(dict.fromkeys([*self.caps.agents, *([current] if current else [])]))

    def agent_status(self, agent: str) -> str:
        caps = self.caps
        if caps.discovered and agent not in caps.agents:
            return f"{agent}: not supported by {caps_mod.launcher_name(caps)}"
        result = caps_mod.agent_sources(agent, caps, gateway_configured=self.gateway_configured)
        if result.available is False:
            return f"{agent}: unavailable — {result.reason}"
        only_gateway = result.available and result.sources == (caps_mod.AGENT_GATEWAY,)
        if only_gateway and not self._uses_gateway(agent):
            return f"{agent}: available only through the agent gateway"
        return ""

    def _update_status(self, agent: str) -> None:
        status = self.query_one("#agent-status", Static)
        text = self.agent_status(agent)
        status.update(text)
        status.display = bool(text)

    def _show_gateway(self, agent: str) -> None:
        gateway = self.query_one("#gateway", CycleField)
        gateway.display = self.gateway_configured and agent in GATEWAY_AGENTS
        with self.prevent(CycleField.Changed):
            gateway.value = "on" if self._uses_gateway(agent) else "off"

    def _update_model_hint(self, agent: str) -> None:
        hint = self.query_one("#model-hint", Static)
        text = models_mod.store_hint(agent, self.model_store)
        hint.update(text)
        hint.display = bool(text)

    def _model_options(self, agent: str, current: str) -> list[str]:
        if agent == "bash" and not self._uses_gateway(agent):
            return [""]
        models = models_mod.suggestions(agent, self.cfg.models.get(agent), self.model_store)
        return list(dict.fromkeys(["", *([current] if current else []), *models, CUSTOM_MODEL]))

    @on(CycleField.Changed, "#agent")
    def _agent_changed(self, event: CycleField.Changed) -> None:
        fast = self.query_one("#fast", CycleField)
        fast.display = event.value in FAST_AGENTS
        fast.value = "on" if self._fast_by_agent.get(event.value, False) else "off"
        self.query_one("#effort", CycleField).display = event.value != "bash"
        self._show_gateway(event.value)
        self._reset_model(event.value)
        self._update_status(event.value)
        self.post_message(self.Changed())

    def _reset_model(self, agent: str) -> None:
        model = self.query_one("#model", CycleField)
        plain_bash = agent == "bash" and not self._uses_gateway(agent)
        current = "" if plain_bash else _resolve_model(self.cfg, self.last, agent)
        model.options = self._model_options(agent, current)
        model.value = current
        model.refresh()
        self.query_one("#custom-model", Input).value = current
        self._update_custom_model()
        self._update_model_hint(agent)

    @on(CycleField.Changed, "#gateway")
    def _gateway_changed(self) -> None:
        agent = self.query_one("#agent", CycleField).value
        if agent in GATEWAY_AGENTS:
            self._gateway_by_agent[agent] = self.query_one("#gateway", CycleField).value == "on"
        if agent == "bash":  # bash needs a model only through the gateway
            self._reset_model(agent)
        self._update_status(agent)
        self._changed()

    def _update_custom_model(self) -> None:
        agent = self.query_one("#agent", CycleField).value
        self.query_one("#custom-model-row").display = (
            agent != "bash" or self._uses_gateway(agent)
        ) and self.query_one("#model", CycleField).value == CUSTOM_MODEL

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
