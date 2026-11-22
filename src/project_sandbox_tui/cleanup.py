"""Find and remove idle, clean or merged workspaces and merged bookmarks."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from project_sandbox_tui.config import Config
from project_sandbox_tui.jobs import JobManager
from project_sandbox_tui.sandbox import read_sessions
from project_sandbox_tui.tasks import TaskManager
from project_sandbox_tui.vcs import (
    JjBackend,
    VcsError,
    _run,
    _run_with_stderr,
    main_branch,
    registered_workspace_name,
    revset_symbol,
    tracked_files,
    workspace_path,
    workspace_store,
)


@dataclass(frozen=True)
class Workspace:
    """A jj workspace other than the TUI's own, or a merged bookmark."""

    name: str  # workspace name, or the bookmark of a merged bookmark without a workspace
    path: Path | None  # None: a merged bookmark without a workspace
    status: str  # "clean", "merged into ...", or why it cannot be removed
    removable: bool = False
    branch: str = ""  # bookmark deleted together with this row
    # Kept, but the user may force-delete it: not busy, not the repository itself.
    forceable: bool = False
    removes_ignored: bool = False
    missing: bool = False

    @property
    def describe(self) -> str:
        if self.path is None:
            return f"bookmark {self.name} ({self.status})"
        return f"{self.name}: {self.path} ({self.status})"


@dataclass(frozen=True)
class _Found:
    name: str
    path: Path
    problem: str = ""  # why it must be kept regardless of its contents
    missing: bool = False  # the directory is gone: nothing on disk to lose
    branch: str = ""  # the bookmark the workspace belongs to, if it exists
    protected: bool = False  # never deleted, not even by force


def _jj_workspaces(repo: Path, wd: Path | None, bookmarks: set[str]) -> list[_Found]:
    """Every jj workspace except the current one."""
    store = workspace_store(repo)
    # Workspaces are registered under their directory's name (``feature-x-1a2b3c``
    # for ``feature/x``); older ones under the bookmark name.
    by_path = {workspace_path(repo, b, wd).resolve(): b for b in bookmarks}
    found: list[_Found] = []
    for registration in JjBackend(repo).registered_workspaces():
        if registration.current:
            continue
        name = registration.name
        # Unknown root: assume project-sandbox's layout (workspaces are named after bookmarks).
        path = registration.root or workspace_path(repo, name, wd)
        missing = not path.exists()
        problem = ""
        if missing:
            problem = f"directory not found (expected at {path})"
        elif path.resolve() == repo or store.is_relative_to(path.resolve()):
            problem = "holds the repository itself"
        protected = problem == "holds the repository itself"
        branch = name if name in bookmarks else by_path.get(path.resolve(), "")
        found.append(_Found(name, path, problem, missing, branch, protected=protected))
    return found


def _jj(repo: Path, *args: str) -> str:
    return _run(["jj", "--no-pager", "--color=never", "--ignore-working-copy", *args], repo)


def _revset_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # a jj string literal


def _merged(repo: Path, main: str | None, ws: _Found) -> bool:
    """Whether everything the workspace holds in the VCS is already part of ``main``."""
    if not main:
        return False
    try:
        # Check the latest non-empty history, skipping any trailing empty revisions.
        # Keep all ancestry paths so an unmerged side of a merge is not overlooked.
        revset = f"(::{_revset_str(ws.name)}@ ~ ::{_revset_str(main)}) ~ empty()"
        return not _jj(repo, "log", "--no-graph", "-r", revset, "-T", "commit_id", "-n", "1")
    except VcsError:  # main / the workspace cannot be resolved
        return False


def _verify_workspace(repo: Path, path: Path, name: str) -> None:
    # jj may no longer know a missing workspace's root. A guessed path
    # can now belong to another workspace of the same repository.
    current = registered_workspace_name(repo, path)
    if current != name:
        raise VcsError(f"{path} is not the registered workspace {name}")


def _dirty(path: Path) -> tuple[str, bool]:
    """Snapshot before inspecting and report dirty and ignored files."""
    diff, warnings = _run_with_stderr(["jj", "diff", "--summary"], path)
    dirty = "uncommitted changes" if diff.strip() else ""
    if "Refused to snapshot" in warnings:
        dirty = "files too large for jj to snapshot"
    tracked = set(tracked_files(path, ["."]))
    # jj ignores some files when snapshotting; only removal of a merged workspace loses those.
    ignored = False

    def fail(exc: OSError) -> None:
        raise exc  # an unreadable subtree must never look like an empty one

    for directory, dirs, files in os.walk(path, onerror=fail):
        if Path(directory) == path:
            # Colocated workspaces use either a .git directory or a pointer file.
            dirs[:] = [d for d in dirs if d not in {".jj", ".git"}]
            files = [f for f in files if f not in {".jj", ".git"}]
        # os.walk lists symlinks to directories in dirs, but does not follow them.
        # Inspect the links themselves just like files, including jj tracking status.
        links = [d for d in dirs if (Path(directory) / d).is_symlink()]
        for file in [*files, *links]:
            if str((Path(directory) / file).relative_to(path)) not in tracked:
                ignored = True
    return dirty, ignored


