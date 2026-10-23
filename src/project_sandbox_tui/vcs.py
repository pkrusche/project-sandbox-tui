"""Bookmark discovery and creation for jj repositories."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path


class VcsError(RuntimeError):
    """A VCS command failed or the directory is not a repository."""


@dataclass(frozen=True)
class Branch:
    name: str
    head: str  # short change id
    subject: str  # first line of the tip's description
    timestamp: int  # committer time (unix seconds), 0 if unknown
    is_current: bool = False  # on the working-copy commit or its parent
    # The pseudo-branch for the working copy (``@``), always listed first;
    # sessions on it run in the repository's own workspace.
    working_copy: bool = False

    @property
    def label(self) -> str:
        return "@ working copy" if self.working_copy else self.name


def _run(args: list[str], cwd: Path) -> str:
    return _run_with_stderr(args, cwd)[0]


def _run_with_stderr(args: list[str], cwd: Path) -> tuple[str, str]:
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
    return proc.stdout, proc.stderr


_INVALID_NAME = re.compile(r"[\s\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|^[-/.]|[/.]$|\.lock(?:/|$)|/\.|//")


def validate_branch_name(name: str) -> str | None:
    """Return an error message for an unusable bookmark name, else None.

    Applies git's ref rules (bookmarks are exported to git) plus what is
    sensible for a bookmark that project-sandbox turns into a workspace directory.
    """
    if not name:
        return "bookmark name is empty"
    if _INVALID_NAME.search(name):
        return f"invalid bookmark name: {name!r}"
    return None


def workspace_path(repo: Path, branch: str, worktree_dir: Path | None) -> Path:
    """Mirror project-sandbox's workspace location for ``branch``.

    Must match ``project_sandbox.jj_workspace.path_for``: sibling
    ``<repo>-workspaces``, slashes replaced by ``-`` plus a 6-hex sha256 suffix
    when the name contained one.
    """
    repo = repo.resolve()
    root = worktree_dir or (repo.parent / f"{repo.name}-workspaces")
    safe = branch.replace("/", "-")
    if "/" in branch:
        safe = f"{safe}-{hashlib.sha256(branch.encode()).hexdigest()[:6]}"
    return root / safe


_JJ_TEMPLATE = (
    # stringify: ``name`` prints names that need revset quoting quoted (``"fix-"``)
    'if(remote, "", stringify(name) ++ "\\x00" ++ '
    "if(normal_target, "
    'normal_target.change_id().shortest(8) ++ "\\x00" ++ '
    'normal_target.description().first_line() ++ "\\x00" ++ '
    'normal_target.committer().timestamp().format("%s") ++ "\\x00" ++ '
    # "current" in jj: the bookmark sits on the working-copy commit or its parent
    'if(normal_target.contained_in("@ | @-"), "1", "0"), '
    '"\\x00(conflicted)\\x000\\x000") ++ "\\n")'
)


WORKING_COPY = "@"


def revset_symbol(name: str) -> str:
    """Quote a bookmark name for a jj revset: unquoted, ``fix-`` means the
    parents of ``fix`` and ``a+b`` does not parse. The working copy stays ``@``."""
    if name == WORKING_COPY:
        return name
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


_JJ_WORKING_COPY_TEMPLATE = (
    'change_id.shortest(8) ++ "\\x00" ++ description.first_line() ++ "\\x00" ++ '
    'committer.timestamp().format("%s") ++ "\\n"'
)


class JjBackend:
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
                    head=head,
                    subject=subject,
                    timestamp=int(ts) if ts.isdigit() else 0,
                    is_current=current == "1",
                )
            )
        return [self._working_copy(), *branches]

    def _working_copy(self) -> Branch:
        """The working copy as a row: work directly on ``@`` without a bookmark."""
        out = self._jj("log", "--no-graph", "-r", "@", "-T", _JJ_WORKING_COPY_TEMPLATE)
        head, subject, ts = ([*out.rstrip("\n").split("\0"), "", ""])[:3]
        return Branch(
            name=WORKING_COPY,
            head=head,
            subject=subject or "(no description set)",
            timestamp=int(ts) if ts.isdigit() else 0,
            is_current=True,
            working_copy=True,
        )

    def create_branch(self, name: str, start: str) -> None:
        # Not --ignore-working-copy: creating a bookmark is a write and should
        # see the latest snapshot of the user's working copy.
        _run(
            [
                "jj",
                "--no-pager",
                "--color=never",
                "bookmark",
                "create",
                revset_symbol(name),  # names like ``fix-`` only parse quoted
                "-r",
                start or "@-",
            ],
            self.root,
        )

    def diff_command(self, base: str, target: str) -> list[str]:
        """Interactive diff: let jj choose the formatter, colors and pager."""
        return ["jj", "diff", "--from", revset_symbol(base), "--to", revset_symbol(target)]

    def _bookmark_commit(self, name: str) -> str:
        if name == WORKING_COPY:
            raise VcsError("the working copy is not a local bookmark")
        commit = self._jj(
            "bookmark",
            "list",
            f"exact:{name}",
            "-T",
            'if(remote, "", if(normal_target, normal_target.commit_id(), "conflicted"))',
        ).strip()
        if not commit:
            raise VcsError(f"bookmark {name} does not exist")
        if commit == "conflicted":
            raise VcsError(f"bookmark {name} has no unambiguous target; resolve it first")
        return commit

    def fast_forward(self, main: str, target: str) -> bool:
        """Advance an existing local bookmark without rewriting commits or files."""
        if target == WORKING_COPY:
            # Snapshot before inspecting @, then snapshot again during the move.
            # The pinned destination rejects a working copy that changed between them.
            _run(["jj", "--no-pager", "--color=never", "status"], self.root)
        old = self._bookmark_commit(main)
        new = (
            self._jj("log", "--no-graph", "-r", "@", "-T", "commit_id").strip()
            if target == WORKING_COPY
            else self._bookmark_commit(target)
        )
        if old == new:
            return False
        if self._jj("log", "--no-graph", "-r", f"{new} & conflicts()", "-T", "commit_id"):
            raise VcsError(f"{target} has unresolved file conflicts")
        if not self._jj("log", "--no-graph", "-r", f"{old} & ::{new}", "-T", "commit_id"):
            raise VcsError(f"cannot fast-forward {main} to {target}: {main} is not an ancestor")
        # Pin the inspected target, and let jj enforce forward-only movement
        # again against main's current position in the mutation transaction.
        destination = (
            f"@ & {new}"
            if target == WORKING_COPY
            else f"bookmarks(exact:{json.dumps(target)}) & {new}"
        )
        if target == WORKING_COPY:
            _run(
                [
                    "jj",
                    "--no-pager",
                    "--color=never",
                    "bookmark",
                    "move",
                    f"exact:{main}",
                    "--to",
                    destination,
                ],
                self.root,
            )
        else:
            self._jj("bookmark", "move", f"exact:{main}", "--to", destination)
        return True

    def move_working_copy_to(self, target: str) -> str | None:
        """Save an unbookmarked @, then start a new working-copy change on target.

        Return the name of a bookmark created for the old working copy, if any.
        """
        # Snapshot first: edits can rewrite the target bookmark or its ancestors.
        _run(["jj", "--no-pager", "--color=never", "status"], self.root)
        commit = self._bookmark_commit(target)
        destination = f"bookmarks(exact:{json.dumps(target)}) & {commit}"
        old_commit, change = (
            self._jj("log", "--no-graph", "-r", "@", "-T", 'commit_id ++ "\\x00" ++ change_id')
            .strip()
            .split("\0")
        )
        bookmarks = self._jj(
            "bookmark",
            "list",
            "-T",
            'if(remote, "", name ++ "\\x00" ++ '
            'if(normal_target, normal_target.commit_id(), "") ++ "\\n")',
        )
        local = dict(line.split("\0", 1) for line in bookmarks.splitlines())
        saved: str | None = None
        if old_commit not in local.values():
            base = f"saved/{change[:8]}"
            saved = base
            suffix = 2
            while saved in local:
                saved = f"{base}-{suffix}"
                suffix += 1
            _run(
                [
                    "jj",
                    "--no-pager",
                    "--color=never",
                    "bookmark",
                    "create",
                    saved,
                    "-r",
                    f"@ & {old_commit}",
                ],
                self.root,
            )
        _run(["jj", "--no-pager", "--color=never", "new", destination], self.root)
        return saved

    def default_start(self) -> str:
        names = {b.name for b in self.list_branches() if not b.working_copy}
        # With no bookmarks, sessions have been working in @: bookmark that.
        return main_branch(names) or ("@-" if names else "@")


def main_branch(names: Collection[str], configured: str = "") -> str | None:
    """``configured`` if it is one of ``names``, else the first of main / master / trunk."""
    if configured:
        return configured if configured in names else None
    return next((c for c in ("main", "master", "trunk") if c in names), None)


def detect(path: Path) -> JjBackend:
    """Find the jj repository containing ``path``."""
    path = path.resolve()
    for candidate in (path, *path.parents):
        if (candidate / ".jj").is_dir():
            return JjBackend(candidate)
        if (candidate / ".git").exists():
            raise VcsError(
                f"{candidate} is a git repository without jj; "
                "run `jj git init --colocate` there first"
            )
    raise VcsError(f"{path} is not inside a jj repository")


def sort_branches(branches: list[Branch]) -> list[Branch]:
    """Working copy first, then the current bookmarks, then most recently committed."""
    return sorted(
        branches, key=lambda b: (not b.working_copy, not b.is_current, -b.timestamp, b.name)
    )
