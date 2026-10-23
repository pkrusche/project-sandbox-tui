from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from project_sandbox_tui import vcs


def test_detect_and_list(repo: Path) -> None:
    backend = vcs.detect(repo / ".")
    assert backend.root == repo.resolve()
    branches = vcs.sort_branches(backend.list_branches())
    assert [b.name for b in branches] == ["@", "main", "feature/x"]  # working copy, current
    assert branches[1].is_current and not branches[2].is_current
    assert branches[2].subject == "add b"
    assert backend.default_start() == "main"


def test_detect_from_subdirectory(repo: Path) -> None:
    sub = repo / "deep" / "er"
    sub.mkdir(parents=True)
    assert vcs.detect(sub).root == repo.resolve()


def test_not_a_repo(tmp_path: Path) -> None:
    with pytest.raises(vcs.VcsError, match="not inside a jj repository"):
        vcs.detect(tmp_path / "nowhere")


def test_plain_git_repo_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "plain" / ".git").mkdir(parents=True)
    with pytest.raises(vcs.VcsError, match="jj git init --colocate"):
        vcs.detect(tmp_path / "plain")


def test_create_branch(repo: Path) -> None:
    backend = vcs.detect(repo)
    backend.create_branch("topic/new", "feature/x")
    by_name = {b.name: b for b in backend.list_branches()}
    assert by_name["topic/new"].head == by_name["feature/x"].head
    with pytest.raises(vcs.VcsError):
        backend.create_branch("topic/new", "main")  # already exists


def test_names_with_revset_operators(repo: Path) -> None:
    """``fix-`` unquoted means "parents of fix": it must name the bookmark."""
    backend = vcs.detect(repo)
    backend.create_branch("fix", "main")
    backend.create_branch("fix-", "feature/x")
    backend.create_branch("a+b", "feature/x")
    names = {b.name for b in backend.list_branches()}
    assert {"fix", "fix-", "a+b"} <= names
    assert backend.diff("main", "fix-") == backend.diff("main", "feature/x") != ""
    assert backend.diff("fix", "a+b") == backend.diff("main", "feature/x")
    assert vcs.revset_symbol("@") == "@"
    assert vcs.revset_symbol('a"b\\c') == '"a\\"b\\\\c"'


def test_jj_list_and_create(jj_repo: Path) -> None:
    backend = vcs.detect(jj_repo)
    assert [b.name for b in backend.list_branches()] == ["@", "main"]  # working copy first
    assert backend.default_start() == "main"
    backend.create_branch("feat/y", "main")
    names = sorted(b.name for b in backend.list_branches() if not b.working_copy)
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
        ("topic/.hidden", False),
        ("topic.lock/child", False),
        ("a\x00b", False),
        ("a\x7fb", False),
    ],
)
def test_validate_branch_name(name: str, ok: bool) -> None:
    assert (vcs.validate_branch_name(name) is None) == ok


def test_workspace_path_matches_project_sandbox(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    suffix = hashlib.sha256(b"feat/x").hexdigest()[:6]
    assert vcs.workspace_path(repo, "feat/x", None) == (
        tmp_path / "proj-workspaces" / f"feat-x-{suffix}"
    )
    assert vcs.workspace_path(repo, "plain", None) == tmp_path / "proj-workspaces" / "plain"
    assert vcs.workspace_path(repo, "plain", tmp_path / "wt") == tmp_path / "wt" / "plain"


def test_jj_marks_bookmark_under_working_copy_as_current(jj_repo: Path) -> None:
    from tests.conftest import run

    backend = vcs.detect(jj_repo)
    current = {b.name: b.is_current for b in backend.list_branches()}
    assert current == {"@": True, "main": True}  # main is @-
    (jj_repo / "b.txt").write_text("b\n")
    run("jj", "commit", "--quiet", "-m", "second", cwd=jj_repo)  # main is now @--
    backend.create_branch("tip", "@-")
    by_name = {b.name: b for b in backend.list_branches()}
    assert not by_name["main"].is_current
    assert by_name["tip"].is_current


def test_jj_always_offers_the_working_copy(jj_repo_no_bookmarks: Path) -> None:
    backend = vcs.detect(jj_repo_no_bookmarks)
    [wc] = backend.list_branches()
    assert wc.working_copy and wc.is_current and wc.name == vcs.WORKING_COPY
    assert wc.subject == "work in progress" and wc.label == "@ working copy"
    assert backend.default_start() == "@"  # no bookmarks: bookmark the work done in @
    backend.create_branch("feature", backend.default_start())
    branches = vcs.sort_branches(backend.list_branches())
    assert [b.name for b in branches] == ["@", "feature"]  # still listed, and first
    assert backend.default_start() == "@-"  # bookmarks exist, none named main/master/trunk


def test_sort_branches_priorities_and_ties() -> None:
    def branch(
        name: str,
        timestamp: int = 0,
        *,
        is_current: bool = False,
        working_copy: bool = False,
    ) -> vcs.Branch:
        return vcs.Branch(
            name,
            "abc",
            "subject",
            timestamp,
            is_current=is_current,
            working_copy=working_copy,
        )

    branches = [
        branch("z", 10),
        branch("a", 10),
        branch("newest", 20),
        branch("current", 1, is_current=True),
        branch("old", 1),
        branch("@", working_copy=True),
    ]
    original = branches.copy()
    assert [b.name for b in vcs.sort_branches(branches)] == [
        "@",
        "current",
        "newest",
        "a",
        "z",
        "old",
    ]
    assert branches == original
    assert vcs.sort_branches([]) == []


def test_diff_tips(repo: Path) -> None:
    from tests.conftest import add_bookmark, jj

    backend = vcs.detect(repo)
    diff = backend.diff("main", "feature/x")
    assert "diff --git" in diff
    assert "+b" in diff
    assert backend.diff("main", "main") == ""
    assert backend.diff("feature/x", "main") != diff
    # Diverging main contributes deletions to a tip-to-tip comparison.
    add_bookmark(repo, "next", "main-only.txt", "main only\n")
    jj(repo, "bookmark", "set", "--quiet", "main", "-r", "next")
    assert "-main only" in backend.diff("main", "feature/x")
    with pytest.raises(vcs.VcsError):
        backend.diff("missing", "main")
