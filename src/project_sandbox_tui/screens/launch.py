"""Launch form: agent / model / effort (+ prompt and timeout for unsupervised runs)."""

from __future__ import annotations

from collections.abc import Callable

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import Input, Static, TextArea

from project_sandbox_tui.config import AGENTS, EFFORTS, Config, LastUsed, LaunchDefaults
from project_sandbox_tui.config import resolve_model as _resolve_model
from project_sandbox_tui.history import PromptHistory
from project_sandbox_tui.sandbox import LaunchSpec, Mode
from project_sandbox_tui.screens.history import HistorySearchScreen
from project_sandbox_tui.widgets import CycleField, PromptArea, modifier_key


class LaunchScreen(ModalScreen[LaunchSpec | None]):
    DEFAULT_CSS = """
    LaunchScreen { align: center middle; }
    LaunchScreen > Vertical {
        width: 90; max-width: 100%; height: auto; max-height: 100%;
        background: $panel; padding: 1 2; border: none;
    }
    LaunchScreen .title { text-style: bold; margin-bottom: 1; }
    LaunchScreen Input { border: none; height: 1; padding: 0 1; margin: 0; }
    LaunchScreen Input { background: $panel; }
    LaunchScreen Input:focus { background: $boost; }
    LaunchScreen .row-label { color: $text-muted; height: 1; padding: 0 1; margin-top: 1; }
    LaunchScreen .field { height: 1; }
    LaunchScreen .field-label { width: 10; color: $text-muted; padding: 0 0 0 1; }
    LaunchScreen .field Input { width: 1fr; padding: 0; }
    LaunchScreen TextArea { height: 8; border: none; margin: 0; }
    LaunchScreen TextArea:focus { background: $boost; border: none; }
    LaunchScreen PromptArea > .text-area--suggestion { color: $text-disabled; text-style: italic; }
    LaunchScreen .prompt-hint { color: $text-disabled; height: 1; padding: 0 1; }
    LaunchScreen .preview { color: $text-muted; margin-top: 1; height: auto; }
    LaunchScreen .hint { color: $text-disabled; margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel", show=False),
        Binding("ctrl+enter", "launch", "launch", show=False, priority=True),
    ]

    def __init__(
        self,
        branch: str,
        mode: Mode,
        cfg: Config,
        last: LastUsed,
        defaults: LaunchDefaults,
        preview: Callable[[LaunchSpec], str],
        prompt_text: str = "",
        *,
        main_checkout: bool = False,
        branch_label: str | None = None,
        prompt_history: PromptHistory | None = None,
        opens_in: str = "",
    ) -> None:
        super().__init__()
        self.branch = branch
        self.mode = mode
        self.cfg = cfg
        self.last = last
        self.defaults = defaults
        self.preview_fn = preview
        self.initial_prompt = prompt_text
        self.main_checkout = main_checkout
        self.branch_label = branch_label or branch
        self.prompt_history = prompt_history
        self.opens_in = opens_in

    def compose(self) -> ComposeResult:
        what = "interactive session" if self.mode == "interactive" else "unsupervised run"
        with Vertical():
            title = Text(f"{what} on ")
            title.append(self.branch_label, style="bold")
            if self.main_checkout and self.branch_label == self.branch:
                title.append("  (in the main checkout)", style="dim")
            if self.opens_in:
                title.append(f"  · {self.opens_in}", style="dim")
            yield Static(title, classes="title")
            yield CycleField("agent", AGENTS, self.defaults.agent, id="agent")
            with Horizontal(classes="field"):
                yield Static("model", classes="field-label")
                yield Input(
                    value=self.defaults.model,
                    placeholder="agent default",
                    suggester=self._suggester(self.defaults.agent),
                    id="model",
                )
            yield CycleField("effort", ("", *EFFORTS), self.defaults.effort, id="effort")
            if self.mode == "background":
                with Horizontal(classes="field"):
                    yield Static("timeout", classes="field-label")
                    yield Input(
                        value=str(self.defaults.timeout or ""),
                        restrict=r"[0-9]*",
                        placeholder="none (seconds)",
                        id="timeout",
                    )
                yield Static("prompt", classes="row-label")
                yield PromptArea(
                    self.initial_prompt, prompt_history=self.prompt_history, id="prompt"
                )
                yield Static("", id="prompt-hint", classes="prompt-hint")
            yield Static("", classes="preview", id="preview")
            keys = "tab/j/k fields · h/l change · enter launch · esc cancel"
            if self.mode == "background":
                keys = "tab fields · h/l change · ctrl+enter launch · esc cancel"
            self.default_hint = keys
            yield Static(keys, classes="hint", id="launch-hint")

    def on_key(self, event: events.Key) -> None:
        modifier = modifier_key(event.key)
        hint = self.default_hint
        if modifier:
            if modifier == "ctrl":
                hint = "ctrl+enter launch"
                if isinstance(self.focused, PromptArea):
                    hint += " · ctrl+p/n history · ctrl+r search · ctrl+f complete"
            elif modifier == "shift":
                hint = "shift+tab previous field"
            else:
                hint = f"{modifier}: no form shortcuts"
            event.stop()
        self.query_one("#launch-hint", Static).update(hint)

    def on_mount(self) -> None:
        target = "#prompt" if self.mode == "background" else "#agent"
        self.query_one(target).focus()
        self._update_preview()
        self._update_prompt_hint()

    def _update_prompt_hint(self) -> None:
        if self.mode != "background":
            return
        area = self.query_one("#prompt", PromptArea)
        count = len(self.prompt_history.entries) if self.prompt_history else 0
        if not count:
            hint = "no prompt history yet"
        elif (pos := area.browse_position) is not None:
            hint = f"history {pos[0]}/{pos[1]} · ctrl+p older · ctrl+n newer · ctrl+r search"
        elif area.suggestion:
            hint = "→ or ctrl+f: complete from history · ctrl+p/n browse · ctrl+r search"
        else:
            hint = f"{count} prompts in history · ctrl+p/n browse · ctrl+r search"
        self.query_one("#prompt-hint", Static).update(hint)

    def _suggester(self, agent: str) -> SuggestFromList | None:
        models = self.cfg.models.get(agent) or []
        return SuggestFromList(models, case_sensitive=False) if models else None

    # ------------------------------------------------------------ state

    def current_spec(self) -> LaunchSpec:
        timeout = 0
        if self.mode == "background":
            raw = self.query_one("#timeout", Input).value.strip()
            timeout = int(raw) if raw.isdigit() else 0
        prompt = self.query_one("#prompt", TextArea).text if self.mode == "background" else ""
        agent = self.query_one("#agent", CycleField).value
        return LaunchSpec(
            branch=self.branch,
            agent=agent,
            model=self.query_one("#model", Input).value.strip(),
            effort=self.query_one("#effort", CycleField).value,
            mode=self.mode,
            prompt_text=prompt,
            timeout=timeout,
            main_checkout=self.main_checkout,
        )

    def _update_preview(self) -> None:
        try:
            text = self.preview_fn(self.current_spec())
        except Exception as exc:  # preview must never break the form
            text = f"! {exc}"
        self.query_one("#preview", Static).update(Text(text))

    # ------------------------------------------------------------ events

    @on(CycleField.Changed, "#agent")
    def _agent_changed(self, event: CycleField.Changed) -> None:
        model = self.query_one("#model", Input)
        model.suggester = self._suggester(event.value)
        model.value = _resolve_model(self.cfg, self.last, event.value)
        self._update_preview()

    @on(CycleField.Changed, "#effort")
    @on(Input.Changed)
    @on(TextArea.Changed)
    def _changed(self) -> None:
        self.query_one("#launch-hint", Static).update(self.default_hint)
        self._update_preview()
        # after PromptArea has processed the same Changed (browse state, suggestion)
        self.call_after_refresh(self._update_prompt_hint)

    @on(PromptArea.SearchRequested)
    def _search_history(self, event: PromptArea.SearchRequested) -> None:
        if self.prompt_history is None:
            return
        area = event.area

        def chosen(prompt: str | None) -> None:
            if prompt is not None:
                area.replace_text(prompt)
            area.focus()

        self.app.push_screen(HistorySearchScreen(self.prompt_history, area.text), chosen)

    @on(Input.Submitted)
    def _submitted(self) -> None:
        self._enter()

    def key_enter(self, event: events.Key) -> None:
        # Inputs report Enter as Submitted; this covers the CycleFields.
        if isinstance(self.focused, CycleField):
            # Consume the key: left to bubble, it reaches the App after the form
            # is popped and fires the branch list's Enter binding (open log).
            event.stop()
            event.prevent_default()
            self._enter()

    def _enter(self) -> None:
        """Enter launches an interactive session; in the unsupervised form, where
        the prompt is the main field, it only moves on (ctrl+enter launches)."""
        if self.mode == "interactive":
            self.action_launch()
        else:
            self.focus_next()

    def action_launch(self) -> None:
        spec = self.current_spec()
        if spec.mode == "background" and not spec.prompt_text.strip():
            self.notify("an unsupervised run needs a prompt", severity="warning")
            self.query_one("#prompt").focus()
            return
        self.dismiss(spec)

    def action_cancel(self) -> None:
        self.dismiss(None)
