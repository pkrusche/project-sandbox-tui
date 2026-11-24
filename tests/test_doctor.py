from __future__ import annotations

import dataclasses
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from project_sandbox_tui import capabilities, doctor
from project_sandbox_tui import models as models_mod
from project_sandbox_tui.config import Action, AgentProfile, Config


def runner(responses: Mapping[str, tuple[int, str] | None]) -> doctor.Runner:
    """Answer by the argv's first word(s); None means "not found"."""

    def run(argv: list[str], timeout: float) -> tuple[int, str]:
        for prefix, response in responses.items():
            if " ".join(argv).startswith(prefix):
                if response is None:
                    raise FileNotFoundError(argv[0])
                return response
        raise AssertionError(f"unexpected command {argv}")

    return run


HEALTHY = {
    "jj --version": (0, "jj 0.46.0-7d382314"),
    "project-sandbox --version": (0, "project-sandbox 0.4.1"),
    "project-sandbox --help": (0, "--mount MOUNT"),
    "docker info": (0, "Server Version: 27"),
}


def by_name(checks: list[doctor.Check]) -> dict[str, doctor.Check]:
    return {c.name: c for c in checks}


@pytest.fixture
def egress_policy() -> Path:
    """A sidecar policy, so a healthy host reports no egress failure."""
    path = doctor.egress_policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('allow = ["api.openai.com", "api.anthropic.com"]\n')
    return path


def test_healthy(repo: Path) -> None:
    # The fake runner serves Docker; don't auto-detect a runtime on the test host.
    (repo / ".project-sandbox-tui.toml").write_text(
        '[project]\nbase_image = "img"\n[sandbox]\nruntime = "docker"\n'
    )
    from project_sandbox_tui.config import ProjectTrust

    ProjectTrust.check(repo).trust(repo)
    checks = by_name(doctor.run_checks(repo, runner=runner(HEALTHY), which=lambda c: f"/bin/{c}"))
    for name in ("jj", "repository", "project-sandbox", "container runtime"):
        assert checks[name].status == "ok", checks[name]
    assert checks["project-sandbox"].detail == "0.4.1 (project-sandbox)"
    assert "image" not in checks and "project config" not in checks
    assert checks["comparisons to main"].status == "ok"


@pytest.mark.parametrize("configured", ["", "missing"])
def test_comparison_diagnostics_missing_baseline(repo: Path, configured: str) -> None:
    from project_sandbox_tui.vcs import detect
    from tests.conftest import jj

    jj(repo, "bookmark", "delete", "main")
    branches = detect(repo).list_branches(compare_to_main=configured)
    checks = doctor.check_comparisons(branches, configured)
    assert checks[0].status == "warn"
    assert checks[1].detail == "0/2 revisions available; 2 show ?"
    for check in checks[2:]:
        assert check.status == "warn"
        assert "set project.main_branch" in check.hint
        assert ("'missing' does not exist" if configured else "no local main") in check.detail


def test_comparison_diagnostics_unrequested_listing(repo: Path) -> None:
    from project_sandbox_tui.vcs import detect

    checks = doctor.check_comparisons(detect(repo).list_branches())
    assert checks[0].status == "ok"
    assert all("not requested" in check.detail for check in checks[2:])


@pytest.mark.parametrize(
    ("output", "status"),
    [("jj 0.46.0", "ok"), ("jj 0.45.9", "warn"), ("garbage", "fail"), (None, "fail")],
)
def test_jj_versions(output: str | None, status: str) -> None:
    check = doctor.check_jj(runner({"jj": None if output is None else (0, output)}))
    assert check.status == status


@pytest.mark.parametrize(
    ("response", "status", "text"),
    [
        ((0, "project-sandbox 0.4.1"), "ok", "0.4.1"),
        ((0, "project-sandbox 0.4.0"), "fail", "too old"),
        ((1, "Traceback\nboom"), "fail", "boom"),
        (None, "fail", "not found"),
    ],
)
def test_project_sandbox_versions(response: tuple[int, str] | None, status: str, text: str) -> None:
    cfg = Config()
    cfg.sandbox.command = "uvx project-sandbox"
    check = doctor.check_project_sandbox(cfg, runner({"uvx project-sandbox": response}))
    assert check.status == status and text in check.detail


