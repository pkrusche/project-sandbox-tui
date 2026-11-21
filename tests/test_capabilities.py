from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from project_sandbox_tui import capabilities as caps_mod
from project_sandbox_tui.capabilities import (
    AGENT_GATEWAY,
    HOST_CREDENTIALS,
    NO_CREDENTIALS,
    SANDBOX_LOGIN,
    TOKEN_LOGIN,
    Capabilities,
    RuntimeStatus,
)
from project_sandbox_tui.config import AGENTS, RUNTIMES

FIXTURES = Path(__file__).with_name("fixtures")
HELP = (FIXTURES / "project-sandbox-0.4.1-help.txt").read_text()
DOCTOR = (FIXTURES / "project-sandbox-0.4.1-doctor.json").read_text()


def fake_runner(
    responses: Mapping[str, tuple[int, str] | Exception], calls: list[list[str]] | None = None
) -> caps_mod.Runner:
    """Answer by the first launcher argument: --version, --help or doctor."""

    def runner(argv: list[str], timeout: float) -> tuple[int, str]:
        if calls is not None:
            calls.append(argv)
        response = responses.get(argv[1], (2, "unknown"))
        if isinstance(response, Exception):
            raise response
        return response

    return runner


REAL = {"--version": (0, "project-sandbox 0.4.1"), "--help": (0, HELP), "doctor": (0, DOCTOR)}


# ---------------------------------------------------------------- parsing


def test_real_help_and_doctor_shapes() -> None:
    caps = caps_mod.discover(["project-sandbox"], fake_runner(REAL))
    assert caps.discovered and caps.version == "0.4.1"
    assert caps.agents == ("claude", "codex", "opencode", "pi", "copilot", "bash")
    assert caps.vcs_modes == ("full", "read-only", "hidden")
    assert caps.runtime_source == "doctor"
    assert caps.runtimes["docker"].status == "usable"
    assert caps.runtimes["apple-container"] == RuntimeStatus("unavailable", "macOS only")
    assert caps.auto_selects == "docker"
    assert caps.mount and caps.help_ok and caps.problems == ()


def test_missing_vcs_access_means_no_modes() -> None:
    old = HELP.replace("[--vcs-access {full,read-only,hidden}]", "")
    old = old.replace("--vcs-access {full,read-only,hidden}", "")
    caps = caps_mod.discover(["ps"], fake_runner({**REAL, "--help": (0, old)}))
    assert caps.discovered and caps.vcs_modes == ()


@pytest.mark.parametrize(
    "text",
    [
        "usage: x [--agent AGENT]",  # plain argparse metavar, as the test fake prints
        "usage: x [--agent {claude,Bad Name}]",
        "usage: x [--agent {}]",
    ],
)
def test_malformed_or_missing_agent_choices_fall_back(text: str) -> None:
    caps = caps_mod.discover(["ps"], fake_runner({**REAL, "--help": (0, text)}))
    assert not caps.discovered
    assert caps.agents == AGENTS and caps.vcs_modes == ()
    assert any("--agent" in problem for problem in caps.problems)


def test_invalid_doctor_json_uses_runtime_choices_from_help() -> None:
    caps = caps_mod.discover(["ps"], fake_runner({**REAL, "doctor": (0, "{not json")}))
    assert caps.discovered and caps.runtime_source == "help"
    assert set(caps.runtimes) == {"auto", "apple-container", "docker", "podman", "colima", "chroot"}
    assert {rt.status for rt in caps.runtimes.values()} == {"unknown"}
    assert any("doctor --json" in problem for problem in caps.problems)


def test_doctor_unsupported_and_no_runtime_choices_uses_builtin_list() -> None:
    help_text = HELP.replace("--runtime {auto,apple-container,docker,podman,colima,chroot}", "")
    runner = fake_runner({**REAL, "--help": (0, help_text), "doctor": (2, "invalid choice")})
    caps = caps_mod.discover(["ps"], runner)
    assert caps.runtime_source == "fallback" and tuple(caps.runtimes) == RUNTIMES


def test_timed_out_runner_falls_back_without_raising() -> None:
    runner = fake_runner({"--version": (124, "timed out"), "--help": (124, "timed out after 30s")})
    caps = caps_mod.discover(["ps"], runner)
    assert not caps.discovered and not caps.help_ok and caps.version == ""
    assert any("timed out" in problem for problem in caps.problems)


