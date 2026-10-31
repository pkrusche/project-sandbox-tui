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
    assert branches[1].subject == "first commit"
    assert len(branches[1].head) == 8
    assert backend.default_start() == "main"


def test_detect_from_subdirectory(repo: Path) -> None:
    sub = repo / "deep" / "er"
    sub.mkdir(parents=True)
    assert vcs.detect(sub).root == repo.resolve()


def test_main_comparisons_and_refresh(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conftest import add_bookmark, jj

    backend = vcs.detect(repo)
    jj(repo, "bookmark", "create", "same", "-r", "main")

    def comparisons() -> dict[str, vcs.MainComparison | None]:
        return {b.name: b.comparison for b in backend.list_branches(compare_to_main="")}

    assert comparisons() == {
        "@": vcs.MainComparison(1, 0, 0),
        "main": vcs.MainComparison(0, 0, 0),
        "same": vcs.MainComparison(0, 0, 0),
        "feature/x": vcs.MainComparison(1, 0, 1),
    }
    original = backend._jj
    calls: list[tuple[str, ...]] = []

    def record(*args: str) -> str:
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(backend, "_jj", record)
    comparisons()
    assert not any(args[0] == "diff" or "--count" in args for args in calls)

    add_bookmark(repo, "next", "main-only.txt", "main only\n")
    jj(repo, "bookmark", "move", "main", "--to", "next")
    result = comparisons()
    assert result["same"] == vcs.MainComparison(0, 1, 1)
    assert result["feature/x"] == vcs.MainComparison(1, 1, 2)
    assert result["main"] == result["next"] == vcs.MainComparison(0, 0, 0)

    # Rewriting a tip keeps its change ID but must invalidate the comparison.
    jj(repo, "new", "feature/x")
    (repo / "b.txt").unlink()
    jj(repo, "squash", "--quiet")
    assert comparisons()["feature/x"] == vcs.MainComparison(1, 1, 1)


@pytest.mark.parametrize("main", ["main", "master", "trunk", "a+b", "fix-"])
def test_main_comparison_baseline(repo: Path, main: str) -> None:
    from tests.conftest import jj

    if main != "main":
        jj(repo, "bookmark", "rename", "main", vcs.revset_symbol(main))
    backend = vcs.detect(repo)
    configured = main if main in {"a+b", "fix-"} else ""
    branches = {b.name: b for b in backend.list_branches(compare_to_main=configured)}
    assert branches["feature/x"].comparison == vcs.MainComparison(1, 0, 1)
    assert all(b.comparison is None for b in backend.list_branches(compare_to_main="missing"))


def test_main_comparison_merge_and_equal_trees(repo: Path) -> None:
    from tests.conftest import add_bookmark, jj

    add_bookmark(repo, "side", "line\nbreak.txt", "side\n")
    jj(repo, "new", "feature/x", "side")
    jj(repo, "bookmark", "create", "merged", "-r", "@")
    backend = vcs.detect(repo)
    branches = {b.name: b for b in backend.list_branches(compare_to_main="")}
    assert branches["merged"].comparison == vcs.MainComparison(3, 0, 2)
    jj(repo, "new", "main")
    jj(repo, "bookmark", "create", "empty", "-r", "@")
    branches = {b.name: b for b in backend.list_branches(compare_to_main="")}
    assert branches["empty"].comparison == vcs.MainComparison(1, 0, 0)


def test_main_comparison_error_retries(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = vcs.detect(repo)
    original = backend._jj

    def fail_diff(*args: str) -> str:
        if args[0] == "diff":
            raise vcs.VcsError("comparison unavailable")
        return original(*args)

    monkeypatch.setattr(backend, "_jj", fail_diff)
    branches = {b.name: b for b in backend.list_branches(compare_to_main="")}
    assert branches["main"].comparison == vcs.MainComparison(0, 0, 0)
    assert branches["feature/x"].comparison is None
    monkeypatch.setattr(backend, "_jj", original)
    branches = {b.name: b for b in backend.list_branches(compare_to_main="")}
    assert branches["feature/x"].comparison == vcs.MainComparison(1, 0, 1)


def test_working_copy_comparison_snapshots_edits_and_bookmarks(repo: Path) -> None:
    from tests.conftest import jj

    backend = vcs.detect(repo)

    def comparisons() -> dict[str, vcs.MainComparison | None]:
        return {b.name: b.comparison for b in backend.list_branches(compare_to_main="")}

    assert comparisons()["@"] == vcs.MainComparison(1, 0, 0)
    jj(repo, "bookmark", "create", "working", "-r", "@")
    (repo / "uncommitted.txt").write_text("new file\n")
    result = comparisons()
    assert result["@"] == result["working"] == vcs.MainComparison(1, 0, 1)
    (repo / "uncommitted.txt").unlink()
    assert comparisons()["@"] == vcs.MainComparison(1, 0, 0)
    jj(repo, "new", "feature/x")
    assert comparisons()["@"] == vcs.MainComparison(2, 0, 1)
    jj(repo, "bookmark", "move", "main", "--to", "@")
    assert comparisons()["@"] == vcs.MainComparison(0, 0, 0)
    jj(repo, "bookmark", "delete", "main")
    assert comparisons()["@"] is None


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


@pytest.mark.parametrize("name", ["main", "feature/x", "fix-", "a+b", "naïve", "@"])
def test_edit_description(repo: Path, name: str) -> None:
    from tests.conftest import jj

    backend = vcs.detect(repo)
    if name not in {"main", "feature/x", "@"}:
        backend.create_branch(name, "feature/x")
    if name == "@":
        (repo / "dirty.txt").write_text("uncommitted work\n")
    commit, original = backend.get_description(name)
    assert original == ("" if name == "@" else "first commit\n" if name == "main" else "add b\n")
    description = "--new subject [bold] café\n\nA longer body.\nSecond line.\n"
    backend.set_description(name, description, commit)
    updated, text = backend.get_description(name)
    assert text == description
    assert updated != commit
    assert jj(repo, "diff", "--from", commit, "--to", updated) == ""
    branch = next(b for b in backend.list_branches() if b.name == name)
    assert branch.subject == description.splitlines()[0]
    backend.set_description(name, "", updated)
    assert backend.get_description(name)[1] == ""
    if name == "@":
        assert (repo / "dirty.txt").read_text() == "uncommitted work\n"


@pytest.mark.parametrize("change", ["move", "delete", "describe"])
def test_edit_description_rejects_changed_target(repo: Path, change: str) -> None:
    from tests.conftest import jj

    backend = vcs.detect(repo)
    commit, _ = backend.get_description("feature/x")
    if change == "move":
        jj(repo, "bookmark", "set", "feature/x", "-r", "main", "--allow-backwards")
    elif change == "delete":
        jj(repo, "bookmark", "delete", "feature/x")
    else:
        jj(repo, "describe", "feature/x", "-m", "external edit")
    with pytest.raises(vcs.VcsError, match="changed since|does not exist"):
        backend.set_description("feature/x", "my draft", commit)
    assert jj(repo, "log", "--no-graph", "-r", commit, "-T", "description") == "add b\n"


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


@pytest.mark.parametrize("name", ["topic", "fix-", "a+b", "naïve"])
def test_delete_and_restore_bookmark(repo: Path, name: str) -> None:
    from tests.conftest import jj

    backend = vcs.detect(repo)
    backend.create_branch(name, "feature/x")
    commit = backend.delete_bookmark(name)
    assert commit == backend._bookmark_commit("feature/x")
    assert name not in {b.name for b in backend.list_branches()}
    with pytest.raises(vcs.VcsError, match="does not exist"):
        backend.delete_bookmark(name)
    # Restoring also works once the commit has been abandoned, and revives it.
    jj(repo, "bookmark", "delete", "feature/x")
    jj(repo, "abandon", commit)
    backend.restore_bookmark(name, commit)
    assert backend._bookmark_commit(name) == commit
    with pytest.raises(vcs.VcsError):
        backend.restore_bookmark(name, commit)  # exists again


def test_delete_bookmark_snapshots_edits_first(repo: Path) -> None:
    from tests.conftest import jj

    backend = vcs.detect(repo)
    jj(repo, "bookmark", "create", "wip", "-r", "@")
    (repo / "edit.txt").write_text("edit\n")
    commit = backend.delete_bookmark("wip")
    # The returned commit includes the edit: restoring must not revive the old @.
    assert commit == backend._jj("log", "--no-graph", "-r", "@", "-T", "commit_id")
    assert "edit.txt" in backend._jj("file", "list", "-r", commit)


def test_bookmarked_ancestors(repo: Path) -> None:
    from tests.conftest import add_bookmark, jj

    add_bookmark(repo, "left", "left.txt", "left\n", start="feature/x")
    jj(repo, "bookmark", "create", '"fix-"', "-r", "feature/x")
    jj(repo, "new", "--quiet", "left", "main", "-m", "merge")
    backend = vcs.detect(repo)
    merge = backend._jj("log", "--no-graph", "-r", "@", "-T", "commit_id")
    heads = backend.bookmarked_ancestors(merge)
    assert sorted(sorted(names) for names in heads) == [["left"]]  # main is behind left
    left = backend._bookmark_commit("left")
    assert sorted(backend.bookmarked_ancestors(left)[0]) == ["feature/x", "fix-"]
    assert backend.bookmarked_ancestors(backend._bookmark_commit("main")) == []


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
    old = backend._bookmark_commit("feature/x")
    with pytest.raises(vcs.VcsError, match="not an ancestor"):
        backend.fast_forward("feature/x", "@")
    assert backend._bookmark_commit("feature/x") == old


@pytest.mark.parametrize("target", ["@", "conflicted"])
def test_fast_forward_refuses_unresolved_file_conflicts(repo: Path, target: str) -> None:
    from tests.conftest import add_bookmark, jj

    add_bookmark(repo, "left", "a.txt", "left\n")
    add_bookmark(repo, "right", "a.txt", "right\n")
    jj(repo, "new", "left", "right")
    if target != "@":
        jj(repo, "bookmark", "create", target, "-r", "@")
        jj(repo, "new", "main")  # the conflicted target is not the current working copy
    backend = vcs.detect(repo)
    old = backend._bookmark_commit("main")
    with pytest.raises(vcs.VcsError, match="unresolved file conflicts"):
        backend.fast_forward("main", target)
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


@pytest.mark.parametrize("target", ["main", "feature/x"])
def test_move_working_copy_snapshots_before_resolving_target(repo: Path, target: str) -> None:
    from tests.conftest import jj

    jj(repo, "edit", "main")
    backend = vcs.detect(repo)
    (repo / "a.txt").write_text("unsnapshotted edit\n")
    assert backend.move_working_copy_to(target) is None
    assert backend._jj("log", "--no-graph", "-r", "@-", "-T", "commit_id").strip() == (
        backend._bookmark_commit(target)
    )
    assert (repo / "a.txt").read_text() == "unsnapshotted edit\n"
    assert "unsnapshotted edit" in backend._jj("file", "show", "-r", "main", "a.txt")


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
    branches = {b.name: b for b in backend.list_branches(compare_to_main="")}
    assert branches[name].comparison is None
    if name == "main":
        assert all(b.comparison is None for b in branches.values())
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