def _inspect(
    repo: Path,
    main: str | None,
    busy: set[Path],
    jobs: JobManager,
    ws: _Found,
    merged_bookmarks: set[str],
    workspace_roots: set[Path],
    tasks: set[Path],
) -> Workspace:
    name, path = ws.name, ws.path

    def keep(why: str, forceable: bool = True) -> Workspace:
        return Workspace(name, path, why, forceable=forceable)

    def remove(status: str, merged: bool, *, ignored: bool = False) -> Workspace:
        # A workspace's working-copy revision and its namesake bookmark can
        # move independently. Never infer that the bookmark is merged from
        # the workspace's ancestry alone.
        branch = ws.branch if merged and ws.branch in merged_bookmarks else ""
        if merged:
            status = ", ".join(filter(None, (status, f"merged into {main}")))
        if branch:
            status += f" · deletes bookmark {branch}"
        return Workspace(
            name, path, status, True, branch, removes_ignored=ignored, missing=ws.missing
        )

    if ws.problem and not ws.missing:
        return keep(ws.problem, forceable=not ws.protected)
    if not ws.missing and (
        path.is_symlink() or path.resolve() == repo or path.resolve() in repo.parents
    ):
        return keep("protected path", forceable=False)
    resolved = path.resolve()
    if any(root != resolved and root.is_relative_to(resolved) for root in workspace_roots):
        return keep("contains another workspace", forceable=False)
    job = jobs.jobs.get(name)
    if (job is not None and job.running) or any(
        active.is_relative_to(resolved) or resolved.is_relative_to(active) for active in busy
    ):
        return keep("running session", forceable=False)
    if any(active.is_relative_to(resolved) for active in tasks):
        return keep("running :* task", forceable=False)
    if ws.missing:
        if _merged(repo, main, ws):
            return remove("directory not found", merged=True)
        return keep(ws.problem)
    try:
        _verify_workspace(repo, path, name)
    except (VcsError, OSError) as exc:
        return keep(str(exc), forceable=False)
    try:
        changes, ignored = _dirty(path)  # snapshots before _merged looks
        merged = _merged(repo, main, ws)
    except (VcsError, OSError) as exc:
        return keep(str(exc))
    # The snapshot lands in the working-copy revision: a diff already in main is safe to remove.
    if changes and not (merged and changes == "uncommitted changes"):
        return keep(changes)
    if ignored and not merged:
        return keep("ignored files (removable once merged into the main branch)")
    return remove("ignored files are deleted" if ignored else "clean", merged, ignored=ignored)


def _merged_bookmarks(
    repo: Path,
    main: str,
    busy: set[Path],
    jobs: JobManager,
    wd: Path | None,
) -> list[Workspace]:
    """Idle bookmarks fully merged into ``main``, including those with workspaces."""
    template = 'if(remote || conflict, "", stringify(name) ++ "\\n")'
    out = _jj(repo, "bookmark", "list", "-r", f"::{_revset_str(main)}", "-T", template)
    found: list[Workspace] = []
    for name in filter(None, out.splitlines()):
        job = jobs.jobs.get(name)
        if name == main or (job is not None and job.running):
            continue
        if workspace_path(repo, name, wd).resolve() in busy:
            continue
        found.append(Workspace(name, None, f"merged into {main}", True, name))
    return found


@dataclass(frozen=True)
class _Survey:
    """The repository-wide facts deciding about each workspace needs, gathered once."""

    repo: Path
    main: str | None
    jobs: JobManager
    found: list[_Found]  # every workspace except the current one
    roots: set[Path]  # their resolved paths
    busy: set[Path]  # workspaces of running sessions
    tasks: set[Path]  # directories of running :* tasks
    merged_rows: list[Workspace]  # idle bookmarks merged into main

    def inspect(self, ws: _Found) -> Workspace:
        """Whether ``ws`` can be removed, checking its files now."""
        merged = {row.branch for row in self.merged_rows}
        return _inspect(
            self.repo, self.main, self.busy, self.jobs, ws, merged, self.roots, self.tasks
        )

    def bookmark_rows(self) -> list[Workspace]:
        """Merged bookmarks without a workspace of their own."""
        taken = {ws.name for ws in self.found} | {ws.branch for ws in self.found}
        return [ws for ws in self.merged_rows if ws.name not in taken]


