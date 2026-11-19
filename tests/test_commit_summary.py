from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from project_sandbox_tui import commit_summary as summary
from project_sandbox_tui import feedback, feedback_transport
from project_sandbox_tui.config import AgentProfile, Config, ConfigError
from project_sandbox_tui.vcs import ReviewCommit, VcsError, detect


@pytest.mark.parametrize("scope", ["shared", "profile"])
@pytest.mark.parametrize("argument", ["--mount=/live", "--volume", "--runtime=chroot", "--prompt"])
def test_isolated_launch_rejects_extra_options(tmp_path: Path, scope: str, argument: str) -> None:
    cfg = Config()
    cfg.commit_review.profile = "review"
    cfg.profiles["review"] = AgentProfile(agent="codex")
    args = cfg.sandbox.extra_args if scope == "shared" else cfg.profiles["review"].extra_args
    args.append(argument)
    with pytest.raises(ConfigError, match="incompatible with isolated commit review"):
        summary.resolve_launch(cfg, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "project-sandbox --mount=source=/checkout,target=/checkout",
        "uvx project-sandbox@0.4.1 --mount=source=/checkout,target=/checkout",
    ],
)
def test_isolated_launch_rejects_options_in_command(tmp_path: Path, command: str) -> None:
    cfg = Config()
    cfg.sandbox.command = command
    with pytest.raises(ConfigError, match="sandbox.command includes project-sandbox arguments"):
        summary.resolve_launch(cfg, tmp_path)


def test_isolated_launch_accepts_uv_project_command(tmp_path: Path) -> None:
    cfg = Config()
    cfg.sandbox.command = "uv run --project /path/to/project-sandbox project-sandbox"
    isolated, _ = summary.resolve_launch(cfg, tmp_path)
    assert isolated.sandbox_command() == cfg.sandbox_command()


@pytest.mark.parametrize("cancel", [False, True])
def test_isolated_launch_and_cleanup(
    repo: Path,
    tmp_path: Path,
    fake_cfg: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("SBX_FAKE_REVIEW_CONTROL", str(tmp_path))
    cfg = fake_cfg(worktree_dir="bookmarks")
    cfg.project.base_image = ""
    cfg.project.dockerfile = "Dockerfile"
    cfg.sandbox.memory = "4g"
    cfg.sandbox.runtime = "podman"
    cfg.sandbox.cpus = 2
    cfg.sandbox.no_forward_credentials = True
    cfg.sandbox.extra_args = ["--shm-size", "512m"]
    cfg.commit_review.profile = "review"
    cfg.profiles["review"] = AgentProfile(
        agent="codex", model="chosen-model", effort="high", fast=True, extra_args=["--memory=6g"]
    )
    cfg, spec = summary.resolve_launch(cfg, repo)
    backend = detect(repo)
    commits = backend.review_commits("main", "feature/x")
    (tmp_path / "request.json").write_text('{"state": {}}')

    async def exercise() -> None:
        run = asyncio.create_task(summary.run(cfg, spec, backend, commits))
        ready = tmp_path / "ready.json"
        async with asyncio.timeout(10):
            while not ready.exists():
                assert not run.done(), run.exception() if run.done() else None
                await asyncio.sleep(0.02)
        data = json.loads(ready.read_text())
        argv = data["argv"]
        workspace = Path(argv[0])
        root = workspace.parent
        assert root.parent == tmp_path and workspace.name == "workspace"
        assert str(workspace) == data["cwd"]
        assert all(str(repo) not in arg for arg in argv)
        assert set(argv[2:]) >= {
            "--agent=codex",
            "--model=chosen-model",
            "--effort=high",
            "--fast",
            "--runtime=podman",
            "--memory=4g",
            "--memory=6g",
            "--cpus=2",
            "--shm-size=512m",
            "--no-forward-credentials",
            *feedback_transport.mounts(root / "feedback"),
            f"--mount=type=bind,source={root}/input,target={summary.INPUT},readonly",
        }
        assert not any(arg.startswith(("--branch", "--docker", "--worktree")) for arg in argv)
        assert data["patches"] == [backend.review_commit_patch(commits[0]).decode()]
        if cancel:
            run.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run
        else:
            (tmp_path / "release").touch()
            assert await run == {}
        assert not root.exists()

    asyncio.run(exercise())


def test_stage_failure_cleans_input(
    repo: Path,
    tmp_path: Path,
    fake_cfg: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    backend = detect(repo)
    commits = backend.review_commits("main", "feature/x")
    cfg, spec = summary.resolve_launch(fake_cfg(), repo)

    def fail(_commit: ReviewCommit) -> bytes:
        raise VcsError("cannot prepare this patch")

    monkeypatch.setattr(backend, "review_commit_patch", fail)
    with pytest.raises(VcsError, match="cannot prepare this patch"):
        asyncio.run(summary.run(cfg, spec, backend, commits))
    assert list(tmp_path.glob("sbx-review-*")) == []


@pytest.mark.parametrize("state", [{}, {"commits": {}}, {"commits": [None, {}, {"commit_id": []}]}])
def test_collect_missing_and_malformed_entries(tmp_path: Path, state: dict[str, object]) -> None:
    root = tmp_path / "feedback"
    feedback_transport.prepare(root, "review")
    feedback.report(root, "done", "success", state)
    assert (
        summary._collect(root / "output/feedback.json", "review", [ReviewCommit("a" * 40, "")], 0)
        == {}
    )


def test_collect_only_unique_valid_requested_entries(tmp_path: Path) -> None:
    root = tmp_path / "feedback"
    feedback_transport.prepare(root, "review")
    valid = {"summary": "[bold]Plain[/bold]", "complexity": "m", "harmful": False}
    reason = {"harmful": True, "harm_reason": "Could delete data"}
    entries = [
        {"commit_id": "a" * 40, **valid},
        {"commit_id": "b" * 40, **valid, **reason},
        {"commit_id": "c" * 40, **valid},
        {"commit_id": "c" * 40},
        {"commit_id": "d" * 40, **valid, "complexity": []},
        {"commit_id": "e" * 40, **valid, "harmful": 1},
        {"commit_id": "f" * 40, **valid, "harmful": True},
        {"commit_id": "0" * 40, **valid, "summary": "   "},
        {"commit_id": "1" * 40, **valid},  # unrequested
        {"commit_id": "f", **valid},  # abbreviated ID
    ]
    feedback.report(root, "done", "success", {"commits": entries})
    commits = [ReviewCommit(char * 40, "") for char in "abcdef02"]
    path = root / "output/feedback.json"
    assert summary._collect(path, "review", commits, 0) == {
        "a" * 40: summary.Assessment("[bold]Plain[/bold]", "M", False, ""),
        "b" * 40: summary.Assessment("[bold]Plain[/bold]", "M", True, "Could delete data"),
    }
    with pytest.raises(summary.SummaryError, match="status 7"):
        summary._collect(path, "review", commits, 7)
    with pytest.raises(summary.SummaryError, match="invalid commit review feedback"):
        summary._collect(path, "another-run", commits, 0)


@pytest.mark.parametrize("outcome", [None, "failure", "blocked"])
def test_collect_requires_final_success(tmp_path: Path, outcome: str | None) -> None:
    root = tmp_path / "feedback"
    feedback_transport.prepare(root, "review")
    path = root / "output/feedback.json"
    with pytest.raises(summary.SummaryError, match="no final feedback"):
        summary._collect(path, "review", [], 0)
    feedback.report(root, "reported", outcome)
    with pytest.raises(summary.SummaryError, match="did not report final success"):
        summary._collect(path, "review", [], 0)