def test_runtime_checks() -> None:
    cfg = Config()
    cfg.sandbox.runtime = "podman"
    up = runner({"podman info": (0, "ok")})
    assert doctor.check_runtime(cfg, up, which=lambda c: f"/bin/{c}").status == "ok"
    missing = doctor.check_runtime(cfg, up, which=lambda c: None)
    assert missing.status == "fail" and "podman not found" in missing.detail
    down = doctor.check_runtime(
        cfg, runner({"podman info": (125, "cannot connect")}), which=lambda c: f"/bin/{c}"
    )
    assert down.status == "fail" and "cannot connect" in down.detail
    cfg.sandbox.runtime = "apple-container"
    stopped = doctor.check_runtime(
        cfg, runner({"container system status": (1, "not running")}), which=lambda c: c
    )
    assert stopped.hint == "container system start"


def test_outside_a_repository_and_main(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    egress_policy: Path,
) -> None:
    monkeypatch.setattr(doctor, "run", runner({**HEALTHY, "project-sandbox --version": None}))
    monkeypatch.setattr(doctor.shutil, "which", lambda c: f"/bin/{c}")
    monkeypatch.setattr(doctor, "runtime_cli", lambda runtime, which: "docker")
    assert doctor.main(tmp_path) == 1
    out = capsys.readouterr().out
    assert "! repository:" in out and "✗ project-sandbox" in out
    assert "→ uv tool install project-sandbox" in out and "1 problem found" in out


def test_probe_timeout_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    probe = Mock(side_effect=subprocess.TimeoutExpired(["jj", "--version"], 30))
    monkeypatch.setattr(capabilities.subprocess, "run", probe)  # doctor.run lives there
    check = doctor.check_jj(doctor.run)
    assert check.status == "fail" and "timed out after 30s" in check.detail


