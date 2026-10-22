from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from project_sandbox_tui import vcs


def test_detect_git_and_list(git_repo: Path) -> None:
    backend = vcs.detect(git_repo / ".")
    assert backend.kind == "git"
    assert backend.root == git_repo.resolve()
    branches = vcs.sort_branches(backend.list_branches())
    assert [b.name for b in branches] == ["main", "feature/x"]  # current first
    assert branches[0].is_current
    assert branches[1].subject == "add b"
    assert backend.default_start() == "main"


def test_detect_from_subdirectory(git_repo: Path) -> None:
    sub = git_repo / "deep" / "er"
    sub.mkdir(parents=True)
    assert vcs.detect(sub).root == git_repo.resolve()


def test_not_a_repo(tmp_path: Path) -> None:
    with pytest.raises(vcs.VcsError):
        vcs.detect(tmp_path / "nowhere")


def test_git_create_branch(git_repo: Path) -> None:
    backend = vcs.detect(git_repo)
    backend.create_branch("topic/new", "feature/x")
    by_name = {b.name: b for b in backend.list_branches()}
    assert by_name["topic/new"].head == by_name["feature/x"].head
    with pytest.raises(vcs.VcsError):
        backend.create_branch("topic/new", "main")  # already exists


def test_jj_list_and_create(jj_repo: Path) -> None:
    backend = vcs.detect(jj_repo)
    assert backend.kind == "jj"
    assert [b.name for b in backend.list_branches()] == ["main"]
    assert backend.default_start() == "main"
    backend.create_branch("feat/y", "main")
    names = sorted(b.name for b in backend.list_branches())
    assert names == ["feat/y", "main"]
    main = next(b for b in backend.list_branches() if b.name == "main")
    assert main.subject == "first commit"
    assert len(main.head) == 8


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("feature/x", True),
        ("fix-123", True),
        ("", False),
        ("has space", False),
        ("a..b", False),
        ("-leading", False),
        ("trailing/", False),
        ("x.lock", False),
        ("a:b", False),
    ],
)
def test_validate_branch_name(name: str, ok: bool) -> None:
    assert (vcs.validate_branch_name(name) is None) == ok


def test_workspace_path_matches_project_sandbox(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    suffix = hashlib.sha256(b"feat/x").hexdigest()[:6]
    assert vcs.workspace_path(repo, "git", "feat/x", None) == (
        tmp_path / "proj-worktrees" / f"feat-x-{suffix}"
    )
    assert vcs.workspace_path(repo, "jj", "plain", None) == tmp_path / "proj-workspaces" / "plain"
    assert vcs.workspace_path(repo, "git", "plain", tmp_path / "wt") == tmp_path / "wt" / "plain"
