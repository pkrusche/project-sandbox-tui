"""Bookmark discovery and creation for jj repositories."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import subprocess
import tomllib
from collections.abc import Collection
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


class VcsError(RuntimeError):
    """A VCS command failed or the directory is not a repository."""


class StaleWorkingCopyError(VcsError):
    """jj refuses to snapshot: another workspace rewrote this one's working-copy commit."""


class HeldByWorkspaceError(VcsError):
    """The target commit is another workspace's working copy.

    Moving onto it would leave this working copy stale as soon as that
    workspace snapshots an edit, which rewrites the commit underneath it.
    """

    def __init__(self, target: str, workspaces: list[tuple[str, Path | None]]) -> None:
        names = ", ".join(name for name, _ in workspaces)
        super().__init__(f"{target} is the working copy of workspace {names}")
        self.target = target
        self.workspaces = workspaces


@dataclass(frozen=True)
class MainComparison:
    ahead: int
    behind: int
    changed_files: int


@dataclass(frozen=True)
class Branch:
    name: str
    head: str  # short change id
    subject: str  # first line of the tip's description
    timestamp: int  # committer time (unix seconds), 0 if unknown
    is_current: bool = False  # on the working-copy commit or its parent
    # The pseudo-branch for the working copy (``@``), always listed first;
    # external sessions on it use the repository's own workspace.
    working_copy: bool = False
    comparison: MainComparison | None = None
    commit: str = ""  # full commit id; empty for a conflicted bookmark

    @property
    def label(self) -> str:
        return "@ working copy" if self.working_copy else self.name


@dataclass(frozen=True)
class FileChange:
    """One file in a diff between two revisions."""

    path: str  # the target side for copies and renames
    source_path: str
    display: str  # ``{old => new}`` for copies and renames
    status: str  # M, A, D, R or C
    key: str  # the same for the same change to this file: both sides' content, type, path
    added: int | None = None  # lines; None for binary files
    removed: int | None = None
    size: int | None = None  # bytes in the target revision; None if removed or unknown

    @property
    def paths(self) -> tuple[str, ...]:
        """Both sides of a copy or rename, so jj still pairs them in a restricted diff."""
        return (self.path,) if self.source_path == self.path else (self.source_path, self.path)


@dataclass(frozen=True)
class FileChanges:
    """Changed files between two revisions, pinned to the commits they were read from."""

    base: str  # commit id; empty for a commit diff against its automatically merged parents
    target: str  # commit id
    files: list[FileChange]


@dataclass(frozen=True)
class ReviewCommit:
    """A pinned commit on a bookmark's path from main."""

    commit: str
    subject: str

    @property
    def key(self) -> str:
        return f"commit:{self.commit}"


def _run(args: list[str], cwd: Path) -> str:
    return _run_with_stderr(args, cwd)[0]


def _run_with_stderr(args: list[str], cwd: Path) -> tuple[str, str]:
    proc = _completed(args, cwd, text=True)
    return proc.stdout, proc.stderr


def _run_bytes(args: list[str], cwd: Path) -> bytes:
    """``_run`` for file contents and paths, which need not be valid text."""
    return _completed(args, cwd, text=False).stdout


def _completed(args: list[str], cwd: Path, *, text: bool) -> subprocess.CompletedProcess[Any]:
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=text,
            check=False,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise VcsError(f"{args[0]} not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise VcsError(f"{' '.join(args[:3])} timed out") from exc
    if proc.returncode != 0:
        output = proc.stderr or proc.stdout
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        if "working copy is stale" in output:
            raise StaleWorkingCopyError("the jj working copy is stale")
        msg = output.strip().splitlines()
        raise VcsError(msg[-1] if msg else f"{' '.join(args[:3])} failed")
    return proc


_INVALID_NAME = re.compile(r"[\s\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|^[-/.]|[/.]$|\.lock(?:/|$)|/\.|//")


def validate_branch_name(name: str) -> str | None:
    """Return an error message for an unusable bookmark name, else None.

    Applies git's ref rules (bookmarks are exported to git) plus what is
    sensible for a bookmark that project-sandbox turns into a workspace directory.
    """
    if not name:
        return "bookmark name is empty"
    if name == WORKING_COPY or _INVALID_NAME.search(name):
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
    'if(normal_target.contained_in("@ | @-"), "1", "0") ++ "\\x00" ++ '
    "normal_target.commit_id(), "
    '"\\x00(conflicted)\\x000\\x000\\x00") ++ "\\n")'
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
    'committer.timestamp().format("%s") ++ "\\x00" ++ commit_id ++ "\\n"'
)


