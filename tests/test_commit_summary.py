from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from project_sandbox_tui import capabilities, feedback, feedback_transport
from project_sandbox_tui import commit_summary as summary
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
    (tmp_path / "request.json").write_text('{"state": {"commits": []}}')

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
        prompt = next(arg for arg in argv if arg.startswith("--prompt-text="))
        assert '"unavailable_reason"' in prompt
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
            assert await run == {
                commits[0].commit: summary.Unavailable(
                    "The reviewer returned no entry for this commit."
                )
            }
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


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ({}, "The review report is missing its commits list."),
        ({"commits": {}}, "The review report is missing its commits list."),
        (
            {"commits": [None, {}, {"commit_id": []}]},
            "The reviewer returned no entry for this commit.",
        ),
    ],
)
def test_collect_missing_and_malformed_entries(
    tmp_path: Path, state: dict[str, object], reason: str
) -> None:
    root = tmp_path / "feedback"
    feedback_transport.prepare(root, "review")
    feedback.report(root, "done", "success", state)
    assert summary._collect(
        root / "output/feedback.json", "review", [ReviewCommit("a" * 40, "")], 0
    ) == {"a" * 40: summary.Unavailable(reason)}


def test_collect_only_unique_valid_requested_entries(tmp_path: Path) -> None:
    root = tmp_path / "feedback"
    feedback_transport.prepare(root, "review")
    valid = {"summary": "[bold]Plain[/bold]", "complexity": "m", "harmful": False}
    reason = {"harmful": True, "harm_reason": "Could delete data"}
    entries = [
        {"commit_id": "a" * 40, **valid, "priority_files": "not-an-array"},
        {"commit_id": "b" * 40, **valid, **reason, "priority_files": ["a b", "a b", None, []]},
        {"commit_id": "c" * 40, **valid},
        {"commit_id": "c" * 40},
        {"commit_id": "d" * 40, **valid, "complexity": []},
        {"commit_id": "e" * 40, **valid, "harmful": 1},
        {"commit_id": "f" * 40, **valid, "harmful": True},
        {"commit_id": "0" * 40, **valid, "summary": "   "},
        {"commit_id": "3" * 40, "unavailable_reason": "  Binary patch\n lacks context.  "},
        {"commit_id": "4" * 40, "unavailable_reason": []},
        {"commit_id": "1" * 40, **valid},  # unrequested
        {"commit_id": "f", **valid},  # abbreviated ID
    ]
    feedback.report(root, "done", "success", {"commits": entries})
    commits = [ReviewCommit(char * 40, "") for char in "abcdef0234"]
    path = root / "output/feedback.json"
    assert summary._collect(path, "review", commits, 0) == {
        "a" * 40: summary.Assessment("[bold]Plain[/bold]", "M", False, ""),
        "b" * 40: summary.Assessment(
            "[bold]Plain[/bold]", "M", True, "Could delete data", frozenset({"a b"})
        ),
        "c" * 40: summary.Unavailable("The reviewer returned duplicate entries for this commit."),
        **{
            char * 40: summary.Unavailable("The assessment has missing or invalid fields.")
            for char in "def0"
        },
        "2" * 40: summary.Unavailable("The reviewer returned no entry for this commit."),
        "3" * 40: summary.Unavailable("Binary patch lacks context."),
        "4" * 40: summary.Unavailable("The reviewer gave an empty or invalid reason for skipping."),
    }
    with pytest.raises(summary.SummaryError, match="status 7"):
        summary._collect(path, "review", commits, 7)
    with pytest.raises(summary.SummaryError, match="timed out"):
        summary._collect(path, "review", commits, 124)
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
    expected = (
        f"sandbox reported {outcome}: reported" if outcome else "before publishing final feedback"
    )
    with pytest.raises(summary.SummaryError, match=expected):
        summary._collect(path, "review", [], 0)


# ---------------------------------------------------------------- pinned isolation


def summary_argv(cfg: Config, tmp_path: Path) -> list[str]:
    from project_sandbox_tui.sandbox import RunPaths, build_argv

    isolated, spec = summary.resolve_launch(cfg, tmp_path)
    paths = RunPaths(tmp_path / "a.log", tmp_path / "s.json", tmp_path / "o.log")
    return build_argv(isolated, tmp_path / "workspace", spec, paths)


