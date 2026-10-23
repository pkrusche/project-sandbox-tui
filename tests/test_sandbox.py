from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

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
        ca_certs=["certs/root.pem"],
        extra_domains=["pypi.org"],
    )
    cfg.sandbox = Sandbox(
        command="uvx project-sandbox",
        runtime="docker",
        memory="16g",
        no_forward_credentials=True,
        api_key_env=["ANTHROPIC_API_KEY"],
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
        "--branch=feat/x",
        f"--worktree-dir={repo / '../wt'}",
        "--keep-workspace",
        "--agent=claude",
        "--model=opus",
        "--effort=high",
        "--internet-proxy=http://proxy:3128",
        f"--ca-cert={repo / 'certs/root.pem'}",
        "--extra-domain=pypi.org",
        "--no-forward-credentials",
        "--api-key-env=ANTHROPIC_API_KEY",
        "--verbose",
    ]


def test_values_starting_with_a_dash_stay_values(tmp_path: Path) -> None:
    spec = LaunchSpec(branch="-x", agent="bash", mode="background", prompt_text="--help")
    paths = RunPaths(log=tmp_path / "a.log", summary=tmp_path / "a.json", stdout=tmp_path / "a.out")
    argv = build_argv(make_cfg(), tmp_path, spec, paths)
    assert "--branch=-x" in argv and "--prompt-text=--help" in argv


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
