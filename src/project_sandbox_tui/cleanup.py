"""Find and remove idle, clean workspaces without forcing VCS removal."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from project_sandbox_tui.config import Config
from project_sandbox_tui.jobs import JobManager
from project_sandbox_tui.sandbox import SessionRecord, read_sessions
from project_sandbox_tui.vcs import Backend, VcsError, _run, workspace_path


@dataclass(frozen=True)
class Workspace:
    """A git worktree / jj workspace other than the one the TUI runs in."""

    name: str  # git: checked-out branch (or directory name when detached); jj: workspace name
    path: Path
    status: str  # "clean", or why it cannot be removed
    removable: bool = False

    @property
    def describe(self) -> str:
        return f"{self.name}: {self.path} ({self.status})"


def _git_worktrees(repo: Path) -> list[tuple[str, Path, str]]:
    """(name, path, problem) for every linked worktree; the main worktree is skipped."""
    out = _run(["git", "worktree", "list", "--porcelain"], repo)
    found: list[tuple[str, Path, str]] = []
    for block in out.strip().split("\n\n")[1:]:
        fields = dict(
            (line.split(" ", 1) + [""])[:2] for line in block.splitlines() if line.strip()
        )
        if "worktree" not in fields or "bare" in fields:
            continue
        path = Path(fields["worktree"])
        if path.exists() and path.resolve() == repo:
            continue  # the TUI runs in this linked worktree
        name = fields.get("branch", "").removeprefix("refs/heads/") or f"{path.name} (detached)"
        problem = ""
        if "locked" in fields:
            problem = f"locked{': ' + fields['locked'] if fields['locked'] else ''}"
        elif "prunable" in fields or not path.exists():
            problem = "directory not found (run `git worktree prune` if it was deleted)"
        found.append((name, path, problem))
    return found


def _jj_store(root: Path) -> Path:
    """The shared repo store of a jj workspace (secondary workspaces hold a pointer file)."""
    store = root / ".jj" / "repo"
    if store.is_file():
        store = store.parent / store.read_text().strip()
    return store.resolve()


def _jj_workspaces(repo: Path, wd: Path | None) -> list[tuple[str, Path, str]]:
    """(name, path, problem) for every jj workspace except the current one."""
    base = ["jj", "--no-pager", "--color=never", "--ignore-working-copy", "workspace", "list"]
    current = 'if(target.current_working_copy(), "1", "0")'
    try:
        out = _run([*base, "-T", f'name ++ "\\x00" ++ root ++ "\\x00" ++ {current} ++ "\\n"'], repo)
    except VcsError:  # jj without the ``root`` template keyword
        out = _run([*base, "-T", f'name ++ "\\x00\\x00" ++ {current} ++ "\\n"'], repo)
    store = _jj_store(repo)
    found: list[tuple[str, Path, str]] = []
    for line in out.splitlines():
        name, root, is_current = (line.split("\0") + ["", ""])[:3]
        if is_current == "1":
            continue
        # Unknown root: assume project-sandbox's layout (workspaces are named after bookmarks).
        path = Path(root) if root else workspace_path(repo, "jj", name, wd)
        problem = ""
        if not path.exists():
            problem = f"directory not found (expected at {path})"
        elif path.resolve() == repo or store.is_relative_to(path.resolve()):
            problem = "holds the repository itself"
        found.append((name, path, problem))
    return found


def _dirty(backend: Backend, repo: Path, path: Path) -> str:
    """Why ``path`` must be kept, or "" when it holds nothing that removal would lose."""
    if backend.kind == "git":
        registered = _run(["git", "worktree", "list", "--porcelain"], repo)
        paths = {
            Path(line.removeprefix("worktree ")).resolve()
            for line in registered.splitlines()
            if line.startswith("worktree ")
        }
        if path.resolve() not in paths:
            raise VcsError("not a registered worktree")
        return _run(["git", "status", "--porcelain", "--ignored"], path).strip()
    # Snapshot before inspecting: never discard unsnapshotted jj changes.
    owner = _run(["jj", "root"], path).strip()
    if Path(owner).resolve() != path.resolve():
        raise VcsError("not a workspace")
    if _jj_store(path) != _jj_store(repo):
        raise VcsError("workspace belongs to another repository")
    dirty = _run(["jj", "diff", "--summary"], path).strip()
    tracked = set(_run(["jj", "file", "list"], path).splitlines())
    # jj ignores some files when snapshotting; preserve those too.
    for directory, dirs, files in os.walk(path):
        if Path(directory) == path:
            dirs[:] = [d for d in dirs if d not in {".jj", ".git"}]
        for file in files:
            if str((Path(directory) / file).relative_to(path)) not in tracked:
                dirty = "untracked files"
        if any((Path(directory) / d).is_symlink() for d in dirs):
            dirty = "untracked directory symlinks"
    return dirty


def _inspect(
    backend: Backend,
    repo: Path,
    jobs: JobManager,
    sessions: list[SessionRecord],
    wd: Path | None,
    name: str,
    path: Path,
    problem: str,
) -> Workspace:
    if problem:
        return Workspace(name, path, problem)
    if path.is_symlink() or path.resolve() == repo or path.resolve() in repo.parents:
        return Workspace(name, path, "protected path")
    job = jobs.jobs.get(name)
    busy = {Path(r.workspace_path).resolve() for r in sessions if r.running and r.workspace_path}
    busy |= {
        workspace_path(repo, backend.kind, j.branch, wd).resolve()
        for j in jobs.jobs.values()
        if j.running
    }
    if (job is not None and job.running) or path.resolve() in busy:
        return Workspace(name, path, "running session")
    try:
        if _dirty(backend, repo, path):
            return Workspace(name, path, "uncommitted, untracked or ignored files")
    except (VcsError, OSError) as exc:
        return Workspace(name, path, str(exc))
    return Workspace(name, path, "clean", removable=True)


def _scan(
    backend: Backend, cfg: Config, jobs: JobManager, sessions_dir: Path | None
) -> list[Workspace]:
    repo = backend.root.resolve()
    wd = cfg.resolve_path(cfg.project.worktree_dir, repo) if cfg.project.worktree_dir else None
    found = _git_worktrees(repo) if backend.kind == "git" else _jj_workspaces(repo, wd)
    sessions = read_sessions(sessions_dir)
    found_ws = [_inspect(backend, repo, jobs, sessions, wd, *entry) for entry in found]
    return sorted(found_ws, key=lambda ws: (not ws.removable, ws.name))


def find_workspaces(
    backend: Backend,
    cfg: Config,
    *,
    state_dir: Path | None = None,
    sessions_dir: Path | None = None,
) -> list[Workspace]:
    """Every workspace except the TUI's own, with whether it can be removed safely."""
    jobs = JobManager(backend.root.resolve(), cfg, state_dir)
    with jobs._locked():
        jobs.load()
        return _scan(backend, cfg, jobs, sessions_dir)


