import asyncio
import dataclasses
import os
import sys
from pathlib import Path

import pytest
from textual.widgets import Checkbox, Select, SelectionList, TextArea

from project_sandbox_tui import capabilities, catalogue
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
            app.query_one("#profiles", SelectionList).deselect("claude")
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
        assert result.summary in result.prompt(["claude"])

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
        assert wizard.skill_template("sbx-tui-safety") in argv[-1]
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
            profiles = app.query_one("#profiles", SelectionList)
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
            assert "pi" in app.query_one("#profiles", SelectionList).selected

    asyncio.run(interact())


def test_summary_lists_providers_levels_and_personas(home: Path) -> None:
    from project_sandbox_tui.config import Config

    caps = dataclasses.replace(PROBED, egress_modes=("firewall", "sidecar"))
    text = capabilities.setup_summary(caps, home=home, cfg=Config())
    assert "Provider rows for catalogue.default_provider:" in text
    assert "claude-code (Claude Code: available, host credentials)" in text
    assert "gateway-openai (Gateway/OpenAI: unavailable, no agent gateway is configured" in text
    assert "copilot-openai (Copilot/OpenAI: available, token login)" in text
    assert "1 (no firewall), 2 (firewall + package sources), 3 (agent API only)" in text
    assert "reviewer (high / high / level 4)" in text and "commit-reviewer" not in text
    assert "need security.egress" not in text


def test_summary_without_sidecar_egress(home: Path) -> None:
    from project_sandbox_tui.config import Config

    caps = dataclasses.replace(PROBED, egress_modes=("firewall",))
    text = capabilities.setup_summary(caps, home=home, cfg=Config())
    assert 'levels 3 to 5 need security.egress = "firewall" in the global config' in text
    assert "supports with security.egress = 'sidecar': 1 (no firewall), 2 (" in text


def test_dialog_chooses_offered_providers(home: Path) -> None:
    from project_sandbox_tui.config import Config

    async def interact() -> None:
        app = wizard.SetupDialog(["claude"], PROBED, home=home)
        async with app.run_test() as pilot:
            providers = app.query_one("#providers", SelectionList)
            assert len(providers.selected) == len(catalogue.ROWS)  # [] offers every row
            prompts = [
                str(providers.get_option_at_index(i).prompt) for i in range(providers.option_count)
            ]
            assert "Gateway/OpenAI — unavailable: no agent gateway is configured" in prompts[2]
            for row in catalogue.ROWS:
                if row.id not in ("claude-code", "copilot-fusion"):
                    providers.deselect(row.id)
            await pilot.press("ctrl+s")
        result = app.return_value
        assert result is not None and result.providers == ["claude-code", "copilot-fusion"]
        assert "gateway-openai" not in result.summary  # the interview sees only offered rows
        assert "limited catalogue.providers to claude-code, copilot-fusion" in result.prompt([])

        # An existing allow-list is preselected; keeping every row saves [].
        cfg = Config()
        cfg.catalogue.providers = ["codex"]
        app = wizard.SetupDialog(["claude"], PROBED, home=home, cfg=cfg)
        async with app.run_test() as pilot:
            providers = app.query_one("#providers", SelectionList)
            assert providers.selected == ["codex"]
            providers.select_all()
            await pilot.press("ctrl+s")
        assert app.return_value is not None and app.return_value.providers == []
        assert "kept every provider row" in app.return_value.prompt([])

    asyncio.run(interact())


def test_dialog_refuses_providers_that_would_not_validate(home: Path) -> None:
    from project_sandbox_tui.config import Config

    async def interact() -> None:
        cfg = Config()
        cfg.catalogue.default_provider = "claude-code"
        app = wizard.SetupDialog(["claude"], PROBED, home=home, cfg=cfg)
        async with app.run_test() as pilot:
            providers = app.query_one("#providers", SelectionList)
            providers.deselect_all()
            await pilot.press("ctrl+s")
            assert app.return_value is None and app.is_running  # nothing selected
            providers.select("codex")
            await pilot.press("ctrl+s")
            assert app.return_value is None and app.is_running  # default not offered
            providers.select("claude-code")
            await pilot.press("ctrl+s")
        assert app.return_value is not None
        assert app.return_value.providers == ["codex", "claude-code"]

    asyncio.run(interact())


def test_save_providers_updates_the_project_config(repo: Path) -> None:
    from project_sandbox_tui.config import Config, load, project_config_path

    wizard.save_providers(repo, Config(), None)
    wizard.save_providers(repo, Config(), [])  # unchanged: nothing written
    assert not project_config_path(repo).exists()
    wizard.save_providers(repo, Config(), ["claude-code"])
    assert load(repo).catalogue.providers == ["claude-code"]
    wizard.save_providers(repo, load(repo), [])
    assert load(repo).catalogue.providers == []


@pytest.mark.parametrize("previously_trusted", [False, True])
def test_save_providers_refuses_unreviewed_project_config(
    repo: Path, previously_trusted: bool
) -> None:
    from project_sandbox_tui.config import ConfigError, ProjectTrust, load, project_config_path

    path = project_config_path(repo)
    path.write_text('[catalogue]\nproviders = ["codex"]\n')
    if previously_trusted:
        ProjectTrust.check(repo).trust()
        path.write_text('[catalogue]\nproviders = ["copilot-fusion"]\n')
    before = path.read_text()
    cfg = load(repo)
    with pytest.raises(ConfigError, match="review it with sbx-tui --trust-config"):
        wizard.save_providers(repo, cfg, ["claude-code"])
    assert path.read_text() == before
    assert load(repo).catalogue.providers == cfg.catalogue.providers