def test_missing_launcher_probes_once() -> None:
    calls: list[list[str]] = []
    caps = caps_mod.discover(
        ["nope"], fake_runner({"--version": FileNotFoundError(2, "No such file")}, calls)
    )
    assert calls == [["nope", "--version"]]
    assert not caps.discovered and "could not run" in caps.problems[0]


def test_doctor_statuses_map_to_usable_and_unavailable() -> None:
    doc = {
        "checks": [
            {"section": "runtimes", "name": "docker", "status": "warn", "detail": "old"},
            {"section": "runtimes", "name": "podman", "status": "fail", "detail": "x", "hint": "h"},
            {"section": "runtimes", "name": "auto", "status": "ok", "detail": "selects docker"},
            {"section": "vcs", "name": "jj", "status": "ok", "detail": "0.46"},
        ]
    }
    parsed = caps_mod.parse_doctor("noise before\n" + json.dumps(doc))
    assert parsed is not None
    runtimes, auto = parsed
    assert runtimes["docker"].status == "usable"
    assert runtimes["podman"] == RuntimeStatus("unavailable", "x", "h")
    assert auto == "docker" and "jj" not in runtimes


# ---------------------------------------------------------------- availability


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home directory for project-sandbox's host rules, at the launcher's version."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(caps_mod, "installed_version", lambda: "0.4.1")
    return home


PROBED = Capabilities(command=("ps",), version="0.4.1", source="probe")


def test_host_credentials_source(host: Path) -> None:
    (host / ".claude").mkdir()
    result = caps_mod.agent_sources("claude", PROBED, home=host)
    assert result.available is True and result.sources == (HOST_CREDENTIALS,)


def test_missing_host_configuration_names_the_directory(host: Path) -> None:
    result = caps_mod.agent_sources("codex", PROBED, home=host)
    assert result.available is False
    assert ".codex" in result.reason


def test_gateway_source_only_for_gateway_agents(host: Path) -> None:
    pi = caps_mod.agent_sources("pi", PROBED, gateway_configured=True, home=host)
    assert pi.available is True and pi.sources == (AGENT_GATEWAY,)
    codex = caps_mod.agent_sources("codex", PROBED, gateway_configured=True, home=host)
    assert codex.available is False


def test_launch_without_gateway_does_not_count_gateway(host: Path) -> None:
    result = caps_mod.launch_availability("pi", PROBED, gateway=False, home=host)
    assert result.available is False and "gateway" in result.reason


def test_token_login_and_bash(host: Path) -> None:
    assert caps_mod.agent_sources("copilot", PROBED, home=host).sources == (TOKEN_LOGIN,)
    assert caps_mod.agent_sources("bash", PROBED, home=host).sources == (NO_CREDENTIALS,)


def test_no_forward_credentials_is_login_in_sandbox(host: Path) -> None:
    result = caps_mod.launch_availability(
        "codex", PROBED, extra_args=["--no-forward-credentials"], home=host
    )
    assert result.available is True and result.sources == (SANDBOX_LOGIN,)