def _survey(
    backend: JjBackend,
    cfg: Config,
    jobs: JobManager,
    sessions_dir: Path | None,
    task_manager: TaskManager,
) -> _Survey:
    repo = backend.root.resolve()
    wd = cfg.resolve_path(cfg.project.worktree_dir, repo) if cfg.project.worktree_dir else None
    bookmarks = {b.name for b in backend.list_branches() if not b.working_copy}
    main = main_branch(bookmarks, cfg.project.main_branch)
    found = _jj_workspaces(repo, wd, bookmarks)
    busy = {
        Path(r.workspace_path).resolve()
        for r in read_sessions(sessions_dir)
        if r.running and r.workspace_path
    }
    busy |= {
        repo if j.spec.main_checkout else workspace_path(repo, j.branch, wd).resolve()
        for j in jobs.jobs.values()
        if j.running
    }
    # :* tasks only use a workspace's files, so they keep it but don't make it busy.
    tasks = {Path(t.cwd).resolve() for t in task_manager.tasks.values() if t.running}
    merged_rows = _merged_bookmarks(repo, main, busy | tasks, jobs, wd) if main else []
    roots = {ws.path.resolve() for ws in found}
    return _Survey(repo, main, jobs, found, roots, busy, tasks, merged_rows)


def _scan(
    backend: JjBackend, cfg: Config, jobs: JobManager, sessions_dir: Path | None
) -> list[Workspace]:
    tasks = TaskManager(jobs.state_dir)
    tasks.refresh()
    survey = _survey(backend, cfg, jobs, sessions_dir, tasks)
    rows = [survey.inspect(ws) for ws in survey.found] + survey.bookmark_rows()
    return sorted(rows, key=lambda ws: (not ws.removable, ws.path is None, ws.name))


def find_workspaces(
    backend: JjBackend,
    cfg: Config,
    *,
    state_dir: Path | None = None,
    sessions_dir: Path | None = None,
) -> list[Workspace]:
    """Every workspace except the TUI's own, with whether it can be removed safely.

    Without the launch lock: removal checks each row again under it, and a scan
    of large workspaces must not stall launches and refreshes in every TUI.
    """
    jobs = JobManager(backend.root.resolve(), cfg, state_dir)  # reads the records
    return _scan(backend, cfg, jobs, sessions_dir)


def remove_workspaces(
    backend: JjBackend,
    cfg: Config,
    chosen: Iterable[Workspace],
    *,
    dry_run: bool = False,
    state_dir: Path | None = None,
    sessions_dir: Path | None = None,
) -> list[str]:
    """Remove the ``chosen`` rows, re-checking each one first; returns a report."""
    repo = backend.root.resolve()

    def key(path: Path | None, name: str) -> tuple[Path | None, str]:
        return (path.absolute() if path else None, name)

    wanted = {key(ws.path, ws.name): ws for ws in chosen}
    jobs = JobManager(repo, cfg, state_dir)
    results: list[str] = []

    def remove(ws: Workspace, selected: Workspace) -> None:
        if not ws.removable:
            results.append(f"skipped {ws.describe}")
            return
        if (ws.removes_ignored and not selected.removes_ignored) or (
            selected.missing and not ws.missing
        ):
            results.append(f"skipped {ws.describe}: contents changed, review the workspace again")
            return
        if ws.path is not None:
            try:
                if not dry_run:
                    owned = ws.path.exists()
                    if owned:
                        _verify_workspace(repo, ws.path, ws.name)
                    _run(["jj", "workspace", "forget", "--", ws.name], repo)
                    if owned:
                        shutil.rmtree(ws.path)
                verb = "would remove" if dry_run else "removed"
                results.append(f"{verb} {ws.name}: {ws.path}")
            except (VcsError, OSError) as exc:
                results.append(f"skipped {ws.name}: {ws.path} ({exc})")
                return
        if ws.branch and ws.branch == selected.branch:
            try:
                # Snapshotting or work outside the TUI can move a bookmark since
                # the survey, especially during a batch removal.
                still_merged = survey.main and _jj(
                    repo,
                    "bookmark",
                    "list",
                    f"exact:{revset_symbol(ws.branch)}",
                    "-r",
                    f"::{revset_symbol(survey.main)}",
                    "-T",
                    'if(remote || conflict, "", "1")',
                )
                if not still_merged:
                    results.append(f"kept bookmark {ws.branch} (no longer merged)")
                    return
                if not dry_run:
                    _jj(repo, "bookmark", "delete", f"exact:{revset_symbol(ws.branch)}")
                verb = "would delete" if dry_run else "deleted"
                results.append(f"{verb} bookmark {ws.branch}")
            except VcsError as exc:
                results.append(f"kept bookmark {ws.branch} ({exc})")

    # Share the launch lock so another TUI cannot start a job during removal.
    tasks = TaskManager(jobs.state_dir)
    with jobs._locked(), tasks._locked():
        jobs.load()
        tasks._refresh_locked()
        survey = _survey(backend, cfg, jobs, sessions_dir, tasks)
        # Each workspace is inspected right before it is removed, not all of
        # them first: editors and shells take no lock, so files may change
        # while the others are being checked.
        for found in sorted(survey.found, key=lambda ws: ws.name):
            if selected := wanted.pop(key(found.path, found.name), None):
                remove(survey.inspect(found), selected)
        for ws in survey.bookmark_rows():
            if selected := wanted.pop(key(None, ws.name), None):
                remove(ws, selected)
    results += [
        f"skipped {path or 'bookmark ' + name}: no longer found"
        for path, name in sorted(wanted, key=str)
    ]
    return results


