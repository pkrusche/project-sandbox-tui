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
    assert "skipped feature/x" in capsys.readouterr().out
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
    assert "removed main" in capsys.readouterr().out
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
    monkeypatch.setattr("project_sandbox_tui.cleanup.read_sessions", lambda: [record])
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
    assert "skipped main" in capsys.readouterr().out
    assert (path / "a.txt").read_text() == "changed\n"
