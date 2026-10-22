"""Branch / bookmark discovery and creation for git and jj repositories."""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

Kind = Literal["git", "jj"]


class VcsError(RuntimeError):
    """A VCS command failed or the directory is not a repository."""


@dataclass(frozen=True)
class Branch:
    name: str
    kind: Kind
    head: str  # short commit hash / change id
    subject: str  # first line of the tip's description
    timestamp: int  # committer time (unix seconds), 0 if unknown
    is_current: bool = False


def _run(args: list[str], cwd: Path) -> str:
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise VcsError(f"{args[0]} not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise VcsError(f"{' '.join(args[:3])} timed out") from exc
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip().splitlines()
        raise VcsError(msg[-1] if msg else f"{' '.join(args[:3])} failed")
    return proc.stdout


_INVALID_NAME = re.compile(r"[\s~^:?*\[\\]|\.\.|@\{|^[-/.]|[/.]$|\.lock$|//")


def validate_branch_name(name: str) -> str | None:
    """Return an error message for an unusable branch name, else None.

    Applies the intersection of git's ref rules and what is sensible for a jj
    bookmark that project-sandbox will turn into a worktree directory.
    """
    if not name:
        return "branch name is empty"
    if _INVALID_NAME.search(name):
        return f"invalid branch name: {name!r}"
    return None


def workspace_path(repo: Path, kind: Kind, branch: str, worktree_dir: Path | None) -> Path:
    """Mirror project-sandbox's worktree/workspace location for ``branch``.

    Must match ``project_sandbox.worktree.path_for`` / ``jj_workspace.path_for``:
    sibling ``<repo>-worktrees`` (git) or ``<repo>-workspaces`` (jj), slashes
    replaced by ``-`` plus a 6-hex sha256 suffix when the name contained one.
    """
    repo = repo.resolve()
    suffix_dir = "worktrees" if kind == "git" else "workspaces"
    root = worktree_dir or (repo.parent / f"{repo.name}-{suffix_dir}")
    safe = branch.replace("/", "-")
    if "/" in branch:
        safe = f"{safe}-{hashlib.sha256(branch.encode()).hexdigest()[:6]}"
    return root / safe


class Backend(Protocol):
    kind: Kind
    root: Path

    def list_branches(self) -> list[Branch]: ...

    def create_branch(self, name: str, start: str) -> None: ...

    def default_start(self) -> str: ...


class GitBackend:
    kind: Kind = "git"

    def __init__(self, root: Path) -> None:
        self.root = root

    def list_branches(self) -> list[Branch]:
        fmt = "%00".join(
            (
                "%(refname:short)",
                "%(objectname:short)",
                "%(contents:subject)",
                "%(committerdate:unix)",
                "%(HEAD)",
            )
        )
        out = _run(
            ["git", "for-each-ref", f"--format={fmt}", "refs/heads"],
            self.root,
        )
        branches: list[Branch] = []
        for line in out.splitlines():
            parts = line.split("\0")
            if len(parts) != 5:
                continue
            name, head, subject, ts, current = parts
            branches.append(
                Branch(
                    name=name,
                    kind="git",
                    head=head,
                    subject=subject,
                    timestamp=int(ts) if ts.isdigit() else 0,
                    is_current=current.strip() == "*",
                )
            )
        return branches

    def create_branch(self, name: str, start: str) -> None:
        _run(["git", "branch", "--no-track", "--", name, start or "HEAD"], self.root)

    def default_start(self) -> str:
        names = {b.name for b in self.list_branches()}
        for candidate in ("main", "master", "trunk"):
            if candidate in names:
                return candidate
        return "HEAD"


_JJ_TEMPLATE = (
    'if(remote, "", name ++ "\\x00" ++ '
    "if(normal_target, "
    'normal_target.change_id().shortest(8) ++ "\\x00" ++ '
    'normal_target.description().first_line() ++ "\\x00" ++ '
    'normal_target.committer().timestamp().format("%s") ++ "\\x00" ++ '
    'if(normal_target.current_working_copy(), "1", "0"), '
    '"\\x00(conflicted)\\x000\\x000") ++ "\\n")'
)


class JjBackend:
    kind: Kind = "jj"

    def __init__(self, root: Path) -> None:
        self.root = root

    def _jj(self, *args: str) -> str:
        return _run(
            ["jj", "--no-pager", "--color=never", "--ignore-working-copy", *args],
            self.root,
        )

    def list_branches(self) -> list[Branch]:
        out = self._jj("bookmark", "list", "-T", _JJ_TEMPLATE)
        branches: list[Branch] = []
        for line in out.splitlines():
            parts = line.split("\0")
            if len(parts) != 5:
                continue
            name, head, subject, ts, current = parts
            branches.append(
                Branch(
                    name=name,
                    kind="jj",
                    head=head,
                    subject=subject,
                    timestamp=int(ts) if ts.isdigit() else 0,
                    is_current=current == "1",
                )
            )
        return branches

    def create_branch(self, name: str, start: str) -> None:
        # Not --ignore-working-copy: creating a bookmark is a write and should
        # see the latest snapshot of the user's working copy.
        _run(
            ["jj", "--no-pager", "--color=never", "bookmark", "create", name, "-r", start or "@-"],
            self.root,
        )

    def default_start(self) -> str:
        names = {b.name for b in self.list_branches()}
        for candidate in ("main", "master", "trunk"):
            if candidate in names:
                return candidate
        return "@-"


def detect(path: Path) -> Backend:
    """Find the repository containing ``path``; jj wins for colocated repos."""
    path = path.resolve()
    for candidate in (path, *path.parents):
        if (candidate / ".jj").is_dir():
            return JjBackend(candidate)
        if (candidate / ".git").exists():
            return GitBackend(candidate)
    raise VcsError(f"{path} is not inside a git or jj repository")


def sort_branches(branches: list[Branch]) -> list[Branch]:
    """Current branch first, then most recently committed."""
    return sorted(branches, key=lambda b: (not b.is_current, -b.timestamp, b.name))
