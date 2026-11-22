"""Security levels: config, compilation to launcher flags and launch checks."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from project_sandbox_tui import capabilities as caps_mod
from project_sandbox_tui import config as c
from project_sandbox_tui import security
from project_sandbox_tui.jobs import JobManager
from project_sandbox_tui.sandbox import LaunchSpec, build_argv
from tests.test_capabilities import HELP, REAL, fake_runner


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def trusted(repo: Path, text: str) -> None:
    write(c.project_config_path(repo), text)
    c.ProjectTrust.check(repo).trust(repo)


def cfg_with(**data: object) -> c.Config:
    cfg = c.from_dict(data)  # type: ignore[arg-type]
    cfg.validate()
    return cfg


def network_flags(argv: list[str]) -> list[str]:
    managed = security.MANAGED_FLAGS
    return [a for a in argv if a.partition("=")[0] in managed]


def argv_at(cfg: c.Config, tmp_path: Path, level: int | None, agent: str = "claude") -> list[str]:
    if not cfg.has_image_source():
        cfg.project.base_image = "python:3.12-slim"
    return build_argv(cfg, tmp_path, LaunchSpec("feat", agent, level=level))


# ---------------------------------------------------------------- user config


@pytest.mark.parametrize("key", ["internet_proxy", "ca_certs", "build_proxy", "build_ca_certs"])
def test_corporate_settings_in_project_config_fail_with_a_level(tmp_path: Path, key: str) -> None:
    value = '"http://proxy:3128"' if "proxy" in key else '["corp.pem"]'
    trusted(tmp_path, f"[security]\nlevel = 3\n[network]\n{key} = {value}\n")
    with pytest.raises(c.ConfigError, match=f"network.{key} .*global config"):
        c.load(tmp_path)


def test_corporate_settings_in_project_config_without_level_still_load(tmp_path: Path) -> None:
    trusted(tmp_path, '[network]\ninternet_proxy = "http://proxy:3128"\n')
    assert c.load(tmp_path).network.internet_proxy == "http://proxy:3128"


def test_level_from_global_config_also_makes_project_proxy_an_error(tmp_path: Path) -> None:
    global_path = write(tmp_path / "g.toml", "[security]\nlevel = 2\n")
    trusted(tmp_path / "repo", '[network]\ninternet_proxy = "http://p:1"\n')
    with pytest.raises(c.ConfigError, match="network.internet_proxy"):
        c.load(tmp_path / "repo", global_path)


def test_egress_is_always_global_only(tmp_path: Path) -> None:
    trusted(tmp_path, '[security]\negress = "firewall"\n')
    with pytest.raises(c.ConfigError, match="security.egress .*global config"):
        c.load(tmp_path)
    global_path = write(tmp_path / "g.toml", '[security]\negress = "firewall"\n')
    trusted(tmp_path / "repo", "[security]\nlevel = 3\n")
    cfg = c.load(tmp_path / "repo", global_path)
    assert (cfg.security.level, cfg.security.egress) == (3, "firewall")


@pytest.mark.parametrize(
    ("security_table", "message"),
    [
        ({"level": 6}, "security.level must be between 1 and 5"),
        ({"level": 0}, "security.level must be between 1 and 5"),
        ({"level": -1}, "security.level must be between 1 and 5"),
        ({"egress": "squid"}, "security.egress must be one of sidecar, firewall"),
    ],
)
def test_invalid_security(security_table: dict[str, object], message: str) -> None:
    with pytest.raises(c.ConfigError, match=message):
        cfg_with(security=security_table)


def test_build_proxy_from_user_config_at_any_level(tmp_path: Path) -> None:
    global_path = write(
        tmp_path / "g.toml",
        '[network]\nbuild_proxy = "http://proxy:3128"\nbuild_ca_certs = ["corp.pem"]\n'
        'build_no_proxy = ["internal.example,.corp"]\n',
    )
    repo = tmp_path / "repo"
    trusted(repo, "[security]\nlevel = 3\n")
    cfg = c.load(repo, global_path)
    for level in (None, 1, 3, 5):
        argv = argv_at(cfg, repo, level)
        assert "--build-proxy=http://proxy:3128" in argv
        assert f"--build-ca-cert={repo / 'corp.pem'}" in argv
        assert "--build-no-proxy=internal.example,.corp" in argv


# ---------------------------------------------------------------- compilation


@pytest.mark.parametrize(
    ("level", "flags"),
    [
        (1, ["--no-firewall"]),
        (2, ["--egress=firewall"]),
        (3, ["--egress=sidecar"]),
        (4, ["--egress=sidecar", "--vcs-access=read-only"]),
        (5, ["--egress=sidecar", "--vcs-access=hidden"]),
    ],
)
def test_levels_compile_to_exactly_their_flags(
    tmp_path: Path, level: int, flags: list[str]
) -> None:
    cfg = cfg_with(
        project={"base_image": "x"},
        network={"extra_domains": ["example.org"], "allow_github": True, "no_firewall": True},
    )
    argv = argv_at(cfg, tmp_path, level)
    if level == 2:
        flags = [*flags, "--extra-domain=example.org", "--allow-github"]
    assert network_flags(argv) == flags


def test_firewall_backend(tmp_path: Path) -> None:
    cfg = cfg_with(security={"egress": "firewall"}, network={"extra_domains": ["example.org"]})
    assert network_flags(argv_at(cfg, tmp_path, 3)) == ["--egress=firewall"]


@pytest.mark.parametrize(
    ("project", "domains"),
    [
        ({"build": "python-uv"}, ["pypi.org", "files.pythonhosted.org"]),
        ({"build": "rust-cargo"}, ["crates.io", "index.crates.io", "static.crates.io"]),
        ({"build": "node-npm"}, ["registry.npmjs.org"]),
        ({"dockerfile": "Dockerfile"}, []),
        ({"base_image": "python:3.12-slim"}, []),
    ],
)
def test_level_two_sources_per_build(
    project: dict[str, str], domains: list[str], tmp_path: Path
) -> None:
    cfg = cfg_with(project=project)
    assert security.level_flags(cfg, 2) == [
        "--egress=firewall",
        *(f"--extra-domain={d}" for d in domains),
    ]


def test_python_project_at_level_two(tmp_path: Path) -> None:
    cfg = cfg_with(project={"build": "python-uv"}, network={"extra_domains": ["example.org"]})
    assert network_flags(argv_at(cfg, tmp_path, 2)) == [
        "--egress=firewall",
        "--extra-domain=pypi.org",
        "--extra-domain=files.pythonhosted.org",
        "--extra-domain=example.org",
    ]


def test_level_two_behind_an_internet_proxy(tmp_path: Path) -> None:
    cfg = cfg_with(
        project={"build": "python-uv"},
        network={"internet_proxy": "http://proxy:3128", "extra_domains": ["example.org"]},
    )
    argv = argv_at(cfg, tmp_path, 2)
    assert "--internet-proxy=http://proxy:3128" in argv
    assert not any(a.startswith("--extra-domain") for a in argv)
    assert "proxy's policy decides" in security.level_hint(cfg, 2)


def test_copilot_hint_mentions_github() -> None:
    hint = security.level_hint(c.Config(), 3, "copilot")
    assert "Copilot also allows GitHub hosts" in hint
    assert "GitHub" not in security.level_hint(c.Config(), 3, "codex")


def test_no_security_section_keeps_legacy_flags(tmp_path: Path) -> None:
    cfg = cfg_with(network={"no_firewall": True, "extra_domains": ["a.org"], "allow_github": True})
    assert network_flags(argv_at(cfg, tmp_path, None)) == [
        "--extra-domain=a.org",
        "--allow-github",
        "--no-firewall",
    ]


def test_project_default_applies_to_specs_without_a_level(tmp_path: Path) -> None:
    cfg = cfg_with(security={"level": 3}, network={"no_firewall": True})
    old = LaunchSpec.from_dict({"branch": "feat", "agent": "claude"})
    assert old.level is None and security.effective_level(cfg, old) == 3
    cfg.project.base_image = "x"
    assert network_flags(build_argv(cfg, tmp_path, old)) == ["--egress=sidecar"]


def test_launch_spec_round_trips_selection_fields() -> None:
    spec = LaunchSpec("feat", "codex", persona="reviewer", provider="codex", level=4)
    assert LaunchSpec.from_dict(spec.to_dict()) == spec
    assert LaunchSpec.from_dict({**spec.to_dict(), "level": 9}).level is None
    assert spec.label() == "codex/L4"


# ---------------------------------------------------------------- managed flags


@pytest.mark.parametrize(
    ("args", "found"),
    [
        (["--memory=16g"], []),
        (["--extra-domain=example.org"], ["--extra-domain"]),
        (["--extra-domain", "example.org"], ["--extra-domain"]),
        (["--no-fire"], ["--no-firewall"]),  # argparse abbreviation
        (["--vcs-access=hidden", "--egress", "sidecar"], ["--vcs-access", "--egress"]),
        (["--ca-cert=x", "--build-ca-cert=y"], ["--ca-cert", "--build-ca-cert"]),
        (["--pi-ollama"], ["--pi-ollama"]),
        (["--agent-proxy-key-env=X", "--sandbox-firewall"], []),
    ],
)
def test_managed_conflicts(args: list[str], found: list[str]) -> None:
    assert security.managed_conflicts(args) == found


def test_security_problems_name_the_setting() -> None:
    cfg = cfg_with(
        security={"level": 3},
        profiles={"web": {"agent": "claude", "extra_args": ["--extra-domain=example.org"]}},
        sandbox={"extra_args": ["--no-firewall"]},
    )
    spec = LaunchSpec("feat", "claude", profile="web", extra_args=["--extra-domain=example.org"])
    problems = security.security_problems(cfg, spec)
    assert problems[0].startswith("sandbox.extra_args contains --no-firewall")
    assert "level 1 for the jobs that need it" in problems[0]
    assert problems[1].startswith("profiles.web.extra_args contains --extra-domain")
    assert "network.extra_domains with level 2" in problems[1]
    custom = LaunchSpec("feat", "claude", extra_args=["--allow-github"])
    assert "custom CLI args" in security.security_problems(cfg, custom)[1]
    assert security.security_problems(c.Config(), spec) == []  # no level: earlier behaviour


# ---------------------------------------------------------------- launch checks


def _probe(repo: Path, cfg: c.Config, help_text: str = HELP) -> None:
    caps = caps_mod.discover(["ps"], fake_runner({**REAL, "--help": (0, help_text)}))
    snapshot = dataclasses.replace(caps, command=tuple(cfg.sandbox_command()))
    caps_mod.save(snapshot, caps_mod.cache_path(repo))


def test_conflicting_profile_is_refused_and_memory_kept(
    repo: Path, fake_cfg: Callable[..., c.Config]
) -> None:
    cfg = fake_cfg()
    cfg.security.level = 3
    cfg.profiles["web"] = c.AgentProfile(agent="claude", extra_args=["--extra-domain=example.org"])
    jobs = JobManager(repo, cfg)
    spec = LaunchSpec(
        "main", "claude", mode="background", prompt_text="x", profile="web",
        extra_args=["--extra-domain=example.org"],
    )  # fmt: skip
    with pytest.raises(c.ConfigError, match=r"profiles\.web\.extra_args.*network\.extra_domains"):
        jobs.start_background(spec, {})
    assert "main" not in jobs.jobs
    spec.extra_args = ["--memory=16g"]
    job = jobs.start_background(spec, {})
    assert "--memory=16g" in job.argv and "--egress=sidecar" in job.argv


def test_launch_at_unavailable_level_is_refused(
    repo: Path, fake_cfg: Callable[..., c.Config]
) -> None:
    cfg = fake_cfg()
    cfg.security.level = 3
    _probe(repo, cfg, HELP.replace("{firewall,sidecar}", "{firewall}"))
    jobs = JobManager(repo, cfg)
    spec = LaunchSpec("main", "claude", mode="background", prompt_text="x")
    with pytest.raises(c.ConfigError, match="security.egress = 'sidecar' .*firewall"):
        jobs.start_background(spec, {})
    spec.level = 1
    assert jobs.start_background(spec, {}).status == "running"


# ---------------------------------------------------------------- discovery


def test_egress_modes_from_help() -> None:
    caps = caps_mod.discover(["ps"], fake_runner(REAL))
    assert caps.egress_modes == ("firewall", "sidecar")


def _without_egress() -> str:
    text = HELP.replace("[--egress {firewall,sidecar}]", "")
    return text.replace("--egress {firewall,sidecar}", "")


def test_older_help_without_egress() -> None:
    caps = caps_mod.discover(["ps"], fake_runner({**REAL, "--help": (0, _without_egress())}))
    assert caps.discovered and caps.egress_modes == ()
    cfg = c.Config()
    assert caps_mod.level_problems(caps, cfg, 1) == []
    for level in (2, 3, 4, 5):
        assert "has no --egress option" in caps_mod.level_problems(caps, cfg, level)[0]


def test_egress_modes_are_cached(tmp_path: Path) -> None:
    caps = caps_mod.discover(["ps"], fake_runner(REAL))
    loaded = caps_mod.load(caps_mod.save(caps, tmp_path / "c.json"))
    assert loaded is not None and loaded.egress_modes == ("firewall", "sidecar")


def test_version_one_cache_is_stale(tmp_path: Path) -> None:
    caps = caps_mod.discover(["ps"], fake_runner(REAL))
    path = caps_mod.save(caps, tmp_path / "c.json")
    doc = json.loads(path.read_text())
    doc["version"] = 1
    del doc["egress_modes"]
    path.write_text(json.dumps(doc))
    assert caps_mod.load(path) is None
    assert not caps_mod.is_fresh(caps_mod.load(path), ["ps"])


def test_level_problems() -> None:
    caps = caps_mod.discover(["ps"], fake_runner(REAL))
    cfg = c.Config()
    assert all(caps_mod.level_problems(caps, cfg, level) == [] for level in c.LEVELS)
    firewall_only = caps_mod.discover(
        ["ps"],
        fake_runner({**REAL, "--help": (0, HELP.replace("{firewall,sidecar}", "{firewall}"))}),
    )
    assert caps_mod.level_problems(firewall_only, cfg, 2) == []
    assert "security.egress" in caps_mod.level_problems(firewall_only, cfg, 3)[0]
    cfg.security.egress = "firewall"
    assert caps_mod.level_problems(firewall_only, cfg, 3) == []
    no_vcs = HELP.replace("[--vcs-access {full,read-only,hidden}]", "").replace(
        "--vcs-access {full,read-only,hidden}", ""
    )
    caps = caps_mod.discover(["ps"], fake_runner({**REAL, "--help": (0, no_vcs)}))
    assert caps_mod.level_problems(caps, c.Config(), 3) == []
    for level in (4, 5):
        assert "has no --vcs-access option" in caps_mod.level_problems(caps, c.Config(), level)[0]


def test_fallback_discovery_restricts_no_level() -> None:
    caps = caps_mod.fallback(["ps"], "missing")
    assert all(caps_mod.level_problems(caps, c.Config(), level) == [] for level in c.LEVELS)


def test_fake_launcher_records_isolation_flags(
    repo: Path, fake_cfg: Callable[..., c.Config]
) -> None:
    cfg = fake_cfg()
    cfg.security.level = 4
    cfg.network.build_proxy = "http://proxy:3128"
    cfg.network.build_no_proxy = ["internal.example"]
    cfg.network.build_ca_certs = ["/certs/corp.pem"]
    jobs = JobManager(repo, cfg)
    job = jobs.start_background(
        LaunchSpec("main", "claude", mode="background", prompt_text="x"), {}
    )
    jobs._procs["main"].wait(timeout=30)
    summary = json.loads(Path(job.summary_path).read_text())
    assert summary["isolation"] == {
        "egress": "sidecar",
        "vcs_access": "read-only",
        "build_proxy": "http://proxy:3128",
        "build_no_proxy": ["internal.example"],
        "build_ca_certs": ["/certs/corp.pem"],
    }