@pytest.mark.usefixtures("configured_agent_home")
def test_warnings_do_not_fail_doctor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    egress_policy: Path,
) -> None:
    monkeypatch.setattr(doctor, "run", runner(HEALTHY))
    monkeypatch.setattr(doctor.shutil, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(doctor, "runtime_cli", lambda runtime, which: "docker")
    assert doctor.main(tmp_path) == 0
    out = capsys.readouterr().out
    assert "! repository:" in out and "all good" in out


@pytest.mark.usefixtures("configured_agent_home")
def test_unknown_action_profiles_warn(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    egress_policy: Path,
) -> None:
    from project_sandbox_tui.config import ProjectTrust

    config = repo / ".project-sandbox-tui.toml"
    config.write_text(
        '[project]\nbase_image = "img"\n[sandbox]\nruntime = "docker"\n'
        '[profiles.deep]\nagent = "codex"\n'
        "[actions.review]\nprompt = 'r'\nprofile = 'deep'\n"
        "[actions.check]\nprompt = 'c'\nprofile = 'gone'\n"
        "[actions.lint]\nprompt = 'l'\nprofile = 'typo'\n"
    )
    ProjectTrust.check(repo).trust(repo)
    checks = by_name(doctor.run_checks(repo, runner=runner(HEALTHY), which=lambda c: f"/bin/{c}"))
    check = checks["action profiles"]
    assert check.status == "warn"
    assert check.detail == "unknown profile: actions.check -> 'gone', actions.lint -> 'typo'"
    assert "review" not in check.detail

    monkeypatch.setattr(doctor, "run", runner(HEALTHY))
    monkeypatch.setattr(doctor.shutil, "which", lambda cmd: f"/bin/{cmd}")
    assert doctor.main(repo) == 0  # a warning, not a problem
    assert "! action profiles:" in capsys.readouterr().out

    config.write_text(config.read_text().replace("'gone'", "'deep'").replace("'typo'", "'deep'"))
    ProjectTrust.check(repo).trust(repo)
    checks = by_name(doctor.run_checks(repo, runner=runner(HEALTHY), which=lambda c: f"/bin/{c}"))
    assert "action profiles" not in checks


# ---------------------------------------------------------------- capabilities


FIXTURES = Path(__file__).with_name("fixtures")
REAL_HELP = (FIXTURES / "project-sandbox-0.4.1-help.txt").read_text()
REAL_DOCTOR = (FIXTURES / "project-sandbox-0.4.1-doctor.json").read_text()
DISCOVERING = {
    "jj --version": (0, "jj 0.46.0"),
    "project-sandbox --version": (0, "project-sandbox 0.4.1"),
    "project-sandbox --help": (0, REAL_HELP),
    "project-sandbox doctor --json": (0, REAL_DOCTOR),
}


def caps(**fields: Any) -> capabilities.Capabilities:
    base = capabilities.Capabilities(
        command=("project-sandbox",),
        version=capabilities.installed_version(),
        discovered_at=datetime.now(UTC),
        source="probe",
        agents=("claude", "codex", "opencode", "pi", "copilot", "bash"),
        vcs_modes=("full", "read-only", "hidden"),
        runtimes={
            "auto": capabilities.RuntimeStatus("usable", "selects docker"),
            "docker": capabilities.RuntimeStatus("usable", "29; daemon reachable"),
            "podman": capabilities.RuntimeStatus(
                "unavailable", "not ready", "podman machine start"
            ),
        },
        runtime_source="doctor",
        auto_selects="docker",
        help_ok=True,
        mount=True,
    )
    return dataclasses.replace(base, **fields)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A host home with Claude configured and nothing else."""
    path = tmp_path / "home"
    (path / ".claude").mkdir(parents=True)
    return path


@pytest.fixture
def configured_agent_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make Claude and Codex available to doctor through a temporary home."""
    (home / ".codex").mkdir()
    monkeypatch.setenv("HOME", str(home))


def problems(checks: list[doctor.Check]) -> dict[str, doctor.Check]:
    return {c.name: c for c in checks if c.status != "ok" or c.hint}


def test_discovered_capabilities_are_reported(repo: Path) -> None:
    checks = by_name(
        doctor.run_checks(repo, runner=runner(DISCOVERING), which=lambda c: f"/bin/{c}")
    )
    assert checks["launcher capabilities"].status == "ok"
    assert {"agent claude", "agent copilot", "agent bash"} <= set(checks)
    assert checks["vcs access"].detail == "full, read-only, hidden"
    assert "docker usable" in checks["runtimes"].detail
    assert "auto selects docker" in checks["runtimes"].detail
    assert "container runtime" not in checks  # runtime status came from doctor --json
    cached = capabilities.cached(["project-sandbox"], repo)
    assert cached.discovered  # doctor saved the snapshot for the TUI


def test_failed_discovery_warns_and_uses_builtin_lists(repo: Path) -> None:
    responses = {**HEALTHY, "project-sandbox --help": (2, "boom")}
    checks = by_name(
        doctor.run_checks(
            repo, runner=runner(responses), which=lambda c: c if c == "docker" else None
        )
    )
    assert checks["launcher capabilities"].status == "warn"
    assert "built-in lists" in checks["launcher capabilities"].detail
    assert "container runtime" in checks  # fallback runtime probe


def test_help_runs_once(repo: Path) -> None:
    calls: list[str] = []
    answer = runner(DISCOVERING)

    def counting(argv: list[str], timeout: float) -> tuple[int, str]:
        calls.append(" ".join(argv))
        return answer(argv, timeout)

    doctor.run_checks(repo, runner=counting, which=lambda c: f"/bin/{c}")
    assert calls.count("project-sandbox --help") == 1


def test_doctor_probe_timeout_still_reports_other_checks(repo: Path) -> None:
    responses = {
        **DISCOVERING,
        "project-sandbox doctor --json": (124, "timed out after 60s"),
        "docker info": (0, "Server Version: 27"),
    }
    checks = doctor.run_checks(
        repo, runner=runner(responses), which=lambda c: "/bin/docker" if c == "docker" else None
    )
    named = by_name(checks)
    warnings = [c for c in checks if c.name == "launcher capabilities" and c.status == "warn"]
    assert any("timed out" in c.detail for c in warnings)
    assert named["container runtime"].status == "ok"  # check_runtime fallback
    assert "comparisons to main" in named and "agent claude" in named


def test_several_problems_reported_with_keys(home: Path) -> None:
    cfg = Config()
    cfg.profiles = {
        "review": AgentProfile(agent="claude", extra_args=["--vcs-access=none"]),
        "old": AgentProfile(agent="gpt"),
    }
    found = problems(doctor.check_agent_selections(cfg, caps(), home=home))
    assert found["profiles.review.extra_args"].status == "fail"
    assert "full, read-only, hidden" in found["profiles.review.extra_args"].detail
    assert found["profiles.old.agent"].status == "fail"
    assert "not supported" in found["profiles.old.agent"].detail


def test_agent_unavailable_on_host(home: Path) -> None:
    cfg = Config()
    cfg.defaults.agent = "codex"
    found = problems(doctor.check_agent_selections(cfg, caps(), home=home))
    assert found["defaults.agent"].status == "fail"
    assert ".codex" in found["defaults.agent"].detail


def test_in_sandbox_login_makes_agent_available(home: Path) -> None:
    cfg = Config()
    cfg.profiles = {"c": AgentProfile(agent="codex", extra_args=["--no-forward-credentials"])}
    assert "profiles.c.agent" not in problems(doctor.check_agent_selections(cfg, caps(), home=home))
    cfg.profiles = {"c": AgentProfile(agent="codex")}
    cfg.sandbox.no_forward_credentials = True
    assert "profiles.c.agent" not in problems(doctor.check_agent_selections(cfg, caps(), home=home))


def test_gateway_profile_without_model(home: Path) -> None:
    cfg = Config()
    cfg.network.agent_proxy = "http://127.0.0.1:4000/v1"
    cfg.profiles = {"local": AgentProfile(agent="opencode", gateway=True)}
    found = problems(doctor.check_agent_selections(cfg, caps(), home=home))
    assert found["profiles.local.model"].status == "fail"
    assert "requires a model" in found["profiles.local.model"].detail


def test_gateway_with_unsupported_agent(home: Path) -> None:
    cfg = Config()
    cfg.network.agent_proxy = "http://127.0.0.1:4000/v1"
    cfg.profiles = {"c": AgentProfile(agent="claude", model="opus", gateway=True)}
    found = problems(doctor.check_agent_selections(cfg, caps(), home=home))
    assert found["profiles.c.gateway"].status == "fail"


def test_pi_unavailable_unless_it_uses_the_gateway(home: Path) -> None:
    cfg = Config()
    cfg.network.agent_proxy = "http://127.0.0.1:4000/v1"
    cfg.profiles = {
        "direct": AgentProfile(agent="pi", model="sonnet"),
        "via": AgentProfile(agent="pi", model="gpt-5-mini", gateway=True),
    }
    found = problems(doctor.check_agent_selections(cfg, caps(), home=home))
    assert found["profiles.direct.agent"].status == "fail"
    assert "gateway = true" in found["profiles.direct.agent"].hint
    assert "profiles.via.agent" not in found
    assert "agent gateway" not in found  # used by a profile


def test_unused_gateway_is_reported(home: Path) -> None:
    cfg = Config()
    cfg.network.agent_proxy = "http://127.0.0.1:4000/v1"
    found = problems(doctor.check_agent_selections(cfg, caps(), home=home))
    assert "no configured selection uses it" in found["agent gateway"].detail
    assert found["agent gateway"].status == "ok"


def test_doctor_checks_persona_and_model_only_actions(home: Path) -> None:
    cfg = Config()
    cfg.catalogue.default_provider = "copilot-anthropic"
    cfg.actions["debug"] = Action("debug", persona="debugger")
    cfg.defaults.agent = "missing-agent"
    cfg.actions["check"] = Action("check", model="model")
    found = problems(doctor.check_agent_selections(cfg, caps(), home=home))
    assert "frontier" in found["actions.debug.persona"].detail
    assert "missing-agent" in found["actions.check.agent"].detail
    cfg.catalogue.default_provider = "gateway-anthropic"
    cfg.network.agent_proxy = "http://127.0.0.1:4000/v1"
    assert "agent gateway" not in problems(doctor.check_agent_selections(cfg, caps(), home=home))


def test_configured_runtime_not_ready(home: Path) -> None:
    cfg = Config()
    cfg.sandbox.runtime = "podman"
    found = problems(doctor.check_agent_selections(cfg, caps(), home=home))
    check = found["sandbox.runtime"]
    assert (check.status, check.hint) == ("fail", "podman machine start")
    assert "not ready" in check.detail


def test_unknown_runtime_value(home: Path) -> None:
    cfg = Config()
    cfg.sandbox.runtime = "lxc"
    found = problems(doctor.check_agent_selections(cfg, caps(), home=home))
    assert "not supported" in found["sandbox.runtime"].detail


def test_unknown_codex_model_warns_but_gateway_models_do_not(home: Path) -> None:
    store = models_mod.Store(agents={"codex": [models_mod.ModelInfo("gpt-5.5")]})
    cfg = Config()
    cfg.network.agent_proxy = "http://127.0.0.1:4000/v1"
    cfg.profiles = {
        "cx": AgentProfile(
            agent="codex", model="gpt-9-imaginary", extra_args=["--no-forward-credentials"]
        ),
        "known": AgentProfile(
            agent="codex", model="gpt-5.5", extra_args=["--no-forward-credentials"]
        ),
        "pi": AgentProfile(agent="pi", model="anything-goes", gateway=True),
    }
    found = problems(doctor.check_agent_selections(cfg, caps(), store, home=home))
    assert found["profiles.cx.model"].status == "warn"
    assert ":update-models" in found["profiles.cx.model"].hint
    assert "profiles.known.model" not in found and "profiles.pi.model" not in found


def test_fast_on_unsupported_agent_warns(home: Path) -> None:
    cfg = Config()
    cfg.actions = {"go": Action(prompt="x", agent="bash")}
    cfg.profiles = {"p": AgentProfile(agent="bash", fast=True)}
    found = problems(doctor.check_agent_selections(cfg, caps(), home=home))
    assert found["profiles.p.fast"].status == "warn"


def test_selections_include_actions_and_commit_review(home: Path) -> None:
    cfg = Config()
    cfg.actions = {"go": Action(prompt="x", agent="gpt")}
    found = problems(doctor.check_agent_selections(cfg, caps(agents=("claude", "bash")), home=home))
    assert found["actions.go.agent"].status == "fail"
    assert "commit review (commit-reviewer on Codex).agent" in found  # built-in persona


def test_fallback_snapshot_reports_nothing_unsupported(home: Path) -> None:
    cfg = Config()
    cfg.defaults.agent = "newagent"
    found = problems(doctor.check_agent_selections(cfg, capabilities.fallback(), home=home))
    assert "defaults.agent" not in found  # unknown support and availability never fail


# ---------------------------------------------------------------- security levels


def discovered(**fields: Any) -> capabilities.Capabilities:
    defaults: dict[str, Any] = {
        "command": ("project-sandbox",),
        "version": "0.4.1",
        "discovered_at": datetime.now(UTC),
        "source": "probe",
        "agents": ("claude", "codex", "pi", "copilot", "bash"),
        "vcs_modes": ("full", "read-only", "hidden"),
        "egress_modes": ("firewall", "sidecar"),
    }
    return capabilities.Capabilities(**{**defaults, **fields})


def test_level_conflicts_per_config_key(egress_policy: Path) -> None:
    cfg = Config()
    cfg.security.level = 3
    cfg.sandbox.extra_args = ["--no-firewall", "--memory=8g"]
    cfg.profiles = {
        "web": AgentProfile(agent="claude", extra_args=["--extra-domain=example.org"]),
        "plain": AgentProfile(agent="claude", extra_args=["--memory=16g"]),
    }
    cfg.network.no_firewall = True
    checks = by_name(doctor.check_security(cfg, discovered()))
    shared = checks["sandbox.extra_args"]
    assert shared.status == "fail" and "--no-firewall" in shared.detail
    assert "level 1 for the jobs that need it" in shared.hint
    web = checks["profiles.web.extra_args"]
    assert web.status == "fail" and "network.extra_domains with level 2" in web.hint
    assert "profiles.plain.extra_args" not in checks
    ignored = checks["network.no_firewall"]
    assert (
        ignored.status == "warn" and "ignored while security levels are in effect" in ignored.detail
    )


def test_no_level_conflicts_without_security_levels(egress_policy: Path) -> None:
    cfg = Config()
    cfg.sandbox.extra_args = ["--no-firewall"]
    cfg.network.no_firewall = True
    checks = by_name(doctor.check_security(cfg, discovered()))
    assert "sandbox.extra_args" not in checks and "network.no_firewall" not in checks


def test_sidecar_policy_present(egress_policy: Path) -> None:
    check = by_name(doctor.check_security(Config(), discovered()))["egress backend"]
    assert check.status == "ok"
    assert "sidecar" in check.detail and "allows 2 hosts" in check.detail
    assert "api.openai.com, api.anthropic.com" in check.detail
    assert "shared by all projects on the runtime" in check.hint
    before = egress_policy.read_text()
    doctor.check_security(Config(), discovered())
    assert egress_policy.read_text() == before  # never modified


def test_sidecar_policy_with_nested_tables(egress_policy: Path) -> None:
    egress_policy.write_text(
        '[[rules]]\nhosts = ["a.example"]\n[policy]\ndomains = ["b.example"]\n'
    )
    assert doctor.egress_hosts(egress_policy) == ["a.example", "b.example"]
    egress_policy.write_text("not toml [")
    check = by_name(doctor.check_security(Config(), discovered()))["egress backend"]
    assert check.status == "ok" and str(egress_policy) in check.detail


def test_sidecar_default_policy() -> None:
    check = by_name(doctor.check_security(Config(), discovered()))["egress backend"]
    assert check.status == "ok" and "egress.toml does not exist" in check.detail
    assert "default settings" in check.detail
    assert "GitHub Copilot" in check.hint and "allow access" in check.hint
    assert "project-sandbox-egress" in check.hint


def test_egress_backend_not_offered_by_the_launcher() -> None:
    cfg = Config()
    check = by_name(doctor.check_security(cfg, discovered(egress_modes=("firewall",))))
    assert check["egress backend"].status == "fail"
    assert "not supported" in check["egress backend"].detail
    cfg.security.egress = "firewall"
    check = by_name(doctor.check_security(cfg, discovered(egress_modes=("firewall",))))
    assert check["egress backend"].status == "ok"


def test_provider_availability(egress_policy: Path) -> None:
    cfg = Config()
    cfg.catalogue.default_provider = "gateway-anthropic"
    checks = by_name(doctor.check_security(cfg, discovered()))
    assert (
        "Gateway/Anthropic unavailable: no agent gateway is configured"
        in checks["providers"].detail
    )
    failure = checks["catalogue.default_provider"]
    assert failure.status == "fail" and "no agent gateway is configured" in failure.detail
    cfg.catalogue.default_provider = ""
    cfg.commit_review.provider = "gateway-openai"
    checks = by_name(doctor.check_security(cfg, discovered()))
    assert checks["commit_review.provider"].status == "fail"
    cfg.network.agent_proxy = "http://127.0.0.1:4000/v1"
    checks = by_name(doctor.check_security(cfg, discovered()))
    assert "commit_review.provider" not in checks


def test_default_provider_allows_configured_in_sandbox_login(home: Path) -> None:
    cfg = Config()
    cfg.catalogue.default_provider = "codex"
    assert "catalogue.default_provider" in by_name(doctor.check_security(cfg, caps(), home=home))
    cfg.sandbox.no_forward_credentials = True
    assert "catalogue.default_provider" not in by_name(
        doctor.check_security(cfg, caps(), home=home)
    )


def test_workflow_file_trust_and_local_persona(tmp_path: Path) -> None:
    from project_sandbox_tui import config
    from project_sandbox_tui.doctor import check_config

    path = config.workflow_dir(tmp_path) / "spec.toml"
    path.parent.mkdir(parents=True)
    path.write_text("""
[workflow]
planning_action = "plan"
steps = ["plan"]
[actions.plan]
prompt = "hi"
persona = "house-style"
""")
    cfg = config.load(tmp_path)
    assert any("not trusted" in check.detail for check in check_config(cfg, tmp_path))
    project = tmp_path / config.PROJECT_CONFIG_NAME
    project.write_text('[personas.house-style]\ntier = "high"\n')
    config.ProjectTrust.check(tmp_path).trust()
    config.WorkflowTrust.scan(tmp_path)[0][0].trust()
    cfg = config.load(tmp_path)
    assert any(
        "locally configured persona house-style" in check.detail
        for check in check_config(cfg, tmp_path)
    )


def test_imported_workflow_source_checksum(tmp_path: Path) -> None:
    import hashlib

    from project_sandbox_tui import config
    from project_sandbox_tui.doctor import check_config

    body = '[workflow]\nplanning_action = "plan"\nsteps = ["plan"]\n[actions.plan]\nprompt = "hi"\n'
    digest = hashlib.sha256(body.encode()).hexdigest()
    path = config.workflow_dir(tmp_path) / "spec.toml"
    path.parent.mkdir(parents=True)
    path.write_text(body + f'[source]\nsha256 = "{digest}"\n')
    assert any(
        "matches source" in check.detail for check in check_config(config.load(tmp_path), tmp_path)
    )
    path.write_text(path.read_text().replace('"hi"', '"edited"'))
    assert any(
        "edited since import" in check.detail
        for check in check_config(config.load(tmp_path), tmp_path)
    )


def test_workflow_file_checks_are_reported_once(tmp_path: Path) -> None:
    from project_sandbox_tui import config
    from project_sandbox_tui.doctor import check_config

    directory = config.workflow_dir(tmp_path)
    directory.mkdir(parents=True)
    broken = directory / "broken.toml"
    broken.write_text('[workflow]\nplanning_action = "missing"\nsteps = ["missing"]\n')
    shared = directory / "shared.toml"
    shared.write_text("""
[workflow]
planning_action = "plan"
steps = ["review"]
[actions.plan]
prompt = "hi"
persona = "house-style"
[actions.review]
prompt = "hi"
persona = "house-style"
""")
    (directory / "pending.toml").write_text(shared.read_text())
    (directory / "unreadable.toml").write_bytes(b"\xff")
    project = tmp_path / config.PROJECT_CONFIG_NAME
    project.write_text('[personas.house-style]\ntier = "high"\n')
    config.ProjectTrust.check(tmp_path).trust()
    for file in config.WorkflowTrust.scan(tmp_path)[0]:
        if file.path.stem in ("broken", "shared"):
            file.trust()
    checks = check_config(config.load(tmp_path), tmp_path)
    details = [check.detail for check in checks]
    assert len(details) == len(set(details)), details
    assert not any(check.name == "project config" for check in checks)
    assert f"{broken} is trusted" not in details
    assert sum(str(broken) in detail for detail in details) == 1
    assert f"{shared} is trusted" in details
    pending = next(c for c in checks if "pending.toml is not trusted" in c.detail)
    assert pending.status == "warn" and pending.hint == "sbx-tui --trust-config"
    assert any("unreadable.toml" in detail and "ignored" in detail for detail in details)
    assert not any("unreadable.toml is trusted" in detail for detail in details)
    assert details.count("workflow shared depends on locally configured persona house-style") == 1


def test_doctor_survives_unreadable_workflow_files(tmp_path: Path) -> None:
    from project_sandbox_tui import config
    from project_sandbox_tui.doctor import check_config

    path = config.workflow_dir(tmp_path) / "spec.toml"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe")
    checks = check_config(config.load(tmp_path), tmp_path)
    assert any(check.status == "warn" and "spec.toml" in check.detail for check in checks)