# Every field ends with NUL: paths may contain newlines.
_DIFF_FIELDS = (
    "status_char",
    "source.path()",
    "path",
    "display_diff_path",
    "source.normal_value()",
    "target.normal_value()",
    "source.file_type()",
    "target.file_type()",
    "source.executable()",
    "target.executable()",
    "(source.conflict() || target.conflict())",
)
_JJ_DIFF_TEMPLATE = " ++ ".join(f'{field} ++ "\\x00"' for field in _DIFF_FIELDS)
# The same identity fields as _changed_files, without patches, line counts or sizes.
# Frame each commit with its file count; filenames may contain newlines.
_JJ_REVIEW_KEYS_TEMPLATE = (
    'commit_id ++ "\\x00" ++ diff.files().len() ++ "\\x00" ++ '
    "diff.files().map(|file| "
    'file.source().path() ++ "\\x00" ++ file.path() ++ "\\x00" ++ '
    'file.source().normal_value() ++ "\\x00" ++ file.target().normal_value() ++ "\\x00" ++ '
    'file.source().file_type() ++ "\\x00" ++ file.target().file_type() ++ "\\x00" ++ '
    'file.source().executable() ++ "\\x00" ++ file.target().executable() ++ "\\x00" ++ '
    '(file.source().conflict() || file.target().conflict()) ++ "\\x00").join("")'
)
# A context large enough for whole files: line counts and sizes from one jj call.
_FULL_CONTEXT = str(1 << 30)
SIZE_LOOKUPS = 50  # files whose size needs a jj call of its own: binaries, renames, modes


def _decode_path(raw: bytes) -> str:
    return raw.decode("utf-8", errors="surrogateescape")


def _file_pattern(path: str) -> str:
    """A fileset matching exactly one repository path."""
    return f"root-file:{json.dumps(path, ensure_ascii=False)}"


def _git_file_diffs(out: bytes, headers: list[bytes]) -> dict[bytes, list[bytes]]:
    """Split a git diff using complete headers from the NUL-delimited file list.

    jj emits filenames literally, including newlines. Both outputs follow the
    same file order, so match whole headers before splitting the body into lines.
    """
    files: dict[bytes, list[bytes]] = {}
    previous: bytes | None = None
    offset = 0
    for header in headers:
        if offset == 0 and out.startswith(header + b"\n"):
            start = 0
        else:
            # Added/context lines may themselves contain a complete diff header.
            boundary = out.find(b"\n" + header + b"\n", max(0, offset - 1))
            if boundary < 0:
                continue
            start = boundary + 1
        if previous is not None:
            files[previous] = out[offset:start].split(b"\n")
        previous = header
        offset = start + len(header) + 1
    if previous is not None:
        files[previous] = out[offset:].split(b"\n")
    return files


def _git_stats(lines: list[bytes]) -> tuple[int | None, int | None, int | None]:
    """(added, removed, target size) from one file's whole-file git diff; None if unknown.

    With whole-file context, the target is the context and added lines.
    """
    added = removed = size = 0
    in_hunk = kept = False
    for line in lines:
        if not in_hunk:
            if line.startswith(b"Binary files "):
                return None, None, None
            in_hunk = line.startswith(b"@@")
            continue
        tag = line[:1]
        if tag in (b"+", b" "):
            added += tag == b"+"
            size += len(line)  # the tag stands for the newline
            kept = True
        elif tag == b"-":
            removed += 1
            kept = False
        elif tag == b"\\" and kept:  # "\ No newline at end of file"
            size -= 1
    # No hunk: only the name or mode changed, or the file is empty.
    return added, removed, size if in_hunk else None


