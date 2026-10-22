from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from project_sandbox_tui.config import Config, ConfigError, Network, Project, Sandbox
from project_sandbox_tui.notify import notification_argv
from project_sandbox_tui.sandbox import (
    LaunchSpec,
    RunPaths,
    build_argv,
    read_sessions,
    running_by_branch,
)
from project_sandbox_tui.vcs import workspace_path


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
        "--runtime",
        "docker",
        "--memory",
        "16g",
        "--branch",
        "feat/x",
        "--worktree-dir",
        str(repo / "../wt"),
        "--keep-workspace",
        "--agent",
        "claude",
        "--model",
        "opus",
        "--effort",
        "high",
        "--internet-proxy",
        "http://proxy:3128",
        "--ca-cert",
        str(repo / "certs/root.pem"),
        "--extra-domain",
        "pypi.org",
        "--no-forward-credentials",
        "--api-key-env",
        "ANTHROPIC_API_KEY",
        "--verbose",
    ]


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
    assert argv[:5] == ["project-sandbox", str(repo.resolve()), "--python-uv", "--python", "3.12"]
    assert "--model" not in argv and "--effort" not in argv  # bash takes neither
    assert "--keep-workspace" not in argv
    assert argv[-8:] == [
        "--prompt-text",
        "do it",
        "--log",
        str(paths.log),
        "--json-summary",
        str(paths.summary),
        "--timeout",
        "60",
    ]


def test_dockerfile_source(tmp_path: Path) -> None:
    cfg = Config()
    cfg.project = Project(dockerfile="docker/Dockerfile", docker_context=".")
    argv = build_argv(cfg, tmp_path, LaunchSpec(branch="b", agent="codex"))
    i = argv.index("--dockerfile")
    assert argv[i + 1] == str(tmp_path / "docker/Dockerfile")
    assert argv[i + 2 : i + 4] == ["--docker-context", str(tmp_path / ".")]


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
    ws = workspace_path(repo, "git", "feat/x", None)

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
    rec("dead", "running", 999_999_999, workspace_path(repo, "git", "other", None))
    rec("done", "completed", os.getpid(), workspace_path(repo, "git", "main", None))
    (sessions / "garbage.json").write_text("{not json")

    records = read_sessions(sessions)
    statuses = {r.session_id: r.status for r in records}
    assert statuses == {"live": "running", "dead": "orphaned", "done": "completed"}
    matched = running_by_branch(records, repo, "git", ["main", "feat/x", "other"], None)
    assert list(matched) == ["feat/x"]
    assert matched["feat/x"].container_name == "c-live"


def test_notification_template_is_injection_safe() -> None:
    argv = notification_argv("t; rm -rf /", "msg $(x)", "notifier --title {title} -m {message}")
    assert argv == ["notifier", "--title", "t; rm -rf /", "-m", "msg $(x)"]


def test_main_checkout_decision(git_repo: Path, tmp_path: Path) -> None:
    from project_sandbox_tui.sandbox import CheckoutConflict, uses_main_checkout
    from project_sandbox_tui.vcs import detect
    from tests.conftest import run

    def branch(name: str):  # type: ignore[no-untyped-def]
        return next(b for b in detect(git_repo).list_branches() if b.name == name)

    assert uses_main_checkout(git_repo, branch("main"), None)  # checked out in the repo
    assert not uses_main_checkout(git_repo, branch("feature/x"), None)  # not checked out

    # checked out in project-sandbox's own worktree → reuse it via --branch
    ps_wt = workspace_path(git_repo, "git", "feature/x", None)
    run("git", "worktree", "add", "-q", str(ps_wt), "feature/x", cwd=git_repo)
    assert not uses_main_checkout(git_repo, branch("feature/x"), None)

    # checked out in some other worktree → conflict
    run("git", "branch", "other", cwd=git_repo)
    run("git", "worktree", "add", "-q", str(tmp_path / "mine"), "other", cwd=git_repo)
    with pytest.raises(CheckoutConflict, match="another worktree"):
        uses_main_checkout(git_repo, branch("other"), None)


def test_main_checkout_argv_omits_worktree_flags(tmp_path: Path) -> None:
    spec = LaunchSpec(branch="main", agent="claude", main_checkout=True)
    argv = build_argv(make_cfg(), tmp_path, spec)
    for flag in ("--branch", "--worktree-dir", "--keep-workspace"):
        assert flag not in argv
    assert LaunchSpec.from_dict(spec.to_dict()).main_checkout


def test_main_checkout_sessions_map_to_current_branch(tmp_path: Path) -> None:
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
    assert running_by_branch(records, repo, "git", ["main"], None) == {}
    assert list(running_by_branch(records, repo, "git", ["main"], None, main_branch="main")) == [
        "main"
    ]