def test_default_commit_reviewer_runs_on_codex(tmp_path: Path) -> None:
    cfg = Config()
    _isolated, spec = summary.resolve_launch(cfg, tmp_path)
    assert (spec.agent, spec.model, spec.effort, spec.fast) == ("codex", "gpt-6-luna", "low", False)
    assert (spec.persona, spec.provider, spec.level, spec.profile) == (
        "commit-reviewer",
        "codex",
        3,
        "",
    )


def test_commit_reviewer_on_a_selected_provider(tmp_path: Path) -> None:
    cfg = Config()
    cfg.commit_review.provider = "claude-code"
    _isolated, spec = summary.resolve_launch(cfg, tmp_path)
    assert (spec.agent, spec.model, spec.effort) == ("claude", "claude-haiku-5-5", "low")
    cfg.commit_review.provider = "gateway-anthropic"
    with pytest.raises(ConfigError, match="network.agent_proxy is empty"):
        summary.resolve_launch(cfg, tmp_path)
    cfg.network.agent_proxy = "http://127.0.0.1:4000/v1"
    _isolated, spec = summary.resolve_launch(cfg, tmp_path)
    assert (spec.agent, spec.model, spec.gateway) == ("pi", "claude-haiku-5-5", True)


@pytest.mark.parametrize("level", [0, 1, 5])
def test_shared_widening_is_never_passed(tmp_path: Path, level: int) -> None:
    cfg = Config()
    cfg.security.level = level  # 0: a project without security levels
    cfg.network.extra_domains = ["example.org"]
    cfg.network.no_firewall = True
    cfg.network.allow_github = True
    cfg.network.internet_proxy = "http://proxy:3128"
    cfg.network.ca_certs = ["/certs/corp.pem"]
    argv = summary_argv(cfg, tmp_path)
    assert "--egress=sidecar" in argv
    assert "--internet-proxy=http://proxy:3128" in argv and "--ca-cert=/certs/corp.pem" in argv
    assert not any(
        arg.startswith(("--extra-domain", "--no-firewall", "--allow-github", "--vcs-access"))
        for arg in argv
    )


def test_firewall_backend_for_commit_review(tmp_path: Path) -> None:
    cfg = Config()
    cfg.security.egress = "firewall"
    cfg.network.extra_domains = ["example.org"]
    argv = summary_argv(cfg, tmp_path)
    assert "--egress=firewall" in argv and not any("extra-domain" in arg for arg in argv)


@pytest.mark.parametrize("argument", ["--allow-github", "--extra-domain=x.org", "--pi-ollama"])
def test_profile_with_a_widening_argument(tmp_path: Path, argument: str) -> None:
    cfg = Config()
    cfg.commit_review.profile = "review"
    cfg.profiles["review"] = AgentProfile(agent="codex", extra_args=[argument])
    with pytest.raises(ConfigError, match="widens commit review access"):
        summary.resolve_launch(cfg, tmp_path)


def test_explicit_profile_keeps_its_settings_at_the_pinned_level(tmp_path: Path) -> None:
    cfg = Config()
    cfg.commit_review.profile = "review"
    cfg.profiles["review"] = AgentProfile(agent="claude", model="opus", effort="high", fast=True)
    _isolated, spec = summary.resolve_launch(cfg, tmp_path)
    assert (spec.agent, spec.model, spec.effort, spec.fast, spec.profile, spec.level) == (
        "claude",
        "opus",
        "high",
        True,
        "review",
        3,
    )


def test_missing_sidecar_support_is_reported(repo: Path) -> None:
    import dataclasses

    from tests.test_capabilities import HELP, REAL, fake_runner

    cfg = Config()
    help_text = HELP.replace("{firewall,sidecar}", "{firewall}")
    caps = capabilities.discover(["ps"], fake_runner({**REAL, "--help": (0, help_text)}))
    snapshot = dataclasses.replace(caps, command=tuple(cfg.sandbox_command()))
    capabilities.save(snapshot, capabilities.cache_path(repo))
    with pytest.raises(ConfigError, match='project-sandbox-egress.*security.egress = "firewall"'):
        summary.resolve_launch(cfg, repo)
    cfg.security.egress = "firewall"
    assert summary.resolve_launch(cfg, repo)[1].level == 3