def remove_workspaces(
    backend: Backend,
    cfg: Config,
    paths: Iterable[Path],
    *,
    dry_run: bool = False,
    state_dir: Path | None = None,
    sessions_dir: Path | None = None,
) -> list[str]:
    """Remove the workspaces at ``paths``, re-checking each one first; returns a report."""
    repo = backend.root.resolve()
    wanted = {Path(p).resolve() for p in paths}
    jobs = JobManager(repo, cfg, state_dir)
    results: list[str] = []
    # Share the launch lock so another TUI cannot start a job during removal.
    with jobs._locked():
        jobs.load()
        for ws in _scan(backend, cfg, jobs, sessions_dir):
            if not ws.path.exists() or ws.path.resolve() not in wanted:
                continue
            wanted.discard(ws.path.resolve())
            if not ws.removable:
                results.append(f"skipped {ws.describe}")
                continue
            try:
                if not dry_run:
                    if backend.kind == "git":
                        _run(["git", "worktree", "remove", "--", str(ws.path)], repo)
                    else:
                        _run(["jj", "workspace", "forget", "--", ws.name], repo)
                        shutil.rmtree(ws.path)
                results.append(f"{'would remove' if dry_run else 'removed'} {ws.name}: {ws.path}")
            except (VcsError, OSError) as exc:
                results.append(f"skipped {ws.name}: {ws.path} ({exc})")
    results += [f"skipped {path}: no longer a workspace" for path in sorted(wanted)]
    return results


def cleanup_workspaces(
    backend: Backend,
    cfg: Config,
    *,
    dry_run: bool = False,
    state_dir: Path | None = None,
    sessions_dir: Path | None = None,
) -> list[str]:
    """Remove every removable workspace; report on every workspace that was found."""
    found = find_workspaces(backend, cfg, state_dir=state_dir, sessions_dir=sessions_dir)
    removable = [ws.path for ws in found if ws.removable]
    results = remove_workspaces(
        backend, cfg, removable, dry_run=dry_run, state_dir=state_dir, sessions_dir=sessions_dir
    )
    return results + [f"kept {ws.describe}" for ws in found if not ws.removable]
