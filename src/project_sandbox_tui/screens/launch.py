"""Launch form: saved profiles or custom settings, with unsupervised prompts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import Button, Input, Static, TextArea

from project_sandbox_tui import octopus as octopus_mod
from project_sandbox_tui.config import (
    AgentProfile,
    Config,
    ConfigError,
    LastUsed,
    LaunchDefaults,
)
from project_sandbox_tui.history import PromptHistory
from project_sandbox_tui.sandbox import LaunchSpec, Mode
from project_sandbox_tui.screens.agent import CUSTOM_MODEL as CUSTOM_MODEL
from project_sandbox_tui.screens.agent import AgentControls
from project_sandbox_tui.screens.history import HistorySearchScreen
from project_sandbox_tui.vcs import session_target_error, validate_branch_name
from project_sandbox_tui.widgets import CycleField, KeyHints, PromptArea, modifier_key


@dataclass
class OctopusSetup:
    """Extra launch-form state for integrating several branches (see octopus.py)."""

    inputs: list[str]  # merge / stacking order
    existing: set[str]  # branch names, for validating the result name
    default_base: str
    templates: dict[str, str]  # operation -> prompt template


class LaunchScreen(ModalScreen[LaunchSpec | None]):
    DEFAULT_CSS = """
    LaunchScreen { align: center middle; }
    /* A stable gutter: a scrollbar that appeared in a short terminal would narrow
       the form, wrap its hints and so never go away again once it grew back. */
    LaunchScreen > Vertical {
        width: 90; max-width: 100%; height: auto; max-height: 100%; overflow-y: auto;
        scrollbar-gutter: stable; scrollbar-size-vertical: 1;
        background: $panel; padding: 1 1 1 2; border: none;
    }
    LaunchScreen .title { text-style: bold; margin-bottom: 1; }
    LaunchScreen Input, LaunchScreen Input:focus {
        border: none; height: 1; padding: 0 1; margin: 0;
    }
    LaunchScreen Input { background: $panel; }
    LaunchScreen Input:focus { background: $boost; }
    LaunchScreen .row-label { color: $text-muted; height: 1; padding: 0 1; margin-top: 1; }
    LaunchScreen .field { height: 1; }
    LaunchScreen .field-label { width: 10; color: $text-muted; padding: 0 0 0 1; }
    LaunchScreen .field Input { width: 1fr; padding: 0; }
    LaunchScreen .field-value { width: 1fr; height: 1; }
    LaunchScreen TextArea { height: 6; border: none; margin: 0; }
    LaunchScreen TextArea:focus { background: $boost; border: none; }
    LaunchScreen PromptArea > .text-area--suggestion { color: $text-disabled; text-style: italic; }
    LaunchScreen .inputs { color: $text-muted; padding: 0 1; margin-bottom: 1; }
    LaunchScreen .prompt-hint { color: $text-disabled; height: auto; padding: 0 1; }
    LaunchScreen #command-scroll {
        height: 3; margin-top: 1; scrollbar-gutter: stable; scrollbar-size-vertical: 1;
    }
    LaunchScreen .preview { color: $text-muted; height: auto; }
    LaunchScreen .hint { color: $text-disabled; margin-top: 1; }
    LaunchScreen #launch-row { height: auto; margin-top: 1; }
    LaunchScreen #launch-row .hint { width: 1fr; margin-top: 0; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel", show=False),
        Binding("ctrl+enter", "launch", "launch", show=False, priority=True),
        # ctrl+enter is indistinguishable from enter in many terminals (iOS, Blink)
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
        *,
        branch_label: str | None = None,
        prompt_history: PromptHistory | None = None,
        opens_in: str = "",
        branches: list[str] | None = None,
        octopus: OctopusSetup | None = None,
        draft: str = "",
    ) -> None:
        """``draft``: a prompt typed earlier but not launched; shown instead of ``prompt_text``."""
        super().__init__()
        self.branch = branch
        self.mode = mode
        self.cfg = cfg
        self.last = last
        self.defaults = defaults
        self.initial_profile = (
            next(iter(cfg.profiles), "") if last.profile is None else last.profile
        )
        if self.initial_profile not in cfg.profiles:
            self.initial_profile = ""
        self.preview_fn = preview
        self.initial_prompt = draft or prompt_text
        self._baseline_prompt = prompt_text  # what the prompt shows without edits
        # On dismissal: the edited prompt to keep for next time, "" for none (background only).
        self.draft: str | None = None
        self.branch_label = branch_label or branch
        self.prompt_history = prompt_history
        self.opens_in = opens_in
        self.branches = branches or [branch]
        self.octopus = octopus
        self._auto_prompt = ""  # last template-rendered prompt (octopus only)
        self._auto_result = ""  # last generated result name (octopus only)
        if octopus is not None:
            self.branch = octopus_mod.default_result_name("merge", octopus.inputs)
            self._auto_result = self.branch
            self._auto_prompt = self._render_template("merge", self.branch, octopus.default_base)
            self.initial_prompt = draft or self._auto_prompt

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title(), classes="title", id="title")
            if self.octopus is not None:
                yield Static(
                    Text("inputs   " + " → ".join(self.octopus.inputs), overflow="ellipsis"),
                    classes="inputs",
                )
                yield CycleField("action", octopus_mod.OPERATIONS, "merge", id="operation")
                with Horizontal(classes="field"):
                    yield Static("bookmark", classes="field-label")
                    yield Input(value=self.branch, select_on_focus=False, id="result")
                with Horizontal(classes="field"):
                    yield Static("base", classes="field-label")
                    yield Input(
                        value=self.octopus.default_base,
                        placeholder="rebase onto (rebase only)",
                        select_on_focus=False,
                        id="base",
                    )
            else:
                with Horizontal(classes="field"):
                    yield Static("bookmark", classes="field-label")
                    yield Input(
                        value=self.branch,
                        placeholder="select a bookmark, or enter a new name",
                        suggester=SuggestFromList(
                            [b for b in self.branches if not self._target_error(b)],
                            case_sensitive=True,
                        ),
                        select_on_focus=False,
                        id="branch",
                    )
                with Horizontal(classes="field"):
                    yield Static("base", classes="field-label")
                    yield Static(
                        Text(self.branch_label, overflow="ellipsis"),
                        classes="field-value",
                        id="start-branch",
                    )
                yield Static("", classes="prompt-hint", id="branch-hint")
            yield CycleField(
                "profile",
                [*self.cfg.profiles, "custom"],
                self.initial_profile or "custom",
                id="profile",
            )
            yield Static("", classes="prompt-hint", id="profile-hint")
            controls = AgentControls(
                self.cfg, self.last, self.defaults, extra_args=self.last.extra_args
            )
            controls.display = not self.initial_profile
            yield controls
            if self.mode == "background":
                with Horizontal(classes="field"):
                    yield Static("timeout", classes="field-label")
                    yield Input(
                        value=str(self.defaults.timeout or ""),
                        restrict=r"[0-9]*",
                        placeholder="none (seconds)",
                        select_on_focus=False,
                        id="timeout",
                    )
                yield Static("prompt", classes="row-label")
                yield PromptArea(
                    self.initial_prompt, prompt_history=self.prompt_history, id="prompt"
                )
                yield Static("", id="prompt-hint", classes="prompt-hint")
            with VerticalScroll(id="command-scroll"):
                yield Static("", classes="preview", id="preview")
            keys = "tab/j/k fields · h/l change · enter launch · esc cancel"
            if self.mode == "background":
                keys = "tab fields · h/l change · ctrl+s launch · esc cancel"
            self.default_hint = keys
            if self.mode == "background":
                with Horizontal(id="launch-row"):
                    yield Static(KeyHints(keys), classes="hint", id="launch-hint")
                    yield Button("launch", variant="primary", compact=True, id="launch")
            else:
                yield Static(KeyHints(keys), classes="hint", id="launch-hint")

    def on_key(self, event: events.Key) -> None:
        modifier = modifier_key(event.key)
        hint = self.default_hint
        if modifier:
            if modifier == "ctrl":
                hint = "ctrl+s / ctrl+enter launch"
                if isinstance(self.focused, PromptArea):
                    hint += " · ctrl+p/n history · ctrl+r search · ctrl+f complete · ctrl+l clear"
            elif modifier == "shift":
                hint = "shift+tab previous field"
            else:
                hint = f"{modifier}: no form shortcuts"
            event.stop()
        self.query_one("#launch-hint", Static).update(KeyHints(hint))

    def _title(self) -> Text:
        what = "interactive session" if self.mode == "interactive" else "unsupervised run"
        title = Text(what)
        if self.octopus is not None:
            op = self._value("#operation", "merge") or "merge"
            title.append(f" · {op} {len(self.octopus.inputs)} bookmarks into ")
            title.append(self._value("#result", self.branch) or "…", style="bold")
        if self.opens_in:
            title.append(f"  · {self.opens_in}", style="dim")
        return title

    def _value(self, selector: str, default: str) -> str:
        """Current value of an Input / CycleField, or ``default`` before mount."""
        try:
            widget = self.query_one(selector)
        except Exception:
            return default
        if isinstance(widget, (Input, CycleField)):
            return widget.value.strip()
        return default

    def _render_template(self, operation: str, result: str, base: str) -> str:
        assert self.octopus is not None
        plan = octopus_mod.OctopusPlan(
            "rebase" if operation == "rebase" else "merge",
            self.octopus.inputs,
            result,
            base if operation == "rebase" else "",
        )
        name = self._selected_profile()
        agent = (
            self.cfg.profiles[name].agent if name else self._value("#agent", self.defaults.agent)
        )
        if agent == "bash":
            return octopus_mod.bash_prompt(plan, self.octopus.existing)
        return octopus_mod.render_prompt(self.octopus.templates[operation], plan)

    def _octopus_changed(self) -> None:
        """Keep title, default result name and (untouched) prompt in step."""
        if self.octopus is None:
            return
        op = self._value("#operation", "merge")
        result_input = self.query_one("#result", Input)
        if result_input.value == self._auto_result:  # not typed by the user
            fresh_name = octopus_mod.default_result_name(op, self.octopus.inputs)
            if fresh_name != result_input.value:
                self._auto_result = fresh_name
                result_input.value = fresh_name  # posts Changed, which lands back here
                return
        self.query_one("#title", Static).update(self._title())
        if self.mode != "background":
            return
        area = self.query_one("#prompt", PromptArea)
        if area.text == self._auto_prompt:  # the user has not edited it
            fresh = self._render_template(op, self._value("#result", ""), self._value("#base", ""))
            if fresh != area.text:
                self._auto_prompt = fresh
                area.replace_text(fresh)

    def on_mount(self) -> None:
        target = "#prompt" if self.mode == "background" else "#profile"
        self.query_one(target).focus()
        self._profile_changed()
        self._update_preview()
        self._update_prompt_hint()
        self._update_branch_hint()

    @on(Input.Changed, "#branch")
    def _update_branch_hint(self) -> None:
        """Say when the bookmark field names a bookmark that launching would create."""
        if self.octopus is not None:  # the result is always new; the title names it
            return
        name = self.query_one("#branch", Input).value.strip()
        hint = self.query_one("#branch-hint", Static)
        if error := self._target_error(name):
            hint.update(Text(error, "yellow"))
        elif name and name not in self.branches and not validate_branch_name(name):
            hint.update(Text(f"new bookmark {name}, created from {self.branch_label}", "yellow"))
        else:
            hint.update(KeyHints("existing name selects it · new name creates it from base"))

    def _update_prompt_hint(self) -> None:
        if self.mode != "background":
            return
        area = self.query_one("#prompt", PromptArea)
        count = len(self.prompt_history.entries) if self.prompt_history else 0
        if not count:
            hint = "no prompt history yet · ctrl+l clear"
        elif (pos := area.browse_position) is not None:
            hint = f"history {pos[0]}/{pos[1]} · ctrl+p older · ctrl+n newer · ctrl+r search"
        elif area.suggestion:
            hint = "→ or ctrl+f: complete from history · ctrl+p/n browse · ctrl+r search"
        else:
            hint = f"{count} prompts in history · ctrl+p/n browse · ctrl+r search · ctrl+l clear"
        self.query_one("#prompt-hint", Static).update(KeyHints(hint))

    # ------------------------------------------------------------ state

    def _selected_profile(self) -> str:
        name = self._value("#profile", self.initial_profile or "custom")
        return name if name in self.cfg.profiles else ""

    def _current_profile(self) -> AgentProfile:
        if name := self._selected_profile():
            return self.cfg.profiles[name]
        return self.query_one(AgentControls).current_profile()

    @on(CycleField.Changed, "#profile")
    def _profile_changed(self) -> None:
        name = self._selected_profile()
        self.query_one(AgentControls).display = not name
        hint = self.query_one("#profile-hint", Static)
        hint.display = bool(name)
        if name:
            profile = self.cfg.profiles[name]
            text = " / ".join(
                value
                for value in (
                    profile.agent,
                    profile.model,
                    profile.effort,
                    "fast" if profile.fast else "",
                )
                if value
            )
            hint.update(Text(text + " · edit with :config-profiles"))
        self._octopus_changed()
        self._update_preview()

    def current_spec(self) -> LaunchSpec:
        timeout = 0
        if self.mode == "background":
            raw = self.query_one("#timeout", Input).value.strip()
            timeout = int(raw) if raw.isdigit() else 0
        prompt = self.query_one("#prompt", TextArea).text if self.mode == "background" else ""
        profile = self._current_profile()
        operation, base = "", ""
        inputs: list[str] = []
        if self.octopus is not None:
            operation = self._value("#operation", "merge")
            inputs = list(self.octopus.inputs)
            branch = self._value("#result", self.branch)
            base = self._value("#base", "") if operation == "rebase" else ""
        else:
            branch = self.query_one("#branch", Input).value.strip()
        return LaunchSpec(
            branch=branch,
            agent=profile.agent,
            model=profile.model if profile.agent != "bash" else "",
            effort=profile.effort,
            mode=self.mode,
            prompt_text=prompt,
            timeout=timeout,
            operation=operation,
            inputs=inputs,
            base=base,
            fast=profile.fast,
            extra_args=list(profile.extra_args),
            profile=self._selected_profile(),
        )

    def _update_preview(self) -> None:
        try:
            text = self.preview_fn(self.current_spec())
        except Exception as exc:  # preview must never break the form
            text = f"! {exc}"
        self.query_one("#preview", Static).update(Text(text))

    # ------------------------------------------------------------ events

    @on(CycleField.Changed, "#operation")
    @on(Input.Changed, "#result")
    @on(Input.Changed, "#base")
    def _octopus_field_changed(self) -> None:
        self._octopus_changed()

    @on(AgentControls.Changed)
    @on(Input.Changed)
    @on(TextArea.Changed)
    def _changed(self) -> None:
        self._octopus_changed()
        self.query_one("#launch-hint", Static).update(KeyHints(self.default_hint))
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

    @on(Button.Pressed, "#launch")
    def _launch_pressed(self) -> None:
        self.action_launch()

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
        the prompt is the main field, it only moves on (ctrl+s launches)."""
        if self.mode == "interactive":
            self.action_launch()
        else:
            self.focus_next()

    def _target_error(self, name: str) -> str:
        names = self.octopus.existing if self.octopus is not None else self.branches
        return session_target_error(name, names, self.cfg.project.main_branch)

    def action_launch(self) -> None:
        target = self._value("#result" if self.octopus else "#branch", "")
        if error := self._target_error(target):
            self.notify(error, severity="warning")
            self.query_one("#result" if self.octopus else "#branch").focus()
            return
        if self.octopus is not None:
            operation = self._value("#operation", "merge")
            plan = octopus_mod.OctopusPlan(
                "rebase" if operation == "rebase" else "merge",
                list(self.octopus.inputs),
                self._value("#result", ""),
                self._value("#base", "") if operation == "rebase" else "",
            )
            try:
                octopus_mod.validate(plan, self.octopus.existing)
            except octopus_mod.OctopusError as exc:
                self.notify(str(exc), severity="warning")
                self.query_one("#base" if "base" in str(exc) else "#result").focus()
                return
        else:
            branch = self.query_one("#branch", Input).value.strip()
            if branch not in self.branches and (error := validate_branch_name(branch)):
                self.notify(error, severity="warning")
                self.query_one("#branch").focus()
                return
        try:
            spec = self.current_spec()
        except ConfigError as exc:
            self.notify(str(exc), severity="warning")
            self.query_one("#extra-args").focus()
            return
        if any(ch.isspace() for ch in spec.model):
            self.notify(f"invalid model name: {spec.model!r}", severity="warning")
            custom = self.query_one("#custom-model-row").display
            self.query_one("#custom-model" if custom else "#model").focus()
            return
        if spec.mode == "background" and not spec.prompt_text.strip():
            self.notify("an unsupervised run needs a prompt", severity="warning")
            self.query_one("#prompt").focus()
            return
        self._keep_draft()  # until the launch has started
        self.dismiss(spec)

    def action_cancel(self) -> None:
        self._keep_draft()
        self.dismiss(None)

    def _keep_draft(self) -> None:
        if self.mode != "background":
            return
        text = self.query_one("#prompt", PromptArea).text
        baseline = self._auto_prompt if self.octopus is not None else self._baseline_prompt
        self.draft = text if text.strip() and text != baseline else ""
