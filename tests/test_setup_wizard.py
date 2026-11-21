import asyncio
import os
import sys
from pathlib import Path

import pytest
from textual.widgets import Checkbox, Select, SelectionList, TextArea

from project_sandbox_tui import capabilities
from project_sandbox_tui import setup_wizard as wizard


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Claude configured on the host, at the launcher's version."""
    path = tmp_path / "home"
    (path / ".claude").mkdir(parents=True)
    monkeypatch.setattr(capabilities, "installed_version", lambda: "0.4.1")
    return path


PROBED = capabilities.Capabilities(
    command=("project-sandbox",),
    version="0.4.1",
    source="probe",
    agents=("claude", "codex", "opencode", "pi", "copilot", "bash"),
    vcs_modes=("full", "read-only", "hidden"),
    runtimes={"docker": capabilities.RuntimeStatus("usable")},
    runtime_source="doctor",
)


def test_setup_dialog(home: Path) -> None:
    async def interact() -> None:
        app = wizard.SetupDialog(["claude", "codex", "copilot"], PROBED, home=home)
        async with app.run_test() as pilot:
            app.query_one(Select).value = "copilot"
            app.query_one(SelectionList).deselect("claude")
            app.query_one(TextArea).text = "Focus on reviews."
            await pilot.press("ctrl+s")
        result = app.return_value
        assert result is not None
        assert (result.host, result.profiles, result.instructions) == (
            "copilot",
            ["copilot", "bash"],  # codex has no host configuration here
            "Focus on reviews.",
        )
        assert result.sources["copilot"] == "token login"
        assert "--vcs-access=read-only" in result.summary

        app = wizard.SetupDialog(["copilot"], PROBED, home=home)
        async with app.run_test() as pilot:
            await pilot.press("escape")
        assert app.return_value is None

    asyncio.run(interact())


@pytest.mark.parametrize(
    ("host", "flags"),
    [("claude", []), ("codex", []), ("pi", []), ("copilot", ["-i"]), ("opencode", ["--prompt"])],
)
def test_host_handoff(tmp_path, monkeypatch, host, flags) -> None:
    (tmp_path / ".jj").mkdir()
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    executable = tmp_path / host
    executable.touch(mode=0o755)
    monkeypatch.chdir(subdir)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    instructions = "Include 'review'\nPreserve $HOME and $(hello) literally."

    def select(dialog):
        assert dialog.agents == [host]
        return wizard.Interview(host, [], instructions)

    def launch(path, argv):
        assert Path.cwd() == tmp_path
        assert path == executable
        assert argv[:-1] == [str(executable), *flags]
        assert f"Interview agent: {host}" in argv[-1]
        assert argv[-1].endswith(instructions)
        raise SystemExit(42)

    monkeypatch.setattr(wizard.SetupDialog, "run", select)
    monkeypatch.setattr(os, "execv", launch)
    with pytest.raises(SystemExit) as result:
        wizard.main([])
    assert result.value.code == 42


def test_no_host_agents(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / ".jj").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(SystemExit) as result:
        wizard.main(["-R", str(tmp_path)])
    assert result.value.code == 2
    assert "install a supported host agent" in capsys.readouterr().err


def test_profile_agents_combine_sources_with_the_gateway(home: Path) -> None:
    """Profile agents do not depend on which agents are installed on the host PATH."""

    async def interact() -> None:
        app = wizard.SetupDialog(["claude"], PROBED, home=home)
        async with app.run_test() as pilot:
            profiles = app.query_one(SelectionList)
            values = [profiles.get_option_at_index(i).value for i in range(profiles.option_count)]
            assert values == ["claude", "copilot", "bash"]
            app.query_one(Checkbox).value = True
            await pilot.pause()
            values = [profiles.get_option_at_index(i).value for i in range(profiles.option_count)]
            assert values == ["claude", "opencode", "pi", "copilot", "bash"]
            await pilot.press("ctrl+s")
        result = app.return_value
        assert result is not None and result.gateway
        assert result.sources["claude"] == "host credentials"
        assert result.sources["pi"] == "agent gateway"
        prompt = result.prompt(["claude"])
        assert "pi (agent gateway)" in prompt and "claude (host credentials)" in prompt
        assert "agent gateway section" in prompt

    asyncio.run(interact())


def test_gateway_preselected_when_configured(home: Path) -> None:
    async def interact() -> None:
        app = wizard.SetupDialog(["claude"], PROBED, gateway_configured=True, home=home)
        async with app.run_test():
            assert app.query_one(Checkbox).value
            assert "pi" in app.query_one(SelectionList).selected

    asyncio.run(interact())


def test_fallback_wording_when_launcher_cannot_be_probed(home: Path) -> None:
    async def interact() -> None:
        app = wizard.SetupDialog(["claude"], capabilities.fallback(["project-sandbox"]), home=home)
        async with app.run_test() as pilot:
            await pilot.press("ctrl+s")
        result = app.return_value
        assert result is not None
        assert "built-in fallbacks" in result.summary
        assert "`project-sandbox --help`" in result.prompt(["claude"])

    asyncio.run(interact())
