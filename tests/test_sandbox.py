from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from project_sandbox_tui import sandbox
from project_sandbox_tui.config import Config, ConfigError, Network, Project, Sandbox
from project_sandbox_tui.sandbox import (
    LaunchSpec,
    RunPaths,
    build_argv,
    read_sessions,
    running_by_branch,
)
from project_sandbox_tui.vcs import WORKING_COPY, workspace_path


def make_cfg() -> Config:
    cfg = Config()
    cfg.project = Project(base_image="python:3.12-slim", worktree_dir="../wt")
    cfg.network = Network(
        internet_proxy="http://proxy:3128",
        agent_proxy="http://agent:4000",
        agent_proxy_key_env="PROXY_KEY",
        ca_certs=["certs/root.pem"],
        extra_domains=["pypi.org"],
        allow_github=True,
        no_firewall=True,
    )
    cfg.sandbox = Sandbox(
        command="uvx project-sandbox",
        runtime="docker",
        memory="16g",
        cpus=6,
        no_forward_credentials=True,
        api_key_env=["ANTHROPIC_API_KEY"],
        api_key_env_file=["keys.env"],
        extra_args=["--verbose"],
    )
    return cfg


def test_interactive_argv(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    spec = LaunchSpec(branch="feat/x", agent="claude", model="opus", effort="high")
    assert build_argv(make_cfg(), repo, spec) == [
        "uvx",
        "project-sandbox",
        str(repo.resolve()),
        "python:3.12-slim",
        "--runtime=docker",
        "--memory=16g",
        "--cpus=6",
        "--branch=feat/x",
        f"--worktree-dir={repo / '../wt'}",
        "--keep-workspace",
        "--agent=claude",
        "--model=opus",
        "--effort=high",
        "--internet-proxy=http://proxy:3128",
        "--agent-proxy=http://agent:4000",
        "--agent-proxy-key-env=PROXY_KEY",
        f"--ca-cert={repo / 'certs/root.pem'}",
        "--extra-domain=pypi.org",
        "--allow-github",
        "--no-firewall",
        "--no-forward-credentials",
        "--api-key-env=ANTHROPIC_API_KEY",
        f"--api-key-env-file={repo / 'keys.env'}",
        "--verbose",
    ]


def test_values_starting_with_a_dash_stay_values(tmp_path: Path) -> None:
    spec = LaunchSpec(branch="-x", agent="bash", mode="background", prompt_text="--help")
    paths = RunPaths(log=tmp_path / "a.log", summary=tmp_path / "a.json", stdout=tmp_path / "a.out")
    argv = build_argv(make_cfg(), tmp_path, spec, paths)
    assert "--branch=-x" in argv and "--prompt-text=--help" in argv


@pytest.mark.parametrize("agent", ["claude", "codex", "opencode", "pi", "bash"])
@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("fast", [False, True])
def test_fast_mode_argv(tmp_path: Path, agent: str, background: bool, fast: bool) -> None:
    spec = LaunchSpec(
        branch="b",
        agent=agent,
        fast=fast,
        mode="background" if background else "interactive",
        prompt_text="review",
    )
    paths = RunPaths(log=tmp_path / "a.log", summary=tmp_path / "a.json", stdout=tmp_path / "a.out")
    argv = build_argv(make_cfg(), tmp_path, spec, paths if background else None)
    expected = fast and agent in ("claude", "codex")
    assert ("--fast" in argv) == expected
    assert ("fast" in spec.label()) == expected
    assert LaunchSpec.from_dict(spec.to_dict()) == spec


def test_old_launch_spec_defaults_fast_mode_to_off() -> None:
    assert not LaunchSpec.from_dict({"branch": "b", "agent": "codex"}).fast


def test_background_argv(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    cfg = Config()
    cfg.project = Project(build="python-uv", build_version="3.12", keep_workspace=False)
    paths = RunPaths(log=tmp_path / "a.log", summary=tmp_path / "a.json", stdout=tmp_path / "a.out")
    spec = LaunchSpec(
        branch="b",
        agent="bash",
        model="ignored",
        effort="max",
        mode="background",
        prompt_text="do it",
        timeout=60,
    )
    argv = build_argv(cfg, repo, spec, paths)
    assert argv[:4] == ["project-sandbox", str(repo.resolve()), "--python-uv", "--python=3.12"]
    assert not [a for a in argv if a.startswith(("--model", "--effort"))]  # bash takes neither
    assert "--keep-workspace" not in argv
    assert argv[-4:] == [
        "--prompt-text=do it",
        f"--log={paths.log}",
        f"--json-summary={paths.summary}",
        "--timeout=60",
    ]


def test_dockerfile_source(tmp_path: Path) -> None:
    cfg = Config()
    cfg.project = Project(dockerfile="docker/Dockerfile", docker_context=".")
    argv = build_argv(cfg, tmp_path, LaunchSpec(branch="b", agent="codex"))
    i = argv.index(f"--dockerfile={tmp_path / 'docker/Dockerfile'}")
    assert argv[i + 1] == f"--docker-context={tmp_path / '.'}"


def test_requires_image_and_prompt(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no image configured"):
        build_argv(Config(), tmp_path, LaunchSpec(branch="b", agent="claude"))
    cfg = make_cfg()
    spec = LaunchSpec(branch="b", agent="claude", mode="background", prompt_text="  ")
    with pytest.raises(ConfigError, match="prompt"):
        build_argv(cfg, tmp_path, spec, RunPaths(tmp_path, tmp_path, tmp_path))
    spec.prompt_text = "go"
    with pytest.raises(ValueError, match="run paths"):
        build_argv(cfg, tmp_path, spec)


def test_session_matching(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    ws = workspace_path(repo, "feat/x", None)

    def rec(sid: str, status: str, pid: int, workspace: Path) -> None:
        (sessions / f"{sid}.json").write_text(
            json.dumps(
                {
                    "session_id": sid,
                    "status": status,
                    "pid": pid,
                    "workspace_path": str(workspace),
                    "container_name": f"c-{sid}",
                }
            )
        )

    rec("live", "running", os.getpid(), ws)
    rec("dead", "running", 999_999_999, workspace_path(repo, "other", None))
    rec("done", "completed", os.getpid(), workspace_path(repo, "main", None))
    (sessions / "garbage.json").write_text("{not json")

    records = read_sessions(sessions)
    statuses = {r.session_id: r.status for r in records}
    assert statuses == {"live": "running", "dead": "orphaned", "done": "completed"}
    matched = running_by_branch(records, repo, ["main", "feat/x", "other"], None)
    assert list(matched) == ["feat/x"]
    assert matched["feat/x"].container_name == "c-live"


def test_working_copy_argv_omits_workspace_flags(tmp_path: Path) -> None:
    spec = LaunchSpec(branch=WORKING_COPY, agent="claude")
    assert spec.main_checkout
    argv = build_argv(make_cfg(), tmp_path, spec)
    for flag in ("--branch", "--worktree-dir", "--keep-workspace"):
        assert not [a for a in argv if a.startswith(flag)]
    assert LaunchSpec.from_dict(spec.to_dict()).main_checkout
    assert not LaunchSpec(branch="main", agent="claude").main_checkout


def test_main_checkout_sessions_map_to_working_copy(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "s.json").write_text(
        json.dumps(
            {
                "session_id": "s",
                "status": "running",
                "pid": os.getpid(),
                "workspace_path": str(repo),
            }
        )
    )
    records = read_sessions(sessions)
    assert list(running_by_branch(records, repo, ["main"], None)) == [WORKING_COPY]


@pytest.mark.parametrize(
    ("build", "version", "flags"),
    [("rust-cargo", "1.90", ["--rust-cargo", "--rust=1.90"]), ("node-npm", "", ["--node-npm"])],
)
def test_generated_builds(tmp_path: Path, build: str, version: str, flags: list[str]) -> None:
    cfg = Config(project=Project(build=build, build_version=version))
    assert build_argv(cfg, tmp_path, LaunchSpec("@", "bash")) == [
        "project-sandbox",
        str(tmp_path),
        *flags,
        "--agent=bash",
    ]


@pytest.mark.parametrize("pid", [None, "123", True, False, 0, -1])
def test_invalid_pid_is_not_probed(pid: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    kill = Mock()
    monkeypatch.setattr(sandbox.os, "kill", kill)
    assert not sandbox.pid_alive(pid)
    kill.assert_not_called()


@pytest.mark.parametrize(
    ("error", "alive"),
    [
        (None, True),
        (PermissionError(), True),
        (ProcessLookupError(), False),
        (OverflowError(), False),
    ],
)
def test_pid_probe_handles_process_errors(
    monkeypatch: pytest.MonkeyPatch, error: Exception | None, alive: bool
) -> None:
    from unittest.mock import Mock

    kill = Mock(side_effect=error)
    monkeypatch.setattr(sandbox.os, "kill", kill)
    assert sandbox.pid_alive(123) is alive
    kill.assert_called_once_with(123, 0)


def test_session_reader_skips_corruption_and_normalizes_invalid_pids(tmp_path: Path) -> None:
    for name, payload in {"bad": b"{", "list": b"[]", "encoding": b"\xff"}.items():
        (tmp_path / f"{name}.json").write_bytes(payload)
    for name, pid in [("boolean", True), ("string", "123"), ("missing", None)]:
        (tmp_path / f"{name}.json").write_text(json.dumps({"status": "running", "pid": pid}))
    records = read_sessions(tmp_path)
    assert {r.session_id for r in records} == {"boolean", "string", "missing"}
    assert all(r.status == "orphaned" and r.pid is None for r in records)


def test_sessions_match_custom_workspace_paths_through_symlinks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    root = tmp_path / "custom"
    workspace = workspace_path(repo, "feat/x", root)
    workspace.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)

    def record(path: Path, status: str = "running") -> sandbox.SessionRecord:
        return sandbox.SessionRecord("id", "container", "", str(path), "bash", status, 1, "")

    matching = record(alias / workspace.name)
    records = [matching, record(tmp_path / "unrelated"), record(repo, "completed")]
    assert running_by_branch(records, repo, ["feat/x"], root) == {"feat/x": matching}


@pytest.mark.parametrize(
    ("system", "available", "expected"),
    [
        ("Darwin", {"container", "docker", "podman"}, "container"),
        ("Darwin", {"docker", "podman"}, "docker"),
        ("Linux", {"container", "docker", "podman"}, "docker"),
        ("Linux", {"podman"}, "podman"),
        ("Linux", set(), None),
    ],
)
def test_runtime_auto_selection(
    monkeypatch: pytest.MonkeyPatch, system: str, available: set[str], expected: str | None
) -> None:
    monkeypatch.setattr(sandbox.platform, "system", lambda: system)
    monkeypatch.setattr(sandbox.shutil, "which", lambda cmd: cmd if cmd in available else None)
    assert sandbox.runtime_cli("auto") == expected
