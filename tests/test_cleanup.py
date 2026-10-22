import shutil
from pathlib import Path

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


def test_cleanup_git(git_repo: Path, capsys) -> None:
    path = workspace_path(git_repo, "git", "feature/x", None)
    run("git", "worktree", "add", str(path), "feature/x", cwd=git_repo)
    assert main([str(git_repo), "--cleanup-workspaces", "--dry-run"]) == 0
    assert "would remove feature/x" in capsys.readouterr().out
    assert path.exists()
    (path / "untracked").write_text("keep")
    assert main([str(git_repo), "--cleanup-workspaces"]) == 0
    assert "kept feature/x" in capsys.readouterr().out
    assert path.exists()
    (path / "untracked").unlink()
    assert main([str(git_repo), "--cleanup-workspaces"]) == 0
    assert "removed feature/x" in capsys.readouterr().out
    assert not path.exists()
    assert (git_repo / "a.txt").exists()
    assert "feature/x" in run("git", "branch", cwd=git_repo)


def test_cleanup_jj(jj_repo: Path, capsys) -> None:
    path = workspace_path(jj_repo, "jj", "main", None)
    path.parent.mkdir()
    run("jj", "workspace", "add", str(path), "--name", "sandbox-main", "-r", "main", cwd=jj_repo)
    assert main([str(jj_repo), "--cleanup-workspaces"]) == 0
    assert f"removed sandbox-main: {path}" in capsys.readouterr().out
    assert not path.exists()
    assert "sandbox-main" not in run("jj", "workspace", "list", cwd=jj_repo)


def test_cleanup_preserves_running_session(git_repo: Path, capsys, monkeypatch) -> None:
    import os

    from project_sandbox_tui.sandbox import SessionRecord

    path = workspace_path(git_repo, "git", "feature/x", None)
    run("git", "worktree", "add", str(path), "feature/x", cwd=git_repo)
    record = SessionRecord(
        "test", "test", str(git_repo), str(path), "bash", "running", os.getpid(), ""
    )
    monkeypatch.setattr("project_sandbox_tui.cleanup.read_sessions", lambda *_: [record])
    assert main([str(git_repo), "--cleanup-workspaces"]) == 0
    assert "running session" in capsys.readouterr().out
    assert path.exists()


def test_cleanup_preserves_running_job(git_repo: Path, capsys) -> None:
    import os

    from project_sandbox_tui.config import Config
    from project_sandbox_tui.jobs import JobManager, JobRecord
    from project_sandbox_tui.sandbox import LaunchSpec

    path = workspace_path(git_repo, "git", "feature/x", None)
    run("git", "worktree", "add", str(path), "feature/x", cwd=git_repo)
    JobManager(git_repo, Config()).save(
        JobRecord("feature/x", LaunchSpec("feature/x", "bash"), os.getpid(), "now")
    )
    assert main([str(git_repo), "--cleanup-workspaces"]) == 0
    assert "running session" in capsys.readouterr().out
    assert path.exists()


def test_cleanup_jj_preserves_files(jj_repo: Path, capsys) -> None:
    path = workspace_path(jj_repo, "jj", "main", None)
    path.parent.mkdir()
    run("jj", "workspace", "add", str(path), "--name", "sandbox-main", "-r", "main", cwd=jj_repo)
    (path / "a.txt").write_text("changed\n")
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


