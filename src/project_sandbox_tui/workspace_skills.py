"""Deploying skills into bookmark workspaces before agents or editors use them.

The bundled configuration skills (unless ``project.deploy_config_skill`` is
false) and each ``[[workspace_skills]]`` entry are copied into a bookmark's
workspace on first use; existing valid copies are kept. See
``docs/configuration.md#workspace-skills`` for the user-facing rules.

Copies must stay out of jj snapshots. Before copying, the destination's
effective ignore rules are evaluated with ``git check-ignore --no-index``
against disposable Git metadata, isolated from host-only excludes so the
answer also holds inside a container. The bundled skill may additionally be
excluded through the shared Git backend's ``info/exclude``.

A failed explicit (configured) deployment stops the launch; a problem with the
bundled skill is reported and the launch goes ahead.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path

from project_sandbox_tui.config import (
    CONFIG_SKILLS,
    Config,
)
from project_sandbox_tui.vcs import (
    WORKING_COPY,
    JjBackend,
    VcsError,
    registered_workspace_name,
    tracked_files,
    workspace_path,
)

SKILL_FILE = "SKILL.md"
STAGING_PREFIX = "sbx-tui-skill-"  # under the workspace's .jj directory


class PreparationError(ValueError):
    """The workspace could not be prepared, so the launch must not start."""


class _Problem(Exception):
    """One skill target cannot be deployed (reason for the user)."""


@dataclass(frozen=True)
class Skill:
    """A skill directory to deploy: the bundled one, or a ``[[workspace_skills]]`` entry."""

    source: Path | str  # host directory, or the resource name for a bundled skill
    targets: tuple[str, ...]  # relative to the workspace, e.g. ".agents/skills/x"

    @property
    def explicit(self) -> bool:
        return isinstance(self.source, Path)

    @property
    def label(self) -> str:
        return str(self.source)


@dataclass(frozen=True)
class Note:
    message: str
    warning: bool = False


def configured_skills(cfg: Config, repo: Path) -> list[Skill]:
    """The skills to deploy, bundled first; relative sources resolve from ``repo``."""
    skills = []
    if cfg.project.deploy_config_skill:
        skills.extend(Skill(name, targets) for name, targets in CONFIG_SKILLS.items())
    for entry in cfg.workspace_skills:
        skills.append(Skill(cfg.resolve_path(entry.source, repo), tuple(entry.targets)))
    return skills


def workspace_dir(cfg: Config, repo: Path, branch: str) -> Path:
    """Where project-sandbox puts ``branch``'s workspace with the typed settings."""
    wd = cfg.project.worktree_dir
    return workspace_path(repo, branch, cfg.resolve_path(wd, repo) if wd else None)


def planned_destinations(cfg: Config, repo: Path, branch: str) -> list[Path]:
    """Skill directories a launch on ``branch`` would deploy (for previews; no I/O)."""
    if branch == WORKING_COPY:
        return []
    root = workspace_dir(cfg, repo, branch)
    return [root / target for skill in configured_skills(cfg, repo) for target in skill.targets]


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _is_valid_copy(path: Path) -> bool:
    st = _lstat(path / SKILL_FILE)
    return st is not None and stat.S_ISREG(st.st_mode)


