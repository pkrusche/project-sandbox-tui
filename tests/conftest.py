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


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("container smoke")
    group.addoption(
        "--container-smoke", action="store_true", help="Run real bash-agent container smoke tests"
    )
    group.addoption(
        "--container-runtime",
        choices=("auto", "docker", "podman", "apple-container"),
        default="auto",
        help="Runtime for --container-smoke (default: auto)",
    )
    group.addoption(
        "--container-command", default="project-sandbox", help="Real project-sandbox command"
    )
    group.addoption(
        "--container-base-image", default="python:3.12-slim", help="Base image for the smoke build"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--container-smoke"):
        skip = pytest.mark.skip(reason="real containers require --container-smoke")
        for item in items:
            if "container_smoke" in item.keywords:
                item.add_marker(skip)


def run(*args: str, cwd: Path, env_extra: dict[str, str] | None = None) -> str:
    env = {**os.environ, "JJ_USER": "Test", "JJ_EMAIL": "test@example.com", **(env_extra or {})}
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout


@pytest.fixture(autouse=True)
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))  # also isolates jj's config
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))  # never the real ~/.codex
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


RepoCopy = Callable[[Callable[[Path], None], Path], Path]


@pytest.fixture(scope="session")
def repo_copy(tmp_path_factory: pytest.TempPathFactory) -> RepoCopy:
    """``repo_copy(build, dest)``: a copy of the repo ``build`` makes, built once per session.

    Creating a jj repo takes several jj calls; copying one is much faster and keeps tests
    from seeing each other's changes. With xdist, each worker builds its own templates.
    """
    templates: dict[Callable[[Path], None], Path] = {}
    xdg = tmp_path_factory.mktemp("template-xdg")

    def copy(build: Callable[[Path], None], dest: Path) -> Path:
        if build not in templates:
            template = tmp_path_factory.mktemp(dest.name)
            with pytest.MonkeyPatch.context() as mp:  # not the user's jj config
                mp.setenv("XDG_STATE_HOME", str(xdg / "state"))
                mp.setenv("XDG_CONFIG_HOME", str(xdg / "config"))
                build(template)
            templates[build] = template
        shutil.copytree(templates[build], dest, symlinks=True)
        return dest

    return copy


def _build_repo(repo: Path) -> None:
    jj(repo, "git", "init", "--quiet")
    (repo / "a.txt").write_text("a\n")
    jj(repo, "commit", "--quiet", "-m", "first commit")
    jj(repo, "bookmark", "create", "--quiet", "main", "-r", "@-")
    (repo / "b.txt").write_text("b\n")
    jj(repo, "commit", "--quiet", "-m", "add b")
    jj(repo, "bookmark", "create", "--quiet", "feature/x", "-r", "@-")
    jj(repo, "new", "--quiet", "main")


def _build_jj_repo(repo: Path) -> None:
    jj(repo, "git", "init", "--quiet")
    (repo / "a.txt").write_text("a\n")
    jj(repo, "commit", "--quiet", "-m", "first commit")
    jj(repo, "bookmark", "create", "--quiet", "main", "-r", "@-")


def _build_jj_repo_no_bookmarks(repo: Path) -> None:
    jj(repo, "git", "init", "--quiet")
    (repo / "a.txt").write_text("a\n")
    jj(repo, "describe", "--quiet", "-m", "work in progress")


@pytest.fixture
def repo(tmp_path: Path, repo_copy: RepoCopy) -> Path:
    """main ("first commit") and feature/x on top of it ("add b"); @ is empty on main."""
    return repo_copy(_build_repo, tmp_path / "repo")


@pytest.fixture
def jj_repo(tmp_path: Path, repo_copy: RepoCopy) -> Path:
    return repo_copy(_build_jj_repo, tmp_path / "jjrepo")


@pytest.fixture
def jj_repo_no_bookmarks(tmp_path: Path, repo_copy: RepoCopy) -> Path:
    return repo_copy(_build_jj_repo_no_bookmarks, tmp_path / "jjbare")


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