def test_version_mismatch_gives_unknown(host: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(caps_mod, "installed_version", lambda: "0.5.0")
    result = caps_mod.agent_sources("codex", PROBED, home=host)
    assert result.available is None and "0.5.0" in result.reason


def test_import_failure_gives_unknown(host: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import project_sandbox.config_agents as config_agents

    def boom(home: Path | None = None) -> tuple[str, ...]:
        raise RuntimeError("changed API")

    monkeypatch.setattr(config_agents, "available_agents", boom)
    assert caps_mod.agent_sources("codex", PROBED, home=host).available is None


def test_installed_package_rules_are_callable() -> None:
    """The private rules sbx-tui imports exist in the locked project-sandbox."""
    from project_sandbox import config_agents

    assert "bash" in config_agents.available_agents(Path("/nonexistent"))
    assert "claude" in config_agents._agent_host_paths(Path("/nonexistent"))


# ---------------------------------------------------------------- cache


def test_cache_round_trip(tmp_path: Path) -> None:
    caps = caps_mod.discover(["project-sandbox"], fake_runner(REAL))
    path = caps_mod.save(caps, tmp_path / "c.json")
    loaded = caps_mod.load(path)
    assert loaded is not None
    assert loaded.discovered_at is not None and caps.discovered_at is not None
    assert loaded.discovered_at == caps.discovered_at.replace(microsecond=0)
    assert loaded.agents == caps.agents and loaded.runtimes == caps.runtimes
    assert (loaded.vcs_modes, loaded.mount, loaded.version) == (caps.vcs_modes, True, "0.4.1")


@pytest.mark.parametrize(
    "content", ["{", "[]", '{"version": 99}', '{"version": 1, "command": "ps"}']
)
def test_corrupt_cache_is_ignored(tmp_path: Path, content: str) -> None:
    path = tmp_path / "c.json"
    path.write_text(content)
    assert caps_mod.load(path) is None


def test_oversize_cache_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(" " * (caps_mod.MAX_CACHE_BYTES + 1))
    assert caps_mod.load(path) is None


def test_staleness() -> None:
    now = datetime(2026, 11, 21, 12, tzinfo=UTC)
    caps = Capabilities(command=("ps",), version="0.4.1", discovered_at=now, source="probe")
    assert caps_mod.is_fresh(caps, ["ps"], version="0.4.1", now=now)
    assert not caps_mod.is_fresh(caps, ["uvx", "ps"], now=now)
    assert not caps_mod.is_fresh(caps, ["ps"], version="0.5.0", now=now)
    assert not caps_mod.is_fresh(caps, ["ps"], now=now + timedelta(hours=25))
    assert not caps_mod.is_fresh(None, ["ps"], now=now)


def test_refresh_reuses_fresh_cache_after_version_check(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    calls: list[list[str]] = []
    first = caps_mod.refresh(["ps"], repo, fake_runner(REAL, calls))
    assert [c[1] for c in calls] == ["--version", "--help", "doctor"]
    calls.clear()
    again = caps_mod.refresh(["ps"], repo, fake_runner(REAL, calls))
    assert [c[1] for c in calls] == ["--version"] and again.agents == first.agents
    calls.clear()
    upgraded = {**REAL, "--version": (0, "project-sandbox 0.5.0")}
    assert caps_mod.refresh(["ps"], repo, fake_runner(upgraded, calls)).version == "0.5.0"
    assert [c[1] for c in calls] == ["--version", "--version", "--help", "doctor"]


def test_cached_never_probes_and_ignores_other_commands(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert not caps_mod.cached(["ps"], repo).discovered
    caps_mod.refresh(["ps"], repo, fake_runner(REAL))
    assert caps_mod.cached(["ps"], repo).discovered
    assert not caps_mod.cached(["uvx", "ps"], repo).discovered


def test_parser_matches_installed_launcher() -> None:
    """The locked project-sandbox's real output still parses (skipped without it)."""
    import shutil

    from project_sandbox_tui.capabilities import installed_version

    launcher = shutil.which("project-sandbox")
    if launcher is None or not installed_version():
        pytest.skip("project-sandbox is not installed")
    caps = caps_mod.discover([launcher])
    assert caps.discovered, caps.problems
    assert caps.version == installed_version()
    assert {"claude", "codex", "bash"} <= set(caps.agents)
    assert "full" in caps.vcs_modes
    assert caps.runtime_source == "doctor", caps.problems
    assert caps.mount


def test_profile_agents_combine_sources_and_skip_unavailable(host: Path) -> None:
    (host / ".claude").mkdir()
    agents = dict(caps_mod.profile_agents(PROBED, gateway=True, home=host))
    assert agents["claude"].sources == (HOST_CREDENTIALS,)
    assert agents["pi"].sources == (AGENT_GATEWAY,)
    assert agents["copilot"].sources == (TOKEN_LOGIN,)
    assert "codex" not in agents  # no host configuration and no gateway support


def test_setup_summary_lists_capabilities(host: Path) -> None:
    caps = caps_mod.discover(["project-sandbox"], fake_runner(REAL))
    text = caps_mod.setup_summary(caps, home=host)
    assert "copilot (token login)" in text and "bash (no credentials needed)" in text
    assert "--vcs-access=read-only" in text
    assert "auto selects docker" in text
    assert "fallback" not in text


def test_setup_summary_says_when_it_is_a_fallback() -> None:
    text = caps_mod.setup_summary(caps_mod.fallback(["uvx", "project-sandbox"]))
    assert "built-in fallbacks" in text and "`uvx project-sandbox --help`" in text