def test_cleanup_git_detached_and_missing(git_repo: Path, capsys) -> None:
    detached = git_repo.parent / "detached"
    gone = git_repo.parent / "gone"
    run("git", "worktree", "add", "--detach", str(detached), "main", cwd=git_repo)
    run("git", "worktree", "add", str(gone), "feature/x", cwd=git_repo)
    shutil.rmtree(gone)
    assert main([str(git_repo), "--cleanup-workspaces", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"would remove detached (detached): {detached}" in out
    assert "kept feature/x" in out and "directory not found" in out


def test_find_workspaces_reports_status(jj_repo: Path) -> None:
    path = workspace_path(jj_repo, "jj", "main", None)
    path.parent.mkdir()
    run("jj", "workspace", "add", str(path), "--name", "main", "-r", "main", cwd=jj_repo)
    (path / "a.txt").write_text("changed\n")
    [ws] = find_workspaces(detect(jj_repo), Config())
    assert (ws.name, ws.path, ws.removable) == ("main", path, False)
    assert "uncommitted" in ws.status


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


def _merge_feature(git_repo: Path) -> None:
    run("git", "merge", "-q", "--ff-only", "feature/x", cwd=git_repo)


def test_merged_git_worktree_with_ignored_files(git_repo: Path, capsys) -> None:
    (git_repo / ".gitignore").write_text(".venv/\n")
    run("git", "add", ".gitignore", cwd=git_repo)
    run("git", "commit", "-q", "-m", "ignore", cwd=git_repo)
    run("git", "checkout", "-q", "feature/x", cwd=git_repo)
    run("git", "merge", "-q", "main", cwd=git_repo)
    run("git", "checkout", "-q", "main", cwd=git_repo)
    path = workspace_path(git_repo, "git", "feature/x", None)
    run("git", "worktree", "add", str(path), "feature/x", cwd=git_repo)
    (path / ".venv").mkdir()
    (path / ".venv" / "lib").write_text("x")
    # Not merged yet: ignored files keep it.
    ws = _scan(git_repo)["feature/x"]
    assert not ws.removable and "ignored files" in ws.status
    _merge_feature(git_repo)
    ws = _scan(git_repo)["feature/x"]
    assert ws.removable and ws.branch == "feature/x"
    assert "merged into main" in ws.status and "deletes branch feature/x" in ws.status
    # Untracked files still keep a merged worktree.
    (path / "notes.txt").write_text("keep")
    assert not _scan(git_repo)["feature/x"].removable
    (path / "notes.txt").unlink()
    assert main([str(git_repo), "--cleanup-workspaces"]) == 0
    out = capsys.readouterr().out
    assert f"removed feature/x: {path}" in out and "deleted branch feature/x" in out
    assert not path.exists()
    assert "feature/x" not in run("git", "branch", cwd=git_repo)


def test_merged_git_branch_without_worktree(git_repo: Path, capsys) -> None:
    run("git", "branch", "old", "main", cwd=git_repo)
    rows = _scan(git_repo)
    assert "feature/x" not in rows  # not merged
    assert set(rows) == {"old"} and rows["old"].path is None and rows["old"].removable
    _merge_feature(git_repo)
    assert set(_scan(git_repo)) == {"old", "feature/x"}
    # A configured main branch replaces the main/master/trunk guess.
    assert set(_scan(git_repo, main_branch="old")) == set()
    assert set(_scan(git_repo, main_branch="nope")) == set()
    assert main([str(git_repo), "--cleanup-workspaces", "--dry-run"]) == 0
    assert "would delete branch old" in capsys.readouterr().out
    assert main([str(git_repo), "--cleanup-workspaces"]) == 0
    assert "deleted branch old" in capsys.readouterr().out
    assert run("git", "branch", "--format=%(refname:short)", cwd=git_repo).split() == ["main"]


def test_missing_git_worktree_removed_once_merged(git_repo: Path, capsys) -> None:
    gone = git_repo.parent / "gone"
    run("git", "worktree", "add", str(gone), "feature/x", cwd=git_repo)
    shutil.rmtree(gone)
    assert not _scan(git_repo)["feature/x"].removable
    _merge_feature(git_repo)
    ws = _scan(git_repo)["feature/x"]
    assert ws.removable and "directory not found" in ws.status
    assert main([str(git_repo), "--cleanup-workspaces"]) == 0
    assert "removed feature/x" in capsys.readouterr().out
    assert "gone" not in run("git", "worktree", "list", cwd=git_repo)
    assert "feature/x" not in run("git", "branch", cwd=git_repo)


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


def test_force_remove_dirty_locked_git_worktree(git_repo: Path) -> None:
    path = workspace_path(git_repo, "git", "feature/x", None)
    run("git", "worktree", "add", str(path), "feature/x", cwd=git_repo)
    (path / "untracked").write_text("lost")
    run("git", "worktree", "lock", str(path), cwd=git_repo)
    ws = _scan(git_repo)["feature/x"]
    assert not ws.removable and ws.forceable
    result = force_remove_workspace(detect(git_repo), Config(), ws)
    assert result == f"force-removed feature/x: {path}"
    assert not path.exists()
    assert str(path) not in run("git", "worktree", "list", cwd=git_repo)
    assert "feature/x" in run("git", "branch", cwd=git_repo)  # branches are kept


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
    path = workspace_path(jj_repo, "jj", "ghost", None)
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