class JjBackend:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._comparisons: dict[tuple[str, str], MainComparison] = {}

    def _jj(self, *args: str) -> str:
        return _run(
            ["jj", "--no-pager", "--color=never", "--ignore-working-copy", *args],
            self.root,
        )

    def list_branches(self, *, compare_to_main: str | None = None) -> list[Branch]:
        """List local bookmarks, optionally comparing to configured/auto-detected main."""
        # Snapshot before reading bookmarks: edits to @ may also move a bookmark.
        working_copy, working_commit = self._working_copy(snapshot=compare_to_main is not None)
        out = self._jj("bookmark", "list", "-T", _JJ_TEMPLATE)
        branches: list[Branch] = [working_copy]
        targets: dict[str, str] = {}
        for line in out.splitlines():
            parts = line.split("\0")
            if len(parts) != 6:
                continue
            name, head, subject, ts, current, commit = parts
            targets[name] = commit
            branches.append(
                Branch(
                    name=name,
                    head=head,
                    subject=subject,
                    timestamp=int(ts) if ts.isdigit() else 0,
                    is_current=current == "1",
                    commit=commit,
                )
            )
        if compare_to_main is not None:
            main = main_branch(targets, compare_to_main)
            base = targets.get(main, "") if main else ""
            targets[WORKING_COPY] = working_commit
            comparisons: dict[tuple[str, str], MainComparison] = {}
            for index, branch in enumerate(branches):
                target = targets[branch.name]
                if not base or not target:  # missing main or a conflicted bookmark
                    continue
                key = (base, target)
                comparison = comparisons.get(key) or self._comparisons.get(key)
                if comparison is None:
                    try:
                        comparison = self._compare_commits(base, target)
                    except VcsError:
                        # Keep the list usable; retry unavailable comparisons next refresh.
                        continue
                comparisons[key] = comparison
                branches[index] = replace(branch, comparison=comparison)
            # Full commit IDs invalidate rewritten tips; discard obsolete pairs.
            self._comparisons = comparisons
        return branches

    def _compare_commits(self, base: str, target: str) -> MainComparison:
        if base == target:
            return MainComparison(0, 0, 0)
        ahead = int(self._jj("log", "--count", "-r", f"{base}..{target}"))
        behind = int(self._jj("log", "--count", "-r", f"{target}..{base}"))
        # One byte per file, independent of filenames and the user's diff formatter.
        files = self._jj("diff", "--from", base, "--to", target, "-T", '"1"')
        return MainComparison(ahead, behind, len(files))

    def _working_copy(self, *, snapshot: bool = False) -> tuple[Branch, str]:
        """The working copy as a row: work directly on ``@`` without a bookmark."""
        args = ("log", "--no-graph", "-r", "@", "-T", _JJ_WORKING_COPY_TEMPLATE)
        out = (
            _run(["jj", "--no-pager", "--color=never", *args], self.root)
            if snapshot
            else self._jj(*args)
        )
        head, subject, ts, commit = out.rstrip("\n").split("\0")
        return (
            Branch(
                name=WORKING_COPY,
                head=head,
                subject=subject or "(no description set)",
                timestamp=int(ts) if ts.isdigit() else 0,
                is_current=True,
                working_copy=True,
                commit=commit,
            ),
            commit,
        )

    def create_branch(self, name: str, start: str) -> None:
        # A name typed into a launch form is a literal bookmark if it exists;
        # otherwise it may be a revset such as @- or trunk(). In particular,
        # ``fix-`` must not silently mean the parents of ``fix``.
        start = start or "@-"
        if self._jj("bookmark", "list", f"exact:{json.dumps(start)}", "-T", 'if(remote, "", "1")'):
            start = revset_symbol(start)
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
                f"--revision={start}",
            ],
            self.root,
        )

    def add_workspace(self, name: str, path: Path) -> None:
        """Create workspace ``name`` at ``path`` on a new change on top of bookmark ``name``.

        Named after the bookmark, like project-sandbox's own workspaces, so its
        sessions reuse it and cleanup finds it.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        _run(
            [
                "jj",
                "--no-pager",
                "--color=never",
                "workspace",
                "add",
                f"--name={name}",
                f"--revision={revset_symbol(name)}",
                "--",
                str(path),
            ],
            self.root,
        )

    def delete_bookmark(self, name: str) -> str:
        """Delete a local bookmark; return the commit it pointed to, for restoring it."""
        # Snapshot first: edits to @ may move the bookmark, and the restored
        # bookmark must not point to the pre-snapshot commit.
        commit = self._bookmark_commit(name, snapshot=True)
        self._jj("bookmark", "delete", f"exact:{revset_symbol(name)}")
        return commit

    def _description_commit(self, name: str) -> str:
        if name == WORKING_COPY:
            return self._working_copy(snapshot=True)[1]
        return self._bookmark_commit(name, snapshot=True)

    def get_description(self, name: str) -> tuple[str, str]:
        """Return the target commit and its full description, including newlines."""
        commit = self._description_commit(name)
        return commit, self._jj("log", "--no-graph", "-r", commit, "-T", "description")

    def set_description(self, name: str, description: str, expected: str) -> None:
        """Describe the target only if it still points to the commit the user edited."""
        if self._description_commit(name) != expected:
            raise VcsError(f"{name} has changed since the editor opened; reopen it to try again")
        target = (
            "@"
            if name == WORKING_COPY
            else f"bookmarks(exact:{json.dumps(name, ensure_ascii=False)})"
        )
        self._jj("describe", "-r", f"{target} & {expected}", f"--message={description}")

    def restore_bookmark(self, name: str, commit: str) -> None:
        """Recreate a deleted bookmark at ``commit``, even if it was abandoned since."""
        self._jj("bookmark", "create", revset_symbol(name), "-r", commit)

    def move_bookmark(self, name: str, expected: str, to: str) -> None:
        """Move bookmark ``name`` from ``expected`` to ``to``, backwards too (undoing ``f``).

        Refuses when the bookmark no longer points to ``expected``: it moved since.
        """
        if self._bookmark_commit(name) != expected:
            raise VcsError(f"{name} has moved since")
        self._jj("bookmark", "set", revset_symbol(name), "-r", to, "--allow-backwards")

    def bookmarked_ancestors(self, commit: str) -> list[list[str]]:
        """Bookmarks on the nearest bookmarked proper ancestors of ``commit``,
        one list per such commit."""
        out = self._jj(
            "log",
            "--no-graph",
            "-r",
            f"heads(::{commit}- & bookmarks())",
            "-T",
            'local_bookmarks.map(|b| stringify(b.name())).join("\\x00") ++ "\\n"',
        )
        return [line.split("\0") for line in out.splitlines() if line]

    def diff_command(self, base: str, target: str) -> list[str]:
        """Interactive diff: let jj choose the formatter, colors and pager."""
        return ["jj", "diff", "--from", revset_symbol(base), "--to", revset_symbol(target)]

    def paged_diff_command(self, argv: list[str]) -> list[str]:
        """Keep short diffs open, preserving the host's formatter and pager.

        jj's "auto" pagination always starts a pager on a tty; short-output
        auto-exit is a pager option. Override it only for this viewing.
        """
        config = tomllib.loads(
            self._jj(
                "config", "list", "--include-defaults", "ui.pager",
                "--template", 'name ++ " = " ++ value ++ "\\n"',
            )
        )  # fmt: skip
        pager = config["ui"]["pager"]
        command = pager.get("command") if isinstance(pager, dict) else pager
        args = shlex.split(command) if isinstance(command, str) else list(command)
        name = Path(args[0]).name if args else ""
        overrides = [
            "--config=ui.paginate=auto",
            "--config=ui.streampager.interface=full-screen-clear-output",
        ]
        if name == "less":
            # Command-line options override LESS, including a configured -F.
            args.extend(["-+F", "-+e", "-+X", "-K"])
        elif name == "delta":
            # Replace existing paging options rather than passing conflicting
            # copies to versions of delta that reject repeated arguments.
            original = iter(args)
            args = []
            for arg in original:
                if arg == "--paging":
                    next(original, None)
                elif not arg.startswith("--paging="):
                    args.append(arg)
            args.append("--paging=always")
            # Delta normally supplies less -FRX itself. Keep formatting options,
            # but make its nested pager wait for q even for a one-line diff.
            env = pager.get("env", {}) if isinstance(pager, dict) else {}
            nested = (
                env.get("DELTA_PAGER")
                or os.environ.get("DELTA_PAGER")
                or env.get("PAGER")
                or os.environ.get("PAGER")
                or "less -R"
            )
            nested_args = shlex.split(nested)
            if nested_args and Path(nested_args[0]).name == "less":
                nested_args.extend(["-+F", "-+e", "-+X", "-K"])
            overrides.append(
                "--config=ui.pager.env.DELTA_PAGER=" + json.dumps(shlex.join(nested_args))
            )
        else:
            return [*argv, *overrides]
        key = "ui.pager.command" if isinstance(pager, dict) or name == "delta" else "ui.pager"
        # A scalar delta command must become a table to carry DELTA_PAGER too.
        overrides.append(f"--config={key}={json.dumps(args)}")
        return [*argv, *overrides]

    def diff_files_command(self, base: str, target: str, paths: Collection[str]) -> list[str]:
        """Interactive diff of some files, as ``diff_command``."""
        return [*self.diff_command(base, target), " | ".join(map(_file_pattern, paths)) or "none()"]

    def review_commits(
        self, base: str, target: str, *, snapshot: bool = True
    ) -> list[ReviewCommit]:
        """Commits reachable from target but not base, parents before children.

        Pin both tips after snapshotting once, including edits to the working copy.
        Refresh counts can skip the snapshot when they already have pinned tips.
        """
        if snapshot:
            _run(["jj", "--no-pager", "--color=never", "status"], self.root)
        base_commit, target_commit = (
            self._jj("log", "--no-graph", "-r", revset_symbol(name), "-T", "commit_id").strip()
            for name in (base, target)
        )
        out = self._jj(
            "log",
            "--no-graph",
            "--reversed",
            "-r",
            f"{base_commit}..{target_commit}",
            "-T",
            # Descriptions can themselves contain NULs; encode them before framing.
            'commit_id ++ "\\x00" ++ json(description.first_line()) ++ "\\x00"',
        )
        fields = out.split("\0")[:-1]
        return [
            ReviewCommit(fields[i], json.loads(fields[i + 1])) for i in range(0, len(fields), 2)
        ]

    def diff_commit_files_command(self, commit: ReviewCommit, paths: Collection[str]) -> list[str]:
        """Interactive file diffs relative to the commit's automatically merged parents."""
        return [
            "jj",
            "diff",
            "-r",
            commit.commit,
            " | ".join(map(_file_pattern, paths)) or "none()",
        ]

    def changed_files_for_commit(self, commit: ReviewCommit) -> FileChanges:
        """Files in a pinned commit, with independent seen keys for each commit and file."""
        return self._changed_files("", commit.commit, ("-r", commit.commit), f"{commit.key}:file:")

    def review_file_keys(self, base: str, target: str) -> set[str]:
        """Review identities between pinned commit IDs, in one metadata-only query.

        Unlike the review screen, bookmark counts need neither patches nor file sizes.
        Conflicted files have no normal object IDs, so those commits still use the
        full diff to preserve their existing content-based identities.
        """
        fields = self._jj_bytes(
            "log", "--no-graph", "-r", f"{base}..{target}", "-T", _JJ_REVIEW_KEYS_TEMPLATE
        ).split(b"\0")[:-1]
        keys: set[str] = set()
        offset = 0
        while offset < len(fields):
            commit = fields[offset].decode("ascii")
            count = int(fields[offset + 1])
            offset += 2
            end = offset + count * 9
            entries = [fields[start : start + 9] for start in range(offset, end, 9)]
            if any(entry[-1] == b"true" for entry in entries):
                keys.update(
                    f.key for f in self.changed_files_for_commit(ReviewCommit(commit, "")).files
                )
            else:
                keys.update(
                    f"commit:{commit}:file:"
                    + hashlib.sha256(b"\0".join(entry[:-1])).hexdigest()[:32]
                    for entry in entries
                )
            offset = end
        return keys

    def changed_files(self, base: str, target: str) -> FileChanges:
        """Files differing between ``base`` and ``target``, with line counts and sizes."""
        # Snapshot once and pin both sides: the list, its seen marks and the
        # diffs opened from it must all show the same change.
        _run(["jj", "--no-pager", "--color=never", "status"], self.root)
        base_commit, target_commit = (
            self._jj("log", "--no-graph", "-r", revset_symbol(name), "-T", "commit_id").strip()
            for name in (base, target)
        )
        return self._changed_files(
            base_commit, target_commit, ("--from", base_commit, "--to", target_commit)
        )

    def _changed_files(
        self, base_commit: str, target_commit: str, span: tuple[str, ...], key_prefix: str = ""
    ) -> FileChanges:
        fields = self._jj_bytes("diff", *span, "-T", _JJ_DIFF_TEMPLATE).split(b"\0")[:-1]
        width = len(_DIFF_FIELDS)
        headers = [
            b"diff --git a/" + fields[start + 1] + b" b/" + fields[start + 2]
            for start in range(0, len(fields), width)
        ]
        git = _git_file_diffs(
            self._jj_bytes("diff", *span, "--git", "--context", _FULL_CONTEXT), headers
        )
        files: list[FileChange] = []
        lookups = 0
        for start in range(0, len(fields), width):
            status, source, path, display, *ids, conflict = fields[start : start + width]
            lines = git.get(b"diff --git a/" + source + b" b/" + path, [])
            identity = b"\0".join([source, path, *ids])
            if conflict == b"true":  # no object ids: the conflict's own text identifies it
                identity += b"\0" + b"\n".join(lines)
            added, removed, size = _git_stats(lines)
            target_type = ids[3]
            if size is None and target_type in (b"file", b"symlink") and lookups < SIZE_LOOKUPS:
                lookups += 1
                with contextlib.suppress(VcsError):
                    pattern = _file_pattern(_decode_path(path))
                    size = len(self._jj_bytes("file", "show", "-r", target_commit, pattern))
            files.append(
                FileChange(
                    path=_decode_path(path),
                    source_path=_decode_path(source),
                    display=display.decode(errors="replace"),
                    status=status.decode(),
                    key=key_prefix + hashlib.sha256(identity).hexdigest()[:32],
                    added=added,
                    removed=removed,
                    size=None if status == b"D" else size,
                )
            )
        return FileChanges(base_commit, target_commit, files)

    def _jj_bytes(self, *args: str) -> bytes:
        return _run_bytes(
            ["jj", "--no-pager", "--color=never", "--ignore-working-copy", *args], self.root
        )

    def _bookmark_commit(self, name: str, *, snapshot: bool = False) -> str:
        if name == WORKING_COPY:
            raise VcsError("the working copy is not a local bookmark")
        args = (
            "bookmark",
            "list",
            f"exact:{revset_symbol(name)}",
            "-T",
            'if(remote, "", if(normal_target, normal_target.commit_id(), "conflicted"))',
        )
        commit = (
            _run(["jj", "--no-pager", "--color=never", *args], self.root)
            if snapshot
            else self._jj(*args)
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
                    f"exact:{revset_symbol(main)}",
                    "--to",
                    destination,
                ],
                self.root,
            )
        else:
            self._jj("bookmark", "move", f"exact:{revset_symbol(main)}", "--to", destination)
        return True

    def workspaces_at(self, commit: str) -> list[tuple[str, Path | None]]:
        """Other workspaces whose working-copy commit is ``commit``, with their roots."""
        current = 'if(target.current_working_copy(), "1", "0")'
        fields = f'stringify(name) ++ "\\x00" ++ target.commit_id() ++ "\\x00" ++ {current}'
        try:
            out = self._jj("workspace", "list", "-T", f'{fields} ++ "\\x00" ++ root ++ "\\n"')
        except VcsError:  # jj without the ``root`` template keyword
            out = self._jj("workspace", "list", "-T", f'{fields} ++ "\\x00\\n"')
        held: list[tuple[str, Path | None]] = []
        for line in out.splitlines():
            name, at, is_current, root = (line.split("\0") + ["", "", ""])[:4]
            if at == commit and is_current != "1":
                held.append((name, Path(root) if root else None))
        return held

    def move_working_copy_to(
        self, target: str, *, main: str = "", new_in_holders: bool = False
    ) -> str | None:
        """Save an unbookmarked, non-empty @ ahead of main, then start on target.

        Use the configured main bookmark, or detect main/master/trunk. If main
        is missing or conflicted, do not save the previous change.

        Raise :class:`HeldByWorkspaceError` if target's commit is another
        workspace's working copy, unless ``new_in_holders``: then first start a
        new empty change in each such workspace, so later edits there land on
        top of target instead of rewriting it. The caller must make sure no
        session is running in those workspaces.

        Return the name of a bookmark created for the old working copy, if any.
        """
        # Snapshot first: edits can rewrite the target bookmark or its ancestors.
        _run(["jj", "--no-pager", "--color=never", "status"], self.root)
        commit = self._bookmark_commit(target)
        if held := self.workspaces_at(commit):
            if not new_in_holders:
                raise HeldByWorkspaceError(target, held)
            for name, root in held:
                if root is None or not root.is_dir():
                    raise VcsError(f"workspace {name}: directory not found; remove it with W")
                # Snapshots that workspace first, which may move target with it.
                _run(["jj", "--no-pager", "--color=never", "new"], root)
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
            'if(remote, "", stringify(name) ++ "\\x00" ++ '
            'if(normal_target, normal_target.commit_id(), "") ++ "\\n")',
        )
        local = dict(line.split("\0", 1) for line in bookmarks.splitlines())
        main_commit = local.get(main_branch(local, main) or "")
        saved: str | None = None
        if (
            old_commit not in local.values()
            and main_commit
            and old_commit != main_commit
            and self._jj(
                "log", "--no-graph", "-r", f"{main_commit} & ::{old_commit}", "-T", "commit_id"
            )
            and self._jj(
                "log", "--no-graph", "-r", f"{old_commit} & ~empty()", "-T", "commit_id"
            )
        ):
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

    def update_stale(self) -> None:
        """Check out the rewritten working-copy commit: ``jj workspace update-stale``."""
        _run(["jj", "--no-pager", "--color=never", "workspace", "update-stale"], self.root)

    def default_start(self) -> str:
        names = {b.name for b in self.list_branches() if not b.working_copy}
        # With no bookmarks, use the working copy as the first bookmark's base.
        return main_branch(names) or ("@-" if names else "@")


def main_branch(names: Collection[str], configured: str = "") -> str | None:
    """``configured`` if it is one of ``names``, else the first of main / master / trunk."""
    if configured:
        return configured if configured in names else None
    return next((c for c in ("main", "master", "trunk") if c in names), None)


def session_target_error(name: str, names: Collection[str], configured: str = "") -> str:
    """Explain why a revision is protected from sandbox sessions, if it is."""
    # Include a new target: creating the first main/master/trunk bookmark must
    # not bypass the protection just because it does not exist yet.
    main = main_branch([*names, name], configured) or configured or "main"
    if name == main:
        return f"{name} is protected; create a bookmark for changes before starting a session"
    return ""


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
