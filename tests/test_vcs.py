from __future__ import annotations

import hashlib
import subprocess
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
    from tests.conftest import run

    backend = vcs.detect(repo)
    backend.create_branch("fix", "main")
    backend.create_branch("fix-", "feature/x")
    backend.create_branch("a+b", "feature/x")
    names = {b.name for b in backend.list_branches()}
    assert {"fix", "fix-", "a+b"} <= names

    def diff_between(base: str, target: str) -> str:
        return run(*backend.diff_command(base, target), cwd=repo)

    assert diff_between("main", "fix-") == diff_between("main", "feature/x") != ""
    assert diff_between("fix", "a+b") == diff_between("main", "feature/x")
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
    from tests.conftest import add_bookmark, jj, run

    jj(repo, "config", "set", "--repo", "ui.diff-formatter", ":git")
    backend = vcs.detect(repo)

    def diff_between(base: str, target: str) -> str:
        return run(*backend.diff_command(base, target), cwd=repo)

    diff = diff_between("main", "feature/x")
    assert "diff --git" in diff
    assert "+b" in diff
    assert diff_between("main", "main") == ""
    assert diff_between("feature/x", "main") != diff
    # Diverging main contributes deletions to a tip-to-tip comparison.
    add_bookmark(repo, "next", "main-only.txt", "main only\n")
    jj(repo, "bookmark", "set", "--quiet", "main", "-r", "next")
    assert "-main only" in diff_between("main", "feature/x")
    with pytest.raises(subprocess.CalledProcessError):
        diff_between("missing", "main")


def test_diff_uses_configured_formatter_and_snapshots_working_copy(repo: Path) -> None:
    from tests.conftest import jj, run

    jj(repo, "config", "set", "--repo", "ui.diff-formatter", ":summary")
    (repo / "a.txt").write_text("changed\n")
    backend = vcs.detect(repo)
    assert run(*backend.diff_command("main", "feature/x"), cwd=repo) == "A b.txt\n"
    assert run(*backend.diff_command("main", "@"), cwd=repo) == "M a.txt\n"


@pytest.mark.parametrize("names", [("main", "feature/x"), ("main+release", "feature+parser")])
def test_fast_forward_preserves_target_and_working_copy(repo: Path, names: tuple[str, str]) -> None:
    from tests.conftest import jj

    main, target = names
    if main != "main":
        jj(repo, "bookmark", "rename", "main", main)
        jj(repo, "bookmark", "rename", "feature/x", target)
    jj(repo, "bookmark", "create", f"{main}-other", "-r", main)
    backend = vcs.detect(repo)
    old = backend._bookmark_commit(main)
    new = backend._bookmark_commit(target)
    working_copy = backend._jj("log", "--no-graph", "-r", "@", "-T", "commit_id")
    (repo / "a.txt").write_text("uncommitted work\n")
    assert backend.fast_forward(main, target)
    assert backend._bookmark_commit(main) == backend._bookmark_commit(target) == new
    assert backend._bookmark_commit(f"{main}-other") == old
    assert backend._jj("log", "--no-graph", "-r", "@", "-T", "commit_id") == working_copy
    assert (repo / "a.txt").read_text() == "uncommitted work\n"
    assert not backend.fast_forward(main, target)


@pytest.mark.parametrize("direction", ["backwards", "sideways"])
def test_fast_forward_refuses_to_discard_history(repo: Path, direction: str) -> None:
    from tests.conftest import add_bookmark, jj

    jj(repo, "bookmark", "create", "old-main", "-r", "main")
    add_bookmark(repo, "side", "side.txt", "side\n")
    jj(repo, "bookmark", "move", "main", "--to", "feature/x")
    backend = vcs.detect(repo)
    old = backend._bookmark_commit("main")
    with pytest.raises(vcs.VcsError, match="not an ancestor"):
        backend.fast_forward("main", "old-main" if direction == "backwards" else "side")
    assert backend._bookmark_commit("main") == old


@pytest.mark.parametrize("main,target", [("missing", "feature/x"), ("main", "missing")])
def test_fast_forward_requires_existing_local_bookmarks(repo: Path, main: str, target: str) -> None:
    backend = vcs.detect(repo)
    old = backend._bookmark_commit("main")
    with pytest.raises(vcs.VcsError, match="does not exist"):
        backend.fast_forward(main, target)
    assert backend._bookmark_commit("main") == old


def test_fast_forward_to_working_copy_snapshots_edits(repo: Path) -> None:
    backend = vcs.detect(repo)
    (repo / "a.txt").write_text("changed in working copy\n")
    assert backend.fast_forward("main", "@")
    assert (
        backend._bookmark_commit("main")
        == backend._jj("log", "--no-graph", "-r", "@", "-T", "commit_id").strip()
    )
    assert (repo / "a.txt").read_text() == "changed in working copy\n"
    assert not backend.fast_forward("main", "@")


def test_fast_forward_to_working_copy_refuses_divergence(repo: Path) -> None:
    backend = vcs.detect(repo)
    old = backend._bookmark_commit("main")
    with pytest.raises(vcs.VcsError, match="not an ancestor"):
        backend.fast_forward("feature/x", "@")
    assert backend._bookmark_commit("main") == old


