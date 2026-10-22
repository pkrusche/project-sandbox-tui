from pathlib import Path

from project_sandbox_tui.cli import main
from project_sandbox_tui.vcs import workspace_path
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
    import shutil

    shutil.rmtree(gone)
    assert main([str(git_repo), "--cleanup-workspaces", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"would remove detached (detached): {detached}" in out
    assert "kept feature/x" in out and "directory not found" in out


def test_find_workspaces_reports_status(jj_repo: Path) -> None:
    from project_sandbox_tui.cleanup import find_workspaces
    from project_sandbox_tui.config import Config
    from project_sandbox_tui.vcs import detect

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