def _source_tree(source: Path) -> list[tuple[str, bool]]:
    """``(relative path, is directory)`` for every entry, parents first.

    Raises :class:`_Problem` for anything that is not a plain skill directory.
    """
    st = _lstat(source)
    if st is None:
        raise _Problem(f"source {source} does not exist")
    if stat.S_ISLNK(st.st_mode):
        raise _Problem(f"source {source} is a symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise _Problem(f"source {source} is not a directory")
    if not _is_valid_copy(source):
        raise _Problem(f"source {source} has no regular {SKILL_FILE}")
    entries: list[tuple[str, bool]] = []

    def walk(directory: Path, prefix: str) -> None:
        try:
            with os.scandir(directory) as it:
                children = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            raise _Problem(f"cannot read {directory}: {exc.strerror or exc}") from exc
        for entry in children:
            rel = f"{prefix}{entry.name}"
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                entries.append((rel, True))
                walk(Path(entry.path), f"{rel}/")
            elif stat.S_ISREG(mode):
                entries.append((rel, False))
            elif stat.S_ISLNK(mode):
                raise _Problem(f"{source / rel} is a symlink")
            else:
                raise _Problem(f"{source / rel} is not a regular file")

    walk(source, "")
    return entries


def _copy_file(src: Path, dst: Path) -> None:
    """Copy one regular file without following symlinks, keeping its permission bits."""
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as reader:
        st = os.fstat(reader.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise _Problem(f"{src} is not a regular file")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        with os.fdopen(os.open(dst, flags, 0o600), "wb") as writer:
            shutil.copyfileobj(reader, writer)
            os.fchmod(writer.fileno(), stat.S_IMODE(st.st_mode))


def _git_env() -> dict[str, str]:
    """Git without the host's configuration, repository or excludes."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, LC_ALL="C")
    return env


def _git(args: list[str], cwd: Path, stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            input=stdin,
            capture_output=True,
            env=_git_env(),
            check=False,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise _Problem("git is needed to check ignore rules but is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise _Problem("git check-ignore timed out") from exc


def ignore_status(
    workspace: Path, targets: Sequence[str], exclude: str = ""
) -> dict[str, tuple[bool, str]]:
    """Whether each target directory is ignored in ``workspace``: ``{target: (ignored, why)}``.

    Evaluates the in-tree ``.gitignore`` files on the target's path, plus
    ``exclude`` as ``info/exclude``, in a disposable mirror: the targets need
    not exist yet, and host-only excludes (global or system Git config, the
    real repository's metadata) are left out.
    """
    if not targets:
        return {}
    with tempfile.TemporaryDirectory(prefix="sbx-tui-ignore-") as tmp:
        mirror, git_dir = Path(tmp) / "tree", Path(tmp) / "git"
        mirror.mkdir()
        sources = {""}
        for target in targets:
            parts = target.split("/")
            sources.update("/".join(parts[:i]) for i in range(1, len(parts)))
        for rel in sorted(sources):
            src = workspace / rel / ".gitignore"
            st = _lstat(src)
            if st is None:
                continue
            if not stat.S_ISREG(st.st_mode):
                raise _Problem(f"{src} is not a regular file")
            (mirror / rel).mkdir(parents=True, exist_ok=True)
            _copy_file(src, mirror / rel / ".gitignore")
        for target in targets:
            (mirror / target).mkdir(parents=True, exist_ok=True)
        init = _git(["init", "--quiet", "--bare", str(git_dir)], Path(tmp))
        if init.returncode:
            raise _Problem(f"git init failed: {init.stderr.decode(errors='replace').strip()}")
        (git_dir / "info").mkdir(exist_ok=True)
        (git_dir / "info" / "exclude").write_text(exclude, encoding="utf-8")
        empty = Path(tmp) / "no-excludes"
        empty.touch()
        proc = _git(
            [
                f"--git-dir={git_dir}",
                f"--work-tree={mirror}",
                "-c",
                f"core.excludesFile={empty}",
                "check-ignore",
                "--no-index",
                "--verbose",
                "--non-matching",
                "-z",
                "--stdin",
            ],
            mirror,
            stdin=b"".join(t.encode() + b"\0" for t in targets),
        )
        if proc.returncode not in (0, 1):
            detail = proc.stderr.decode(errors="replace").strip()
            raise _Problem(f"git check-ignore failed: {detail}")
        fields = proc.stdout.decode(errors="replace").split("\0")
        result: dict[str, tuple[bool, str]] = {}
        for i in range(0, len(fields) - 3, 4):
            source, line, pattern, path = fields[i : i + 4]
            if source.startswith(str(git_dir)):
                source = "the repository's local info/exclude"
            elif source.startswith(str(mirror)):
                source = os.path.relpath(source, mirror)
            where = f"{source}:{line}: {pattern}"
            if not pattern:
                result[path] = (False, "no ignore rule matches it")
            elif pattern.startswith("!"):
                result[path] = (False, f"it is re-included by {where}")
            else:
                result[path] = (True, where)
        return result


def add_excludes(exclude_file: Path, rules: Sequence[str]) -> list[str]:
    """Append each missing rule once, preserving existing entries; return those added."""
    try:
        existing = exclude_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    lines = existing.splitlines()
    missing = [rule for rule in dict.fromkeys(rules) if rule not in lines]
    if missing:
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        with exclude_file.open("a", encoding="utf-8") as stream:
            if existing and not existing.endswith("\n"):
                stream.write("\n")
            stream.write("".join(f"{rule}\n" for rule in missing))
    return missing


def exclude_rule(target: str) -> str:
    return f"/{target}/"


@dataclass
class _Target:
    skill: Skill
    target: str
    kept: bool = False  # an existing valid copy stays as it is
    problem: str = ""


class WorkspacePreparer:
    """Creates bookmark workspaces and deploys skills into them.

    Called with ``jobs.lock`` held, after the busy check, so no TUI instance
    starts a session in the workspace meanwhile. Notes for the user collect
    until :meth:`take_notes`; launches can run on worker threads.
    """

    def __init__(self, repo: Path, backend: JjBackend) -> None:
        self.repo = repo
        self.backend = backend
        self._notes: list[Note] = []
        self._notes_lock = threading.Lock()

    def take_notes(self) -> list[Note]:
        with self._notes_lock:
            notes, self._notes = self._notes, []
        return notes

    def _note(self, message: str, *, warning: bool = False) -> None:
        with self._notes_lock:
            self._notes.append(Note(message, warning))

    # ---------------------------------------------------------- entry point

    def prepare(
        self,
        cfg: Config,
        branch: str,
        *,
        sandbox: bool,
        extra_args: Sequence[str] = (),
        deploy: bool = True,
    ) -> Path:
        """Prepare ``branch``'s workspace and return its path.

        ``sandbox``: for a project-sandbox start, which uses ``extra_args`` and
        creates the workspace itself when there is nothing to deploy. Host
        agents, editors and tasks always get the workspace created. Raises
        :class:`PreparationError` when the launch must not go ahead.
        """
        if branch == WORKING_COPY:
            return self.repo  # the checkout's own files
        path = workspace_dir(cfg, self.repo, branch)
        skills = configured_skills(cfg, self.repo) if deploy else []
        if sandbox:
            sandbox_args = [*cfg.sandbox.extra_args, *extra_args]
            if "--dry-run" in sandbox_args:
                if skills:
                    planned = ", ".join(str(path / t) for s in skills for t in s.targets)
                    self._note(f"{branch}: dry run, skills not deployed: {planned}")
                return path
        if os.path.lexists(path):
            self._check_registration(branch, path, sandbox=sandbox)
        elif sandbox and not skills:
            return path  # project-sandbox creates it
        else:
            try:
                self.backend.add_workspace(branch, path)
            except (VcsError, OSError) as exc:
                raise PreparationError(f"{branch}: could not create a workspace: {exc}") from exc
            self._note(f"{branch}: created workspace {path}")
        if skills:
            self._deploy(branch, path, skills)
        return path

    def _check_registration(self, branch: str, path: Path, *, sandbox: bool) -> None:
        """Check both the registration and the directory currently at its location."""
        if path.is_symlink() or path.resolve() == self.repo.resolve():
            raise PreparationError(f"{branch}: {path} must be its own workspace directory")
        try:
            names = self.backend.workspace_names()
            name = registered_workspace_name(self.repo, path)
        except (VcsError, OSError) as exc:
            raise PreparationError(f"{branch}: cannot verify workspace {path}: {exc}") from exc
        if names is not None and names.get(path.resolve()) != name:
            raise PreparationError(
                f"{branch}: {path} is not the registered location of workspace {name!r}"
            )
        if not sandbox and name == branch:
            return  # Legacy workspaces were named for their bookmarks.
        if name != path.name:
            raise PreparationError(
                f"{branch}: workspace {path} is registered as {name!r}, but project-sandbox "
                f"reuses and removes it as {path.name!r}; run `jj workspace rename "
                f"{path.name}` in it, or remove it with W"
            )

    # ---------------------------------------------------------- deployment

    def _deploy(self, branch: str, path: Path, skills: list[Skill]) -> None:
        with contextlib.ExitStack() as stack:
            sources: dict[Skill, Path] = {}
            trees: dict[Skill, list[tuple[str, bool]]] = {}
            plans: list[_Target] = [_Target(s, t) for s in skills for t in s.targets]
            for skill in skills:
                try:
                    if isinstance(skill.source, str):
                        bundled = files("project_sandbox_tui").joinpath("skills", skill.source)
                        sources[skill] = stack.enter_context(as_file(bundled))
                    else:
                        sources[skill] = skill.source
                    trees[skill] = _source_tree(sources[skill])
                except (_Problem, OSError) as exc:
                    for plan in plans:
                        if plan.skill == skill:
                            plan.problem = str(exc)
            try:
                self._check_targets(path, [p for p in plans if not p.problem])
            except _Problem as exc:
                for plan in plans:
                    plan.problem = plan.problem or str(exc)
            failed = [p for p in plans if p.problem and p.skill.explicit]
            for plan in plans:
                if plan.problem and not plan.skill.explicit:
                    self._note(
                        f"{branch}: {plan.skill.label} not deployed to {path / plan.target}: "
                        f"{plan.problem}",
                        warning=True,
                    )
            if failed:
                raise PreparationError(
                    "; ".join(
                        f"{branch}: cannot deploy {p.skill.label} to {path / p.target}: {p.problem}"
                        for p in failed
                    )
                )
            deployed: list[str] = []
            for plan in plans:
                if plan.problem or plan.kept:
                    continue
                try:
                    if self._publish(path, plan.target, sources[plan.skill], trees[plan.skill]):
                        deployed.append(plan.target)
                except (_Problem, OSError) as exc:
                    reason = str(exc)
                    if plan.skill.explicit:
                        raise PreparationError(
                            f"{branch}: cannot deploy {plan.skill.label} to "
                            f"{path / plan.target}: {reason}"
                        ) from exc
                    self._note(
                        f"{branch}: {plan.skill.label} not deployed to {path / plan.target}: "
                        f"{reason}",
                        warning=True,
                    )
            if deployed:
                self._note(f"{branch}: deployed skills to {', '.join(deployed)}")

    def _check_targets(self, path: Path, plans: list[_Target]) -> None:
        """Set each plan's problem or ``kept``; raise :class:`_Problem` for all of them."""
        if not plans:
            return
        st = _lstat(path)
        if st is None or stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise _Problem(f"{path} is not a workspace directory")
        if not (path / ".jj").is_dir():
            raise _Problem(f"{path} is not a jj workspace")
        try:
            tracked = tracked_files(path, [p.target for p in plans])
        except VcsError as exc:
            raise _Problem(f"cannot list its tracked files: {exc}") from exc
        for plan in plans:
            if hits := [f for f in tracked if f == plan.target or f.startswith(f"{plan.target}/")]:
                plan.problem = f"{hits[0]} is tracked in the bookmark"
                continue
            try:
                self._check_destination(path, plan)
            except _Problem as exc:
                plan.problem = str(exc)
        self._check_ignored(path, [p for p in plans if not p.problem])

    @staticmethod
    def _check_destination(path: Path, plan: _Target) -> None:
        current = path
        for part in plan.target.split("/")[:-1]:
            current = current / part
            st = _lstat(current)
            if st is None:
                break
            if stat.S_ISLNK(st.st_mode):
                raise _Problem(f"{current} is a symlink")
            if not stat.S_ISDIR(st.st_mode):
                raise _Problem(f"{current} is not a directory")
        dest = path / plan.target
        st = _lstat(dest)
        if st is None:
            return
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise _Problem("it exists and is not a skill directory")
        if not _is_valid_copy(dest):
            raise _Problem(f"it exists without a {SKILL_FILE}; remove it to deploy a fresh copy")
        plan.kept = True

    def _check_ignored(self, path: Path, plans: list[_Target]) -> None:
        """Explicit targets need in-tree rules; the bundled one may use local excludes."""
        explicit = [p for p in plans if p.skill.explicit]
        bundled = [p for p in plans if not p.skill.explicit]
        status = ignore_status(path, [p.target for p in plans])
        for plan in explicit:
            ignored, why = status.get(plan.target, (False, "git did not report on it"))
            if not ignored:
                plan.problem = (
                    f"the bookmark does not ignore it ({why}); add {exclude_rule(plan.target)} "
                    "to its .gitignore"
                )
        bundled = [p for p in bundled if not status.get(p.target, (False, ""))[0]]
        if not bundled:
            return
        git_dir = self.backend.git_dir()
        if git_dir is None:
            for plan in bundled:
                plan.problem = "the repository has no Git backend for local excludes"
            return
        exclude_file = git_dir / "info" / "exclude"

        def read_excludes() -> str:
            try:
                return exclude_file.read_text(encoding="utf-8")
            except FileNotFoundError:
                return ""

        targets = [p.target for p in bundled]
        status = ignore_status(path, targets, read_excludes())
        missing = [
            exclude_rule(t)
            for t in targets
            if not status.get(t, (False, ""))[0]
            and exclude_rule(t) not in read_excludes().splitlines()
        ]
        if missing:
            add_excludes(exclude_file, missing)
            status = ignore_status(path, targets, read_excludes())
        for plan in bundled:
            ignored, why = status.get(plan.target, (False, "git did not report on it"))
            if not ignored:
                plan.problem = f"it cannot be ignored ({why})"

    @staticmethod
    def _publish(path: Path, target: str, source: Path, tree: list[tuple[str, bool]]) -> bool:
        """Stage the complete copy under ``.jj`` and rename it into place.

        Returns False if a valid copy appeared meanwhile (it is kept).
        """
        staging = path / ".jj" / f"{STAGING_PREFIX}{uuid.uuid4().hex}"
        staging.mkdir(mode=0o700)
        try:
            staged = staging / target.rsplit("/", 1)[1]
            staged.mkdir()
            _copy_file(source / SKILL_FILE, staged / SKILL_FILE)
            for rel, is_dir in tree:
                if rel == SKILL_FILE:
                    continue
                if is_dir:
                    (staged / rel).mkdir()
                else:
                    _copy_file(source / rel, staged / rel)
            os.chmod(staged, 0o755)
            parent = path
            for part in target.split("/")[:-1]:
                parent = parent / part
                with contextlib.suppress(FileExistsError):
                    parent.mkdir()
                st = os.lstat(parent)
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                    raise _Problem(f"{parent} is not a plain directory")
            dest = path / target
            if os.path.lexists(dest):
                if _is_valid_copy(dest) and not os.path.islink(dest):
                    return False
                raise _Problem("it appeared while copying and is not a skill directory")
            os.rename(staged, dest)
            return True
        finally:
            shutil.rmtree(staging, ignore_errors=True)
