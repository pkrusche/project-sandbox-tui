"""Saved profile configuration, editing, and launches through the real TUI."""

from __future__ import annotations

import contextlib
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, Static, TextArea

from project_sandbox_tui import config, settings
from project_sandbox_tui.app import SandboxTUI
from project_sandbox_tui.config import AgentProfile, Config, ConfigError, LastUsed, ProjectTrust
from project_sandbox_tui.jobs import JobManager
from project_sandbox_tui.sandbox import LaunchSpec, RunPaths, build_argv
from project_sandbox_tui.screens.agent import CUSTOM_MODEL, AgentControls
from project_sandbox_tui.screens.launch import LaunchScreen
from project_sandbox_tui.screens.profiles import ProfilesScreen
from project_sandbox_tui.widgets import CycleField
from tests.test_app import run_app, wait_until


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ({"profiles": []}, "must be a table"),
        ({"profiles": {"p": "codex"}}, "must be a table"),
        ({"profiles": {"p": {"agent": "unknown"}}}, "profiles.p.agent"),
        ({"profiles": {"p": {"effort": "huge"}}}, "profiles.p.effort"),
        ({"profiles": {"p": {"model": "two words"}}}, "invalid model name"),
        ({"profiles": {"p": {"fast": "yes"}}}, "must be true or false"),
        ({"profiles": {"p": {"extra_args": "--verbose"}}}, "list of strings"),
        ({"profiles": {"p": {"extra_args": ["\0"]}}}, "NUL"),
        ({"profiles": {"p": {"typo": True}}}, "unknown key"),
        ({"profiles": {"custom": {}}}, "reserved"),
        ({"profiles": {" Custom ": {}}}, "surrounding spaces"),
        ({"profiles": {"": {}}}, "printable text"),
    ],
)
def test_profile_validation(data: dict[str, Any], error: str) -> None:
    with pytest.raises(ConfigError, match=error):
        config.from_dict(data).validate()


@pytest.mark.parametrize("trusted", [True, False])
def test_save_profile_preserves_config_and_trust(repo: Path, trusted: bool) -> None:
    path = config.project_config_path(repo)
    path.write_text('# keep me\n[project]\nbase_image = "python:3.12-slim"\n')
    if trusted:
        ProjectTrust.check(repo).trust(repo)
    profile = AgentProfile("codex", "my-model", "high", True, ["--new-switch", "a b", "$(literal)"])
    editor = settings.Editor("project", repo)
    name = 'Deep review.v2 "fast"'
    editor.save_profile(name, profile)
    text = path.read_text()
    assert '# keep me\n[project]\nbase_image = "python:3.12-slim"' in text
    assert config.from_dict(tomllib.loads(text)).profiles == {name: profile}
    assert ProjectTrust.check(repo).needs_review is not trusted
    if not trusted:
        assert config.load(repo).profiles == {}
        ProjectTrust.check(repo).trust(repo)
    assert config.load(repo).profiles == {name: profile}
    # Editing again replaces false/empty values as well, without dropping other settings.
    editor.save_profile(name, AgentProfile(agent="bash"))
    cfg = config.load(repo)
    assert cfg.profiles[name] == AgentProfile(agent="bash")
    assert cfg.project.base_image == "python:3.12-slim"


def test_profiles_merge_and_cli_settings(repo: Path) -> None:
    global_path = config.global_config_path()
    global_path.parent.mkdir(parents=True)
    global_path.write_text('[profiles.review]\nagent = "codex"\nmodel = "m"\neffort = "low"\n')
    editor = settings.Editor("project", repo)
    editor.set(settings.parse_key("profiles.review.effort"), "high")
    cfg = config.load(repo)
    assert cfg.profiles["review"] == AgentProfile("codex", "m", "high")
    assert ("profiles.review.effort", "high") in settings.flatten(settings.effective(cfg))
    assert editor.unset(settings.parse_key("profiles.review.effort"))
    assert config.load(repo).profiles["review"].effort == "low"


@pytest.mark.parametrize("background", [False, True])
def test_profile_argv_is_a_snapshot(tmp_path: Path, background: bool) -> None:
    cfg = Config()
    cfg.project.base_image = "python:3.12-slim"
    cfg.sandbox.extra_args = ["--verbose"]
    spec = LaunchSpec(
        "topic",
        "codex",
        model="m",
        effort="high",
        fast=True,
        profile="review",
        extra_args=["--new-switch", "a b", "$(literal)"],
        mode="background" if background else "interactive",
        prompt_text="review",
    )
    restored = LaunchSpec.from_dict(spec.to_dict())
    assert restored == spec
    cfg.profiles["review"] = AgentProfile(agent="bash")
    paths = RunPaths(tmp_path / "log", tmp_path / "summary", tmp_path / "stdout")
    argv = build_argv(cfg, tmp_path, restored, paths if background else None)
    assert "--agent=codex" in argv and "--model=m" in argv and "--fast" in argv
    start = argv.index("--verbose")
    assert argv[start : start + 4] == ["--verbose", "--new-switch", "a b", "$(literal)"]
    old = LaunchSpec.from_dict({"branch": "topic", "agent": "bash"})
    assert old.profile == "" and old.extra_args == []


