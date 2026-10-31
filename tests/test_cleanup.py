import os
import shutil
from pathlib import Path

import pytest

from project_sandbox_tui.cleanup import (
    Workspace,
    find_workspaces,
    force_remove_workspace,
    remove_workspaces,
)
from project_sandbox_tui.cli import main
from project_sandbox_tui.config import Config
from project_sandbox_tui.vcs import detect, workspace_path
from tests.conftest import run


def test_cleanup_jj(jj_repo: Path, capsys) -> None:
    path = workspace_path(jj_repo, "main", None)
    path.parent.mkdir()
    run("jj", "workspace", "add", str(path), "--name", "sandbox-main", "-r", "main", cwd=jj_repo)
    assert main([str(jj_repo), "--cleanup-workspaces"]) == 0
    assert f"removed sandbox-main: {path}" in capsys.readouterr().out
    assert not path.exists()
    assert "sandbox-main" not in run("jj", "workspace", "list", cwd=jj_repo)


def _add_workspace(repo: Path, name: str) -> Path:
    """A project-sandbox style workspace for bookmark ``name``."""
    path = workspace_path(repo, name, None)
    path.parent.mkdir(exist_ok=True)
    run("jj", "workspace", "add", "--quiet", str(path), "--name", name, "-r", name, cwd=repo)
    return path


def test_cleanup_keeps_untracked_files(repo: Path, capsys) -> None:
    path = _add_workspace(repo, "feature/x")
    assert main([str(repo), "--cleanup-workspaces", "--dry-run"]) == 0
    assert "would remove feature/x" in capsys.readouterr().out
    assert path.exists()
    (path / "untracked").write_text("keep")
    assert main([str(repo), "--cleanup-workspaces"]) == 0
    assert "kept feature/x" in capsys.readouterr().out
    assert (path / "untracked").read_text() == "keep"


