"""Launch form: agent / model / effort (+ prompt and timeout for unsupervised runs)."""

from __future__ import annotations

from collections.abc import Callable

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import Input, Static, TextArea

from project_sandbox_tui.config import AGENTS, EFFORTS, Config, LastUsed, LaunchDefaults
from project_sandbox_tui.config import resolve_model as _resolve_model
from project_sandbox_tui.sandbox import LaunchSpec, Mode
from project_sandbox_tui.widgets import CycleField


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
    LaunchScreen .preview { color: $text-muted; margin-top: 1; height: auto; }
    LaunchScreen .hint { color: $text-disabled; margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel", show=False),
        Binding("ctrl+s", "launch", "launch", show=False, priority=True),
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
    ) -> None:
        super().__init__()
        self.branch = branch
        self.mode = mode
        self.cfg = cfg
        self.last = last
        self.defaults = defaults
        self.preview_fn = preview
        self.initial_prompt = prompt_text

    def compose(self) -> ComposeResult:
        what = "interactive session" if self.mode == "interactive" else "unsupervised run"
        with Vertical():
            title = Text(f"{what} on ")
            title.append(self.branch, style="bold")
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
                yield TextArea(self.initial_prompt, id="prompt", soft_wrap=True)
            yield Static("", classes="preview", id="preview")
            keys = "tab/j/k fields · h/l change · enter launch · esc cancel"
            if self.mode == "background":
                keys = "tab fields · h/l change · ctrl+s launch · esc cancel"
            yield Static(keys, classes="hint")

    def on_mount(self) -> None:
        target = "#prompt" if self.mode == "background" else "#agent"
        self.query_one(target).focus()
        self._update_preview()

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
        self._update_preview()

    @on(Input.Submitted)
    def _submitted(self) -> None:
        self.action_launch()

    def key_enter(self) -> None:
        # Enter on a CycleField launches; Inputs handle their own Submitted.
        if isinstance(self.focused, CycleField):
            self.action_launch()

    def action_launch(self) -> None:
        spec = self.current_spec()
        if spec.mode == "background" and not spec.prompt_text.strip():
            self.notify("an unsupervised run needs a prompt", severity="warning")
            self.query_one("#prompt").focus()
            return
        self.dismiss(spec)

    def action_cancel(self) -> None:
        self.dismiss(None)