def test_fast_forward_to_working_copy_refuses_file_conflicts(repo: Path) -> None:
    from tests.conftest import add_bookmark, jj

    add_bookmark(repo, "left", "a.txt", "left\n")
    add_bookmark(repo, "right", "a.txt", "right\n")
    jj(repo, "new", "left", "right")
    backend = vcs.detect(repo)
    old = backend._bookmark_commit("main")
    with pytest.raises(vcs.VcsError, match="unresolved file conflicts"):
        backend.fast_forward("main", "@")
    assert backend._bookmark_commit("main") == old


def test_move_working_copy_to_bookmark_keeps_previous_change(repo: Path) -> None:
    backend = vcs.detect(repo)
    old_change = backend._jj("log", "--no-graph", "-r", "@", "-T", "change_id").strip()
    (repo / "a.txt").write_text("unfinished\n")
    saved = backend.move_working_copy_to("feature/x")
    assert saved == f"saved/{old_change[:8]}"
    assert (
        backend._bookmark_commit(saved)
        == backend._jj("log", "--no-graph", "-r", old_change, "-T", "commit_id").strip()
    )
    assert saved in {b.name for b in backend.list_branches()}
    assert (repo / "a.txt").read_text() == "a\n"
    assert (repo / "b.txt").read_text() == "b\n"
    assert backend._jj("log", "--no-graph", "-r", "@-", "-T", "commit_id").strip() == (
        backend._bookmark_commit("feature/x")
    )
    assert "unfinished" in backend._jj("file", "show", "-r", old_change, "a.txt")


def test_move_working_copy_does_not_add_bookmark_when_one_exists(repo: Path) -> None:
    from tests.conftest import jj

    jj(repo, "bookmark", "create", "already-saved", "-r", "@")
    backend = vcs.detect(repo)
    (repo / "a.txt").write_text("still saved\n")
    assert backend.move_working_copy_to("feature/x") is None
    assert "still saved" in backend._jj("file", "show", "-r", "already-saved", "a.txt")
    assert not any(b.name.startswith("saved/") for b in backend.list_branches())


def test_move_working_copy_avoids_saved_name_collision(repo: Path) -> None:
    from tests.conftest import jj

    backend = vcs.detect(repo)
    change = backend._jj("log", "--no-graph", "-r", "@", "-T", "change_id").strip()
    base = f"saved/{change[:8]}"
    jj(repo, "bookmark", "create", base, "-r", "main")
    saved = backend.move_working_copy_to("feature/x")
    assert saved == f"{base}-2"
    assert backend._bookmark_commit(base) == backend._bookmark_commit("main")


def test_move_working_copy_requires_local_bookmark(repo: Path) -> None:
    backend = vcs.detect(repo)
    with pytest.raises(vcs.VcsError, match="not a local bookmark"):
        backend.move_working_copy_to("@")
    with pytest.raises(vcs.VcsError, match="does not exist"):
        backend.move_working_copy_to("missing")


def test_fast_forward_refuses_unresolved_file_conflicts(repo: Path) -> None:
    from tests.conftest import add_bookmark, jj

    add_bookmark(repo, "left", "a.txt", "left\n")
    add_bookmark(repo, "right", "a.txt", "right\n")
    jj(repo, "new", "--no-edit", "left", "right")
    jj(repo, "bookmark", "create", "conflicted", "-r", "heads(left:: & right::)")
    backend = vcs.detect(repo)
    old = backend._bookmark_commit("main")
    with pytest.raises(vcs.VcsError, match="unresolved file conflicts"):
        backend.fast_forward("main", "conflicted")
    assert backend._bookmark_commit("main") == old


@pytest.mark.parametrize("name", ["main", "feature/x"])
def test_fast_forward_refuses_conflicted_bookmarks(repo: Path, name: str) -> None:
    from tests.conftest import add_bookmark, jj

    add_bookmark(repo, "left", "left.txt", "left\n", start="feature/x")
    add_bookmark(repo, "right", "right.txt", "right\n", start="feature/x")
    operation = jj(repo, "op", "log", "--no-graph", "-n", "1", "-T", "id").strip()
    jj(repo, "bookmark", "move", f"exact:{name}", "--to", "left")
    jj(repo, "--at-op", operation, "bookmark", "move", f"exact:{name}", "--to", "right")
    backend = vcs.detect(repo)
    before = backend._jj("bookmark", "list")  # integrate the concurrent operations
    with pytest.raises(vcs.VcsError, match="unambiguous target"):
        backend.fast_forward("main", "feature/x")
    assert backend._jj("bookmark", "list") == before


@pytest.mark.parametrize("changed", ["main", "target"])
def test_fast_forward_rechecks_at_mutation(
    repo: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    from tests.conftest import add_bookmark, jj

    add_bookmark(repo, "side", "side.txt", "side\n")
    add_bookmark(repo, "next", "next.txt", "next\n", start="feature/x")
    backend = vcs.detect(repo)
    old = backend._bookmark_commit("main")
    side = backend._bookmark_commit("side")
    original = backend._jj

    def concurrent_move(*args: str) -> str:
        if args[:2] == ("bookmark", "move"):
            # Simulate a bookmark changing after the preflight queries.
            name, destination = ("main", "side") if changed == "main" else ("feature/x", "next")
            jj(repo, "bookmark", "move", f"exact:{name}", "--to", destination)
        return original(*args)

    monkeypatch.setattr(backend, "_jj", concurrent_move)
    with pytest.raises(vcs.VcsError):
        backend.fast_forward("main", "feature/x")
    assert backend._bookmark_commit("main") == (side if changed == "main" else old)
