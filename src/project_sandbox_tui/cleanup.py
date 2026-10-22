"""Remove idle, clean branch workspaces without forcing VCS removal."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from project_sandbox_tui.config import Config
from project_sandbox_tui.jobs import JobManager
from project_sandbox_tui.sandbox import read_sessions
from project_sandbox_tui.vcs import Backend, VcsError, _run, workspace_path


def cleanup_workspaces(backend: Backend, cfg: Config, *, dry_run: bool = False) -> list[str]:
    """Clean known branch workspaces; preserve checkouts, busy branches and changes."""
    repo = backend.root.resolve()
    wd = cfg.resolve_path(cfg.project.worktree_dir, repo) if cfg.project.worktree_dir else None
    jobs = JobManager(repo, cfg)
    results: list[str] = []
    # Share the launch lock so another TUI cannot start a job during removal.
    with jobs._locked():
        jobs.load()
        for branch in backend.list_branches():
            if branch.working_copy:
                continue
            path = workspace_path(repo, backend.kind, branch.name, wd)
            if not path.exists():
                continue
            if path.is_symlink() or path.resolve() == repo or path.resolve() in repo.parents:
                results.append(f"skipped {branch.name}: protected path")
                continue
            job = jobs.jobs.get(branch.name)
            busy = job is not None and job.running
            busy = busy or any(
                rec.running
                and rec.workspace_path
                and Path(rec.workspace_path).resolve() == path.resolve()
                for rec in read_sessions()
            )
            if busy:
                results.append(f"skipped {branch.name}: running session")
                continue
            try:
                if backend.kind == "git":
                    # Verify this is a registered worktree of this repository.
                    registered = _run(["git", "worktree", "list", "--porcelain"], repo)
                    paths = {
                        Path(line.removeprefix("worktree ")).resolve()
                        for line in registered.splitlines()
                        if line.startswith("worktree ")
                    }
                    if path.resolve() not in paths:
                        raise VcsError("not a registered worktree")
                    checked_out = _run(["git", "symbolic-ref", "--short", "HEAD"], path).strip()
                    if checked_out != branch.name:
                        raise VcsError("worktree is checked out on another branch")
                    dirty = _run(["git", "status", "--porcelain", "--ignored"], path).strip()
                else:
                    # Snapshot before inspecting: never discard unsnapshotted jj changes.
                    owner = _run(["jj", "root"], path).strip()
                    if Path(owner).resolve() != path.resolve():
                        raise VcsError("not a workspace")
                    store = path / ".jj" / "repo"
                    if (
                        not store.is_file()
                        or (store.parent / store.read_text().strip()).resolve()
                        != (repo / ".jj" / "repo").resolve()
                    ):
                        raise VcsError("workspace belongs to another repository")
                    dirty = _run(["jj", "diff", "--summary"], path).strip()
                    tracked = set(_run(["jj", "file", "list"], path).splitlines())
                    # jj ignores some files when snapshotting; preserve those too.
                    for directory, dirs, files in os.walk(path):
                        if Path(directory) == path:
                            dirs[:] = [d for d in dirs if d not in {".jj", ".git"}]
                        for name in files:
                            if str((Path(directory) / name).relative_to(path)) not in tracked:
                                dirty = "untracked files"
                        if any((Path(directory) / d).is_symlink() for d in dirs):
                            dirty = "untracked directory symlinks"
                if dirty:
                    results.append(f"skipped {branch.name}: uncommitted or untracked files")
                    continue
                if not dry_run:
                    if backend.kind == "git":
                        _run(["git", "worktree", "remove", "--", str(path)], repo)
                    else:
                        _run(["jj", "workspace", "forget"], path)
                        shutil.rmtree(path)
                results.append(f"{'would remove' if dry_run else 'removed'} {branch.name}: {path}")
            except (VcsError, OSError) as exc:
                results.append(f"skipped {branch.name}: {exc}")
    return results