def test_cleanup_keeps_a_workspace_that_cannot_be_fully_inspected(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _add_workspace(repo, "feature/x")
    (path / ".gitignore").write_text("private/\n")
    run("jj", "commit", "-m", "ignore private data", cwd=path)
    private = path / "private"
    private.mkdir()
    (private / "precious").write_text("keep")
    scandir = os.scandir

    def unreadable(directory):
        if directory == str(private):
            raise PermissionError("cannot inspect private directory")
        return scandir(directory)

    monkeypatch.setattr(os, "scandir", unreadable)
    [ws] = find_workspaces(detect(repo), Config())
    assert not ws.removable and "cannot inspect" in ws.status
    assert remove_workspaces(detect(repo), Config(), [ws])[0].startswith("skipped")
    assert (private / "precious").read_text() == "keep"


def test_cleanup_ignores_git_worktree_metadata(repo: Path) -> None:
    path = _add_workspace(repo, "feature/x")
    # jj 0.46+ can create colocated workspaces with a .git pointer file.
    # Supply the metadata on older jj versions too so they exercise the regression.
    git_file = path / ".git"
    if not git_file.exists():
        git_file.write_text(f"gitdir: {repo / '.jj' / 'repo' / 'store' / 'git'}\n")
    [ws] = find_workspaces(detect(repo), Config())
    assert ws.removable

    # Only the workspace's own metadata is excluded; nested .git files are user data.
    (path / "nested").mkdir()
    (path / "nested" / ".git").write_text("keep\n")
    [ws] = find_workspaces(detect(repo), Config())
    assert not ws.removable
    assert "ignored files" in ws.status


def test_cleanup_preserves_running_session(repo: Path, capsys, monkeypatch) -> None:
    import os

    from project_sandbox_tui.sandbox import SessionRecord

    path = _add_workspace(repo, "feature/x")
    record = SessionRecord("test", "test", str(repo), str(path), "bash", "running", os.getpid(), "")
    monkeypatch.setattr("project_sandbox_tui.cleanup.read_sessions", lambda *_: [record])
    assert main([str(repo), "--cleanup-workspaces"]) == 0
    assert "running session" in capsys.readouterr().out
    assert path.exists()


def test_cleanup_preserves_running_job(repo: Path, capsys) -> None:
    import os

    from project_sandbox_tui.config import Config
    from project_sandbox_tui.jobs import JobManager, JobRecord
    from project_sandbox_tui.sandbox import LaunchSpec

    path = _add_workspace(repo, "feature/x")
    JobManager(repo, Config()).save(
        JobRecord("feature/x", LaunchSpec("feature/x", "bash"), os.getpid(), "now")
    )
    assert main([str(repo), "--cleanup-workspaces"]) == 0
    assert "running session" in capsys.readouterr().out
    assert path.exists()


def test_cleanup_jj_preserves_files(jj_repo: Path, capsys) -> None:
    path = workspace_path(jj_repo, "main", None)
    path.parent.mkdir()
    run("jj", "workspace", "add", str(path), "--name", "sandbox-main", "-r", "main", cwd=jj_repo)
    (path / "a.txt").write_text("changed\n")
    [ws] = find_workspaces(detect(jj_repo), Config())
    assert (ws.name, ws.path, ws.removable) == ("sandbox-main", path, False)
    assert "uncommitted" in ws.status
    assert main([str(jj_repo), "--cleanup-workspaces"]) == 0
    assert "kept sandbox-main" in capsys.readouterr().out
    assert (path / "a.txt").read_text() == "changed\n"


def test_cleanup_finds_workspaces_without_branches(jj_repo: Path, capsys) -> None:
    # Named unlike any bookmark and outside the default workspace directory.
    path = jj_repo.parent / "elsewhere"
    run("jj", "workspace", "add", str(path), "--name", "scratch", "-r", "main", cwd=jj_repo)
    assert main([str(jj_repo), "--cleanup-workspaces", "--dry-run"]) == 0
    assert f"would remove scratch: {path}" in capsys.readouterr().out
    assert main([str(jj_repo), "--cleanup-workspaces"]) == 0
    assert not path.exists()
    assert "scratch" not in run("jj", "workspace", "list", cwd=jj_repo)


def test_cleanup_never_offers_the_main_jj_workspace(jj_repo: Path, capsys) -> None:
    other = jj_repo.parent / "other"
    run("jj", "workspace", "add", str(other), "--name", "other", "-r", "main", cwd=jj_repo)
    assert main([str(other), "--cleanup-workspaces"]) == 0
    assert "kept default" in capsys.readouterr().out
    assert (jj_repo / ".jj" / "repo").is_dir()


def _scan(repo: Path, **project: str) -> dict[str, Workspace]:
    cfg = Config()
    for key, value in project.items():
        setattr(cfg.project, key, value)
    return {ws.name: ws for ws in find_workspaces(detect(repo), cfg)}


def test_merged_jj_workspace_and_bookmarks(jj_repo: Path, capsys) -> None:
    run("jj", "bookmark", "create", "--quiet", "feat", "-r", "main", cwd=jj_repo)
    run("jj", "bookmark", "create", "--quiet", "old", "-r", "main", cwd=jj_repo)
    path = jj_repo.parent / "feat-ws"
    run("jj", "workspace", "add", "--quiet", str(path), "--name", "feat", "-r", "feat", cwd=jj_repo)
    (path / ".gitignore").write_text("cache/\n")
    run("jj", "commit", "--quiet", "-m", "ignore cache", cwd=path)
    (path / "cache").mkdir()
    (path / "cache" / "x").write_text("x")
    rows = _scan(jj_repo)
    assert set(rows) == {"feat", "old"}
    assert not rows["feat"].removable  # its commit is not in main yet
    run("jj", "bookmark", "set", "--quiet", "main", "-r", "feat@-", cwd=jj_repo)
    rows = _scan(jj_repo)
    assert rows["feat"].removable and rows["feat"].branch == "feat"
    assert rows["old"].removable and rows["old"].path is None
    assert main([str(jj_repo), "--cleanup-workspaces"]) == 0
    out = capsys.readouterr().out
    assert "deleted bookmark feat" in out and "deleted bookmark old" in out
    assert not path.exists()
    assert "main" in run("jj", "bookmark", "list", cwd=jj_repo)
    assert "feat" not in run("jj", "bookmark", "list", cwd=jj_repo)


def test_merged_bookmark_without_workspace(repo: Path, capsys) -> None:
    run("jj", "bookmark", "create", "--quiet", "old", "-r", "main", cwd=repo)
    rows = _scan(repo)
    assert "feature/x" not in rows  # not merged
    assert set(rows) == {"old"} and rows["old"].path is None and rows["old"].removable
    run("jj", "bookmark", "set", "--quiet", "main", "-r", "feature/x", cwd=repo)
    assert set(_scan(repo)) == {"old", "feature/x"}
    # A configured main bookmark replaces the main/master/trunk guess.
    assert set(_scan(repo, main_branch="old")) == set()
    assert set(_scan(repo, main_branch="nope")) == set()
    assert main([str(repo), "--cleanup-workspaces", "--dry-run"]) == 0
    assert "would delete bookmark old" in capsys.readouterr().out
    assert main([str(repo), "--cleanup-workspaces"]) == 0
    assert "deleted bookmark old" in capsys.readouterr().out
    names = run("jj", "bookmark", "list", "-T", 'name ++ "\\n"', cwd=repo).split()
    assert names == ["main"]


def test_missing_jj_workspace_forgotten_once_merged(jj_repo: Path) -> None:
    path = jj_repo.parent / "gone"
    run("jj", "workspace", "add", "--quiet", str(path), "--name", "gone", "-r", "main", cwd=jj_repo)
    shutil.rmtree(path)
    ws = _scan(jj_repo)["gone"]
    assert ws.removable and "directory not found" in ws.status
    # jj forgets the root of a deleted workspace: the path is the project-sandbox guess.
    assert remove_workspaces(detect(jj_repo), Config(), [ws]) == [f"removed gone: {ws.path}"]
    assert "gone" not in run("jj", "workspace", "list", cwd=jj_repo)


def test_jj_workspace_with_unmerged_commit_is_kept(jj_repo: Path) -> None:
    path = jj_repo.parent / "work"
    run("jj", "workspace", "add", "--quiet", str(path), "--name", "work", "-r", "main", cwd=jj_repo)
    (path / "new.txt").write_text("new\n")
    run("jj", "commit", "--quiet", "-m", "work", cwd=path)
    shutil.rmtree(path)
    assert not _scan(jj_repo)["work"].removable


def test_jj_files_too_large_to_snapshot_are_kept(jj_repo: Path) -> None:
    path = jj_repo.parent / "big"
    run("jj", "workspace", "add", "--quiet", str(path), "--name", "big", "-r", "main", cwd=jj_repo)
    (path / "big.bin").write_bytes(b"\0" * (2 << 20))
    for _ in range(2):  # the warning must show on every scan, not only the first snapshot
        ws = _scan(jj_repo)["big"]
        assert not ws.removable and "too large" in ws.status


def test_force_remove_stale_jj_workspace(jj_repo: Path) -> None:
    path = jj_repo.parent / "stale"
    run(
        "jj", "workspace", "add", "--quiet", str(path), "--name", "stale", "-r", "main", cwd=jj_repo
    )
    (path / "b.txt").write_text("b\n")
    run("jj", "commit", "--quiet", "-m", "work", cwd=path)
    # Rewrite the workspace's @ from the default workspace: it becomes stale.
    (jj_repo / "x.txt").write_text("x\n")
    run("jj", "squash", "--quiet", "--into", "stale@", cwd=jj_repo)
    ws = _scan(jj_repo)["stale"]
    assert not ws.removable and ws.forceable and "stale" in ws.status
    assert force_remove_workspace(detect(jj_repo), Config(), ws) == f"force-removed stale: {path}"
    assert not path.exists()
    assert "stale" not in run("jj", "workspace", "list", cwd=jj_repo)


def test_force_remove_never_deletes_the_repository(jj_repo: Path) -> None:
    other = jj_repo.parent / "other"
    run(
        "jj",
        "workspace",
        "add",
        "--quiet",
        str(other),
        "--name",
        "other",
        "-r",
        "main",
        cwd=jj_repo,
    )
    ws = _scan(other)["default"]
    assert not ws.forceable
    result = force_remove_workspace(detect(other), Config(), ws)
    assert result.startswith("skipped default")
    assert (jj_repo / ".jj" / "repo").is_dir()
    # Forging the flag does not help: the re-scan decides.
    forged = Workspace(ws.name, ws.path, ws.status, forceable=True)
    assert force_remove_workspace(detect(other), Config(), forged).startswith("skipped")
    assert (jj_repo / ".jj" / "repo").is_dir()


def test_force_remove_jj_refuses_foreign_directory(jj_repo: Path) -> None:
    # jj forgot the root of this workspace; the guessed path holds something else.
    path = workspace_path(jj_repo, "ghost", None)
    path.parent.mkdir()
    run(
        "jj", "workspace", "add", "--quiet", str(path), "--name", "ghost", "-r", "main", cwd=jj_repo
    )
    shutil.rmtree(path)
    path.mkdir()
    (path / "precious").write_text("keep")
    ws = _scan(jj_repo)["ghost"]
    result = force_remove_workspace(detect(jj_repo), Config(), ws)
    assert "not a workspace of this repository" in result
    assert (path / "precious").exists()


@pytest.mark.parametrize("empty_revisions", [0, 1, 3])
def test_jj_merged_latest_nonempty_revision(jj_repo: Path, empty_revisions: int) -> None:
    path = jj_repo.parent / "finished"
    run("jj", "workspace", "add", str(path), "--name", "finished", "-r", "main", cwd=jj_repo)
    (path / "done.txt").write_text("done\n")
    run("jj", "describe", "-m", "finished work", cwd=path)
    run("jj", "bookmark", "set", "main", "-r", "finished@", cwd=jj_repo)
    for i in range(empty_revisions):
        run("jj", "new", "-m", f"empty revision {i}", cwd=path)
    ws = _scan(jj_repo)["finished"]
    assert ws.removable and "merged into main" in ws.status
    assert remove_workspaces(detect(jj_repo), Config(), [ws]) == [f"removed finished: {path}"]
    assert not path.exists()


def test_jj_empty_revisions_do_not_hide_unmerged_work(jj_repo: Path) -> None:
    path = jj_repo.parent / "unfinished"
    run("jj", "workspace", "add", str(path), "--name", "unfinished", "-r", "main", cwd=jj_repo)
    (path / "work.txt").write_text("work\n")
    run("jj", "commit", "-m", "unmerged work", cwd=path)
    (path / ".gitignore").write_text("cache/\n")
    run("jj", "commit", "-m", "ignore cache", cwd=path)
    run("jj", "describe", "-m", "empty but described", cwd=path)
    (path / "cache").mkdir()
    (path / "cache" / "data").write_text("keep")
    assert not _scan(jj_repo)["unfinished"].removable
    assert path.exists()


def test_force_removal_details_lists_contents_without_following_symlinks(tmp_path: Path) -> None:
    from project_sandbox_tui.cleanup import force_removal_details

    path = tmp_path / "workspace"
    path.mkdir()
    (path / "nested").mkdir()
    (path / "nested" / "ignored.txt").write_text("lost")
    (path / "empty").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("kept")
    (path / "link").symlink_to(outside, target_is_directory=True)
    details = force_removal_details(Workspace("work", path, "dirty"))
    assert "Unregister workspace: work" in details
    assert f"Delete directory and all contents: {path}" in details
    assert "nested/ignored.txt" in details and "empty/" in details
    assert f"link -> {outside} (symlink)" in details
    assert "precious.txt" not in details


def test_jj_new_changes_after_merge_are_kept(jj_repo: Path) -> None:
    path = jj_repo.parent / "changed"
    run("jj", "workspace", "add", str(path), "--name", "changed", "-r", "main", cwd=jj_repo)
    (path / "done.txt").write_text("done\n")
    run("jj", "describe", "-m", "finished work", cwd=path)
    run("jj", "bookmark", "set", "main", "-r", "changed@", cwd=jj_repo)
    run("jj", "new", cwd=path)
    chosen = _scan(jj_repo)["changed"]
    assert chosen.removable
    (path / "done.txt").write_text("unsnapshotted changes\n")
    # The user's choice is stale: removal must inspect the new changes itself.
    result = remove_workspaces(detect(jj_repo), Config(), [chosen])[0]
    assert result.startswith("skipped") and "uncommitted" in result
    assert (path / "done.txt").read_text() == "unsnapshotted changes\n"


@pytest.mark.parametrize("ignored", [False, True])
@pytest.mark.parametrize("target_kind", ["directory", "file", "missing"])
def test_jj_cleanup_symlinks(jj_repo: Path, ignored: bool, target_kind: str) -> None:
    if ignored:
        (jj_repo / ".gitignore").write_text("link\n")
        run("jj", "commit", "-m", "ignore link", cwd=jj_repo)
        run("jj", "bookmark", "set", "main", "-r", "@-", cwd=jj_repo)
    path = jj_repo.parent / "linked"
    run("jj", "workspace", "add", str(path), "--name", "linked", "-r", "main", cwd=jj_repo)
    target = jj_repo.parent / "target"
    if target_kind == "directory":
        target.mkdir()
        (target / "precious.txt").write_text("keep")
    elif target_kind == "file":
        target.write_text("keep")
    (path / "link").symlink_to(target, target_is_directory=target_kind == "directory")
    if not ignored:
        # A new tracked link is real unmerged work and must still be kept.
        assert not _scan(jj_repo)["linked"].removable
        run("jj", "bookmark", "set", "main", "-r", "linked@", cwd=jj_repo)
        run("jj", "new", cwd=path)
    assert run("jj", "log", "--no-graph", "-r", "@", "-T", "empty", cwd=path) == "true"
    ws = _scan(jj_repo)["linked"]
    assert ws.removable
    if ignored:
        assert "ignored files are deleted" in ws.status
    assert remove_workspaces(detect(jj_repo), Config(), [ws]) == [f"removed linked: {path}"]
    assert not path.exists()
    if target_kind == "directory":
        assert (target / "precious.txt").read_text() == "keep"
    elif target_kind == "file":
        assert target.read_text() == "keep"
    else:
        assert not target.exists()


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("source", ["job", "session"])
def test_cleanup_rechecks_running_sessions_after_selection(
    repo: Path, force: bool, source: str, tmp_path: Path
) -> None:
    import json
    import os

    from project_sandbox_tui.jobs import JobManager
    from project_sandbox_tui.sandbox import LaunchSpec

    path = _add_workspace(repo, "feature/x")
    if force:
        (path / "work.txt").write_text("keep")
    cfg = Config()
    backend = detect(repo)
    [chosen] = find_workspaces(backend, cfg)
    assert chosen.forceable if force else chosen.removable
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    if source == "job":
        JobManager(repo, cfg).record_external(LaunchSpec("feature/x", "claude"), ["sandbox"])
    else:
        (sessions / "active.json").write_text(
            json.dumps(
                {
                    "status": "running",
                    "pid": os.getpid(),
                    "workspace_path": str(path),
                }
            )
        )
    if force:
        result = force_remove_workspace(backend, cfg, chosen, sessions_dir=sessions)
    else:
        [result] = remove_workspaces(backend, cfg, [chosen], sessions_dir=sessions)
    assert result.startswith("skipped") and "running session" in result
    assert path.exists()
    assert "feature/x" in run("jj", "workspace", "list", cwd=repo)
    assert "feature/x" in run("jj", "bookmark", "list", cwd=repo)


def test_cleanup_rechecks_moved_bookmark(repo: Path) -> None:
    run("jj", "bookmark", "create", "old", "-r", "main", cwd=repo)
    backend, cfg = detect(repo), Config()
    [chosen] = find_workspaces(backend, cfg)
    assert chosen.branch == "old" and chosen.removable
    run("jj", "bookmark", "move", "old", "--to", "feature/x", cwd=repo)
    assert remove_workspaces(backend, cfg, [chosen]) == ["skipped bookmark old: no longer found"]
    assert backend._bookmark_commit("old") == backend._bookmark_commit("feature/x")


def test_cleanup_keeps_unmerged_bookmark_of_merged_workspace(repo: Path) -> None:
    path = _add_workspace(repo, "feature/x")
    run("jj", "new", "main", cwd=path)
    backend, cfg = detect(repo), Config()
    commit = backend._bookmark_commit("feature/x")
    [chosen] = find_workspaces(backend, cfg)
    assert chosen.removable and not chosen.branch
    assert remove_workspaces(backend, cfg, [chosen]) == [f"removed feature/x: {path}"]
    assert backend._bookmark_commit("feature/x") == commit


def test_cleanup_rechecks_bookmark_of_selected_workspace(repo: Path) -> None:
    run("jj", "bookmark", "create", "old", "-r", "main", cwd=repo)
    path = _add_workspace(repo, "old")
    backend, cfg = detect(repo), Config()
    [chosen] = find_workspaces(backend, cfg)
    assert chosen.branch == "old"
    run("jj", "bookmark", "move", "old", "--to", "feature/x", cwd=repo)
    assert remove_workspaces(backend, cfg, [chosen]) == [f"removed old: {path}"]
    assert backend._bookmark_commit("old") == backend._bookmark_commit("feature/x")


@pytest.mark.parametrize("force", [False, True])
def test_cleanup_rechecks_nested_workspaces(jj_repo: Path, force: bool) -> None:
    (jj_repo / ".gitignore").write_text("nested/\n")
    run("jj", "commit", "-m", "ignore nested workspace", cwd=jj_repo)
    run("jj", "bookmark", "set", "main", "-r", "@-", cwd=jj_repo)
    outer = _add_workspace(jj_repo, "main")
    backend, cfg = detect(jj_repo), Config()
    [chosen] = find_workspaces(backend, cfg)
    assert chosen.removable
    nested = outer / "nested"
    run("jj", "workspace", "add", str(nested), "--name", "nested", "-r", "main", cwd=jj_repo)
    (nested / "precious.txt").write_text("uncommitted work")
    if force:
        result = force_remove_workspace(backend, cfg, chosen)
    else:
        [result] = remove_workspaces(backend, cfg, [chosen])
    assert result.startswith("skipped") and "contains another workspace" in result
    assert (nested / "precious.txt").read_text() == "uncommitted work"
    assert "nested" in run("jj", "workspace", "list", cwd=jj_repo)


@pytest.mark.parametrize("force", [False, True])
def test_cleanup_protects_nested_running_session(repo: Path, monkeypatch, force: bool) -> None:
    import os

    from project_sandbox_tui.sandbox import SessionRecord

    outer = _add_workspace(repo, "feature/x")
    backend, cfg = detect(repo), Config()
    [chosen] = find_workspaces(backend, cfg)
    record = SessionRecord(
        "nested", "container", str(outer), str(outer / "nested"), "bash", "running", os.getpid(), ""
    )
    monkeypatch.setattr("project_sandbox_tui.cleanup.read_sessions", lambda *_: [record])
    if force:
        result = force_remove_workspace(backend, cfg, chosen)
    else:
        [result] = remove_workspaces(backend, cfg, [chosen])
    assert result.startswith("skipped") and "running session" in result
    assert outer.exists()
