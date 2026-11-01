from __future__ import annotations

import contextlib
import ctypes
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Generator
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
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    runtime = tmp_path / ".runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))  # never the real ~/.codex
    # XDG alone does not override JJ_CONFIG or ~/.jjconfig.toml.
    monkeypatch.setenv("JJ_CONFIG", str(tmp_path / "jj.toml"))
    return tmp_path


@pytest.fixture(autouse=True)
def child_processes(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Own every real child, including launches hidden inside UI worker threads.

    Tests may deliberately quit with detached jobs running. Always stop those
    groups, even on assertion failure and even if the group leader has exited.
    Capture the real signal functions before tests mock them.
    """
    children: list[tuple[subprocess.Popen[Any], bool]] = []
    init = subprocess.Popen.__init__
    killpg, kill, waitpid = os.killpg, os.kill, os.waitpid
    # Container PID 1 is not always an init that reaps orphans. Adopt our
    # grandchildren on Linux, then wait only for the groups created by this test.
    # On other supported systems, init collects them normally.
    prctl = ctypes.CDLL(None, use_errno=True).prctl if sys.platform == "linux" else None
    was_subreaper = ctypes.c_int()
    if prctl is not None:
        assert prctl(37, ctypes.byref(was_subreaper), 0, 0, 0) == 0  # GET_CHILD_SUBREAPER
        assert prctl(36, 1, 0, 0, 0) == 0  # SET_CHILD_SUBREAPER

    def track(proc: subprocess.Popen[Any], *args: Any, **kwargs: Any) -> None:
        init(proc, *args, **kwargs)
        children.append((proc, bool(kwargs.get("start_new_session"))))

    monkeypatch.setattr(subprocess.Popen, "__init__", track)
    try:
        yield
    finally:
        try:
            # Signal all children before waiting; one stuck child must not prevent
            # cleanup of the others. Only detached groups belong wholly to this test.
            for proc, group in reversed(children):
                with contextlib.suppress(ProcessLookupError):
                    if group:
                        killpg(proc.pid, signal.SIGKILL)
                    elif proc.poll() is None:
                        kill(proc.pid, signal.SIGKILL)
            for proc, _group in children:
                proc.wait(timeout=5)
            if prctl is not None:
                for proc, _group in children:
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            pid, _status = waitpid(-proc.pid, os.WNOHANG)
                        except ChildProcessError:
                            break
                        if pid == 0:
                            # Also covers PTYs that call setsid after Popen.
                            with contextlib.suppress(ProcessLookupError):
                                killpg(proc.pid, signal.SIGKILL)
                            assert time.monotonic() < deadline, f"child group {proc.pid} survived"
                            time.sleep(0.01)
        finally:
            if prctl is not None:
                assert prctl(36, was_subreaper.value, 0, 0, 0) == 0


def jj(repo: Path, *args: str) -> str:
    return run("jj", *args, cwd=repo)


def add_bookmark(repo: Path, name: str, path: str, text: str, start: str = "main") -> None:
    """Commit ``path`` on top of ``start`` as bookmark ``name``; @ goes back onto main."""
    jj(repo, "new", "--quiet", start)
    (repo / path).write_text(text)
    jj(repo, "commit", "--quiet", "-m", f"{name}: {path}")
    jj(repo, "bookmark", "create", "--quiet", name, "-r", "@-")
    jj(repo, "new", "--quiet", "main")


def make_stale(repo: Path, other: Path) -> None:
    """Make ``repo``'s working copy stale: abandon its commit from a new workspace ``other``."""
    (repo / "wip.txt").write_text("wip\n")
    jj(repo, "status")  # snapshot wip.txt into @
    jj(repo, "workspace", "add", "--quiet", str(other))
    jj(other, "abandon", "--quiet", "default@")


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
