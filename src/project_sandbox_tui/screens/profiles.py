"""Manage saved agent profiles."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from project_sandbox_tui.config import (
    AgentProfile,
    Config,
    ConfigError,
    ConfigScope,
    LastUsed,
    resolve_launch_defaults,
    validate_profile_name,
)
from project_sandbox_tui.screens.agent import AgentControls
from project_sandbox_tui.screens.dialogs import ConfirmScreen
from project_sandbox_tui.widgets import CycleField, KeyHints

ProfileEdit = tuple[Literal["saved", "deleted"], ConfigScope]


class ProfilesScreen(ModalScreen[ProfileEdit]):
    DEFAULT_CSS = """
    ProfilesScreen { align: center middle; }
    ProfilesScreen > Vertical {
        width: 90; max-width: 100%; height: auto; max-height: 100%; overflow-y: auto;
        scrollbar-gutter: stable; scrollbar-size-vertical: 1;
        background: $panel; padding: 1 1 1 2;
    }
    ProfilesScreen .title { text-style: bold; margin-bottom: 1; }
    ProfilesScreen .field { height: 1; }
    ProfilesScreen .field-label { width: 10; color: $text-muted; padding-left: 1; }
    ProfilesScreen Input, ProfilesScreen Input:focus {
        border: none; height: 1; padding: 0; margin: 0;
    }
    ProfilesScreen Input { background: $panel; }
    ProfilesScreen Input:focus { background: $boost; }
    ProfilesScreen #save-row { height: auto; margin-top: 1; }
    ProfilesScreen .hint { width: 1fr; color: $text-disabled; }
    """

    BINDINGS = [
        Binding("escape", "cancel", show=False),
        Binding("ctrl+s,ctrl+enter", "save", show=False, priority=True),
        Binding("ctrl+d", "delete", show=False, priority=True),
    ]

    def __init__(
        self,
        cfg: Config,
        last: LastUsed,
        save: Callable[[str, AgentProfile], None],
        delete: Callable[[str, ConfigScope], None],
        paths: dict[ConfigScope, Path],
    ) -> None:
        super().__init__()
        self.cfg, self.last, self.save_profile = cfg, last, save
        self.delete_profile = delete
        self.paths = paths

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("agent profiles", classes="title")
            yield CycleField(
                "profile", ["", *self.cfg.profiles], empty_label="new profile", id="edit-profile"
            )
            with Horizontal(classes="field", id="profile-source-row") as source_row:
                source_row.display = False
                yield Static("source", classes="field-label")
                yield Static(id="profile-source")
            with Horizontal(classes="field"):
                yield Static("name", classes="field-label")
                yield Input(placeholder="profile name", select_on_focus=False, id="profile-name")
            yield AgentControls(
                self.cfg,
                self.last,
                resolve_launch_defaults(self.cfg, self.last),
                extra_args=self.last.extra_args,
            )
            with Horizontal(id="save-row"):
                yield Static(
                    KeyHints(
                        "tab fields · h/l change · ctrl+s save to project"
                        " · ctrl+d delete · esc cancel"
                    ),
                    classes="hint",
                )
                yield Button("save", variant="primary", compact=True, id="save-profile")

    def on_mount(self) -> None:
        self.query_one("#profile-name", Input).focus()

    @on(CycleField.Changed, "#edit-profile")
    def _load_profile(self, event: CycleField.Changed) -> None:
        self.query_one("#profile-name", Input).value = event.value
        sources = self.cfg.profile_sources.get(event.value, ())
        self.query_one("#profile-source-row").display = bool(sources)
        self.query_one("#profile-source", Static).update(" + ".join(sources))
        if event.value:
            self.query_one(AgentControls).set_profile(self.cfg.profiles[event.value])
        else:
            defaults = resolve_launch_defaults(self.cfg, self.last)
            self.query_one(AgentControls).set_profile(
                AgentProfile(
                    agent=defaults.agent,
                    model=defaults.model,
                    effort=defaults.effort,
                    fast=defaults.fast,
                    gateway=defaults.gateway,
                    extra_args=list(self.last.extra_args),
                )
            )

    @on(Button.Pressed, "#save-profile")
    def action_save(self) -> None:
        name = self.query_one("#profile-name", Input).value.strip()
        try:
            validate_profile_name(name)
            profile = self.query_one(AgentControls).current_profile()
            profile.validate(f"profiles.{name}")
            self.save_profile(name, profile)
        except (ConfigError, OSError) as exc:
            self.notify(str(exc), severity="error")
            return
        self.dismiss(("saved", "project"))

    def action_delete(self) -> None:
        name = self.query_one("#edit-profile", CycleField).value
        sources = self.cfg.profile_sources.get(name, ())
        if not sources:
            return
        scope = sources[0]
        body = f"Delete {name!r} from the {scope} config?\n\n{self.paths[scope]}"
        if scope == "global":
            body += "\n\nThis affects every project that uses this global config."
        elif "global" in sources:
            body += "\n\nThe global profile will still be available."

        def decided(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                self.delete_profile(name, scope)
            except (ConfigError, OSError) as exc:
                self.notify(str(exc), severity="error")
                return
            self.dismiss(("deleted", scope))

        self.app.push_screen(ConfirmScreen("delete agent profile?", body, "delete"), decided)

    @on(Input.Submitted)
    def _submitted(self) -> None:
        self.focus_next()

    def key_enter(self, event: events.Key) -> None:
        if isinstance(self.focused, CycleField):
            event.stop()
            event.prevent_default()
            self.focus_next()

    def action_cancel(self) -> None:
        self.dismiss()
