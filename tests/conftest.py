from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from project_sandbox_tui.config import Config, Project, Sandbox

FAKE = Path(__file__).with_name("fake_project_sandbox.py")


def run(*args: str, cwd: Path, env_extra: dict[str, str] | None = None) -> str:
    env = {**os.environ, "JJ_USER": "Test", "JJ_EMAIL": "test@example.com", **(env_extra or {})}
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout


@pytest.fixture(autouse=True)
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    (tmp_path / "gitconfig").write_text(
        "[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n"
    )
    return tmp_path


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run("git", "init", "-q", cwd=repo)
    (repo / "a.txt").write_text("a\n")
    run("git", "add", "a.txt", cwd=repo)
    run(
        "git",
        "commit",
        "-q",
        "-m",
        "first commit",
        cwd=repo,
        env_extra={
            "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z",
        },
    )
    run("git", "checkout", "-q", "-b", "feature/x", cwd=repo)
    (repo / "b.txt").write_text("b\n")
    run("git", "add", "b.txt", cwd=repo)
    run(
        "git",
        "commit",
        "-q",
        "-m",
        "add b",
        cwd=repo,
        env_extra={
            "GIT_AUTHOR_DATE": "2020-01-01T00:00:01Z",
            "GIT_COMMITTER_DATE": "2020-01-01T00:00:01Z",
        },
    )
    run("git", "checkout", "-q", "main", cwd=repo)
    return repo


@pytest.fixture
def jj_repo(tmp_path: Path) -> Path:
    if shutil.which("jj") is None:
        pytest.skip("jj not installed")
    repo = tmp_path / "jjrepo"
    repo.mkdir()
    run("jj", "git", "init", "--quiet", cwd=repo)
    (repo / "a.txt").write_text("a\n")
    run("jj", "commit", "--quiet", "-m", "first commit", cwd=repo)
    run("jj", "bookmark", "create", "--quiet", "main", "-r", "@-", cwd=repo)
    return repo


@pytest.fixture
def jj_repo_no_bookmarks(tmp_path: Path) -> Path:
    if shutil.which("jj") is None:
        pytest.skip("jj not installed")
    repo = tmp_path / "jjbare"
    repo.mkdir()
    run("jj", "git", "init", "--quiet", cwd=repo)
    (repo / "a.txt").write_text("a\n")
    run("jj", "describe", "--quiet", "-m", "work in progress", cwd=repo)
    return repo


@pytest.fixture
def fake_cfg() -> Callable[..., Config]:
    def make(**project: Any) -> Config:
        cfg = Config()
        cfg.sandbox = Sandbox(command=f"{sys.executable} {FAKE}")
        cfg.project = Project(base_image="python:3.12-slim", **project)
        return cfg

    return make


def wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met in time")