@pytest.mark.parametrize("mode", ["i", "r"])
def test_launch_profiles_and_custom(
    repo: Path,
    fake_cfg: Callable[..., Config],
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = fake_cfg()
    cfg.profiles = {
        "Deep review": AgentProfile("codex", "profile-model", "high", True, ["--new-flag=a b"]),
        "shell": AgentProfile(agent="bash"),
    }

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        if mode == "i":
            monkeypatch.setattr(app, "suspend", contextlib.nullcontext)
        await pilot.press("j", mode)
        form = app.screen
        assert isinstance(form, LaunchScreen)
        field = form.query_one("#profile", CycleField)
        controls = form.query_one(AgentControls)
        assert field.value == "Deep review" and not controls.display
        spec = form.current_spec()
        assert (spec.agent, spec.model, spec.effort, spec.fast) == (
            "codex",
            "profile-model",
            "high",
            True,
        )
        assert spec.profile == "Deep review" and spec.extra_args == ["--new-flag=a b"]
        if mode == "r":
            form.query_one("#prompt", TextArea).text = "exit:0"
        field.value = "shell"
        await pilot.pause()
        assert form.current_spec().agent == "bash" and form.current_spec().extra_args == []
        field.value = "custom"
        await pilot.pause()
        assert controls.display
        form.query_one("#extra-args", Input).value = "--new-flag 'custom value'"
        await pilot.pause()
        assert form.current_spec().profile == ""
        assert form.current_spec().extra_args == ["--new-flag", "custom value"]
        field.value = "Deep review"
        await pilot.pause()
        assert not controls.display
        assert "--new-flag=a b" in str(form.query_one("#preview", Static).render())
        await pilot.press("ctrl+s" if mode == "r" else "enter")
        await wait_until(pilot, lambda: "feature/x" in app.jobs.jobs)
        if mode == "r":
            await wait_until(pilot, lambda: not app.jobs.jobs["feature/x"].running)
        job = JobManager(repo, cfg).jobs["feature/x"]
        assert job.spec.profile == "Deep review"
        assert "--new-flag=a b" in job.argv
        assert LastUsed.load(repo).profile == "Deep review"

    if mode == "i":
        cfg.sandbox.command = "true"
    run_app(repo, cfg, scenario)


@pytest.mark.parametrize("remembered", ["shell", "", "removed"])
def test_remember_profile_or_custom(
    repo: Path,
    fake_cfg: Callable[..., Config],
    remembered: str,
) -> None:
    LastUsed(profile=remembered).save(repo)
    cfg = fake_cfg()
    cfg.profiles = {"shell": AgentProfile(agent="bash")}

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j", "i")
        assert isinstance(app.screen, LaunchScreen)
        expected = remembered if remembered == "shell" else "custom"
        assert app.screen.query_one("#profile", CycleField).value == expected

    run_app(repo, cfg, scenario)


def test_profile_editor_save_edit_and_cancel(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await app.run_command("config-profiles")
        await pilot.pause()
        editor = app.screen
        assert isinstance(editor, ProfilesScreen)
        # Invalid names and malformed quoting do not close the dialog or create a file.
        await pilot.press("ctrl+s")
        assert app.screen is editor and not config.project_config_path(repo).exists()
        editor.query_one("#profile-name", Input).value = "Deep review"
        editor.query_one("#agent", CycleField).value = "codex"
        await pilot.pause()
        editor.query_one("#model", CycleField).value = CUSTOM_MODEL
        await pilot.pause()
        editor.query_one("#custom-model", Input).value = "saved-model"
        editor.query_one("#effort", CycleField).value = "high"
        editor.query_one("#fast", CycleField).value = "on"
        editor.query_one("#extra-args", Input).value = "--new-flag 'unfinished"
        await pilot.press("ctrl+s")
        assert app.screen is editor and not config.project_config_path(repo).exists()
        editor.query_one("#extra-args", Input).value = "--new-flag 'a b'"
        await pilot.click("#save-profile")
        await pilot.pause()
        expected = AgentProfile("codex", "saved-model", "high", True, ["--new-flag", "a b"])
        assert app.cfg.profiles == {"Deep review": expected}
        assert config.load(repo).profiles == app.cfg.profiles
        assert not ProjectTrust.check(repo).needs_review
        await app.run_command("config-profiles")
        await pilot.pause()
        editor = app.screen
        assert isinstance(editor, ProfilesScreen)
        editor.query_one("#edit-profile", CycleField).value = "Deep review"
        await pilot.pause()
        assert editor.query_one(AgentControls).current_profile() == expected
        editor.query_one("#fast", CycleField).value = "off"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not config.load(repo).profiles["Deep review"].fast
        before = config.project_config_path(repo).read_text()
        await app.run_command("config-profiles")
        await pilot.pause()
        app.screen.query_one("#profile-name", Input).value = "discard"
        await pilot.press("escape")
        assert config.project_config_path(repo).read_text() == before

    run_app(repo, fake_cfg(), scenario)


def test_custom_cli_args_validation_and_memory(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j", "r")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        form.query_one("#prompt", TextArea).text = "exit:0"
        field = form.query_one("#extra-args", Input)
        field.value = "--new-flag 'unfinished"
        await pilot.press("ctrl+s")
        assert app.screen is form and not app.jobs.jobs
        assert "No closing quotation" in str(form.query_one("#preview", Static).render())
        field.value = "--new-flag 'a b'"
        await pilot.press("ctrl+s")
        await wait_until(
            pilot, lambda: "feature/x" in app.jobs.jobs and not app.jobs.jobs["feature/x"].running
        )
        last = LastUsed.load(repo)
        assert last.profile == "" and last.extra_args == ["--new-flag", "a b"]
        await pilot.press("r")
        form = app.screen
        assert isinstance(form, LaunchScreen)
        assert form.current_spec().extra_args == ["--new-flag", "a b"]

    run_app(repo, fake_cfg(), scenario)