def force_removal_details(ws: Workspace) -> str:
    """Describe the registration and every on-disk entry force removal deletes."""
    lines = [
        f"{ws.name}: {ws.path}",
        f"kept because: {ws.status}",
        "",
        f"Unregister workspace: {ws.name}",
        f"Delete directory and all contents: {ws.path}",
    ]
    if ws.path is not None and ws.path.exists():

        def fail(exc: OSError) -> None:
            raise exc

        for directory, dirs, files in os.walk(ws.path, onerror=fail, followlinks=False):
            dirs.sort()
            for name in sorted(dirs + files):
                entry = Path(directory) / name
                label = str(entry.relative_to(ws.path))
                if entry.is_symlink():
                    label += f" -> {os.readlink(entry)} (symlink)"
                elif name in dirs:
                    label += "/"
                lines.append(f"  {label}")
    else:
        lines.append("  (directory not found)")
    lines.extend(["", "Bookmarks are kept."])
    return "\n".join(lines)


def force_remove_workspace(
    backend: JjBackend,
    cfg: Config,
    chosen: Workspace,
    *,
    expected_details: str | None = None,
    state_dir: Path | None = None,
    sessions_dir: Path | None = None,
) -> str:
    """Delete a kept workspace's directory with everything in it and unregister it.

    Bookmarks stay. Re-checks the workspace first: running sessions and the
    repository itself are never deleted.
    """
    repo = backend.root.resolve()
    if chosen.path is None:
        return f"skipped bookmark {chosen.name}: not a workspace"
    jobs = JobManager(repo, cfg, state_dir)
    tasks = TaskManager(jobs.state_dir)
    with jobs._locked(), tasks._locked():
        jobs.load()
        tasks._refresh_locked()
        survey = _survey(backend, cfg, jobs, sessions_dir, tasks)
        found = next(
            (
                ws
                for ws in survey.found
                if ws.path.absolute() == chosen.path.absolute() and ws.name == chosen.name
            ),
            None,
        )
        ws = survey.inspect(found) if found is not None else None
        if ws is None or ws.path is None:
            return f"skipped {chosen.name}: {chosen.path}: no longer a workspace"
        if not ws.forceable:
            return f"skipped {ws.describe}"
        try:
            owned = ws.path.exists()
            if owned:
                _verify_workspace(repo, ws.path, ws.name)
            if expected_details is not None and force_removal_details(ws) != expected_details:
                return f"skipped {ws.describe}: deletion list changed, review the workspace again"
            _run(["jj", "workspace", "forget", "--", ws.name], repo)
            if owned:
                shutil.rmtree(ws.path)
        except (VcsError, OSError) as exc:
            return f"skipped {ws.name}: {ws.path} ({exc})"
        return f"force-removed {ws.name}: {ws.path}"


def cleanup_workspaces(
    backend: JjBackend,
    cfg: Config,
    *,
    dry_run: bool = False,
    state_dir: Path | None = None,
    sessions_dir: Path | None = None,
) -> list[str]:
    """Remove every removable row; report on every row that was found."""
    found = find_workspaces(backend, cfg, state_dir=state_dir, sessions_dir=sessions_dir)
    removable = [ws for ws in found if ws.removable]
    results = remove_workspaces(
        backend, cfg, removable, dry_run=dry_run, state_dir=state_dir, sessions_dir=sessions_dir
    )
    return results + [f"kept {ws.describe}" for ws in found if not ws.removable]
