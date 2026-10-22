from __future__ import annotations

import os
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
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))  # also isolates jj's config
    return tmp_path


def jj(repo: Path, *args: str) -> str:
    return run("jj", *args, cwd=repo)


def add_bookmark(repo: Path, name: str, path: str, text: str, start: str = "main") -> None:
    """Commit ``path`` on top of ``start`` as bookmark ``name``; @ goes back onto main."""
    jj(repo, "new", "--quiet", start)
    (repo / path).write_text(text)
    jj(repo, "commit", "--quiet", "-m", f"{name}: {path}")
    jj(repo, "bookmark", "create", "--quiet", name, "-r", "@-")
    jj(repo, "new", "--quiet", "main")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """main ("first commit") and feature/x on top of it ("add b"); @ is empty on main."""
    repo = tmp_path / "repo"
    repo.mkdir()
    jj(repo, "git", "init", "--quiet")
    (repo / "a.txt").write_text("a\n")
    jj(repo, "commit", "--quiet", "-m", "first commit")
    jj(repo, "bookmark", "create", "--quiet", "main", "-r", "@-")
    (repo / "b.txt").write_text("b\n")
    jj(repo, "commit", "--quiet", "-m", "add b")
    jj(repo, "bookmark", "create", "--quiet", "feature/x", "-r", "@-")
    jj(repo, "new", "--quiet", "main")
    return repo


@pytest.fixture
def jj_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "jjrepo"
    repo.mkdir()
    jj(repo, "git", "init", "--quiet")
    (repo / "a.txt").write_text("a\n")
    jj(repo, "commit", "--quiet", "-m", "first commit")
    jj(repo, "bookmark", "create", "--quiet", "main", "-r", "@-")
    return repo


@pytest.fixture
def jj_repo_no_bookmarks(tmp_path: Path) -> Path:
    repo = tmp_path / "jjbare"
    repo.mkdir()
    jj(repo, "git", "init", "--quiet")
    (repo / "a.txt").write_text("a\n")
    jj(repo, "describe", "--quiet", "-m", "work in progress")
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
