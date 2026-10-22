"""Octopus merge / stacked rebase of several branches into a new result branch.

The TUI does the mechanical part on the host before any agent runs, then hands
the result branch to a normal session whose prompt asks the agent to resolve
what is left and review the integration (pick the ``bash`` agent to do that
yourself).

- jj merge:  ``jj new --no-edit A B C`` + bookmark ``result`` on it. jj records
  conflicts in the commit; the session starts on top of it.
- jj rebase: ``jj duplicate`` each input's own commits onto the previous tip
  (starting at ``base``), bookmark ``result`` on the last duplicate. Inputs are
  never rewritten.
- git merge: worktree for ``result`` (created at the first input) at
  project-sandbox's own worktree path, then ``git merge`` of the others: one
  octopus merge if it is clean, otherwise one at a time, stopping at the first
  conflict so the session resolves it in place.
- git rebase: worktree for ``result`` at ``base``, then ``git cherry-pick`` of
  each input's own commits, stopping at the first conflict.

Inputs are only read. Host-side git runs with hooks disabled: the sandbox has
``.git`` mounted read-write, so a hook written by an agent must never run here.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from project_sandbox_tui.vcs import Kind, VcsError, validate_branch_name, workspace_path

Operation = Literal["merge", "rebase"]
OPERATIONS: tuple[str, ...] = ("merge", "rebase")

# Disable hooks for every git command run on the host (see module docstring).
_GIT = ["git", "-c", "core.hooksPath=/dev/null"]


@dataclass
class OctopusPlan:
    operation: Operation
    inputs: list[str]  # in merge / stacking order
    result: str
    base: str = ""  # rebase only


@dataclass
class OctopusState:
    """What the TUI managed to do; appended to the agent's prompt."""

    plan: OctopusPlan
    kind: Kind
    clean: bool
    steps: list[str] = field(default_factory=list)  # what was done, in order
    conflicts: list[str] = field(default_factory=list)  # paths or commits with conflicts
    pending: list[str] = field(default_factory=list)  # inputs / commits still to apply
    workspace: Path | None = None  # git: the pre-created worktree

    def summary(self) -> str:
        what = "merge" if self.plan.operation == "merge" else "rebase"
        if self.clean:
            return f"{what} into {self.plan.result} applied cleanly"
        return f"{what} into {self.plan.result} stopped with conflicts"

    def report(self) -> str:
        """Markdown section describing the current state, for the prompt."""
        lines = ["## Current state (prepared by sbx-tui before this session)", ""]
        lines += [f"- {step}" for step in self.steps]
        if self.conflicts:
            lines += ["", "Conflicts to resolve:"]
            lines += [f"- `{c}`" for c in self.conflicts]
        if self.pending:
            lines += ["", "Still to apply after resolving, in this order:"]
            lines += [f"- `{p}`" for p in self.pending]
        if self.clean:
            lines += ["", "No conflicts were reported; review the result anyway."]
        return "\n".join(lines)


class OctopusError(RuntimeError):
    """The plan is invalid or the preparation failed before anything was changed."""


# ---------------------------------------------------------------- names & prompts


def default_result_name(operation: str, inputs: list[str], max_len: int = 60) -> str:
    """``merge/a-b-c`` or ``rebase/a-b-c``: no revset operators like ``+``."""
    parts = [re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-.") for name in inputs]
    name = f"{operation}/" + "-".join(p for p in parts if p)
    if len(name) > max_len:
        name = name[:max_len].rstrip("-.")
    return name


DEFAULT_TEMPLATES: dict[tuple[str, str], str] = {
    ("merge", "jj"): (
        "Integrate the bookmarks {inputs} into the bookmark `{result}`.\n\n"
        "sbx-tui has created the merge commit (`jj new {inputs_plain}`) and pointed "
        "`{result}` at it; your working copy sits on top of it. jj records conflicts in "
        "the commit, see the state below and `jj resolve --list -r @-`.\n\n"
        "1. Resolve every conflict so that the intent of each branch survives, then fold "
        "the resolution into the merge commit (`jj squash`).\n"
        "2. Review the integrated result for semantic conflicts the VCS cannot see "
        "(renamed APIs, duplicated logic, changed assumptions between branches).\n"
        "3. Run the test suite and fix integration breakage.\n\n"
        "Do not modify or move the input bookmarks {inputs}, and do not push.\n"
        "Finish with a short summary: each conflict and how you resolved it, and any "
        "follow-ups."
    ),
    ("merge", "git"): (
        "Integrate the branches {inputs} into the branch `{result}`.\n\n"
        "sbx-tui has created `{result}` from `{first}` in this worktree and started "
        "merging the others; see the state below (`git status` shows any merge in "
        "progress).\n\n"
        "1. Resolve the conflicts, commit the merge, then merge the remaining branches "
        "listed below one at a time (`git merge --no-ff <branch>`), resolving as you go.\n"
        "2. Review the integrated result for semantic conflicts the VCS cannot see "
        "(renamed APIs, duplicated logic, changed assumptions between branches).\n"
        "3. Run the test suite and fix integration breakage.\n\n"
        "Do not modify the branches {inputs}, and do not push.\n"
        "Finish with a short summary: each conflict and how you resolved it, and any "
        "follow-ups."
    ),
    ("rebase", "jj"): (
        "Build `{result}` as a linear history on top of `{base}` containing the work "
        "of {inputs}, in that order.\n\n"
        "sbx-tui has duplicated each bookmark's own commits onto the previous one "
        "(`jj duplicate`), starting at `{base}`, and pointed `{result}` at the last "
        "duplicate; your working copy sits on top of it. The input bookmarks are "
        "unchanged. jj records conflicts in the commits, see the state below "
        "(`jj log -r 'conflicts()'`).\n\n"
        "1. Resolve conflicts in each conflicted commit, oldest first (`jj new <commit>`, "
        "resolve, `jj squash`), so every commit in the stack is conflict-free.\n"
        "2. Review the stack: each commit should still make sense on its new base.\n"
        "3. Run the test suite on the final commit and fix breakage.\n\n"
        "Do not modify or move the input bookmarks {inputs}, and do not push.\n"
        "Finish with a short summary of conflicts, resolutions and follow-ups."
    ),
    ("rebase", "git"): (
        "Build `{result}` as a linear history on top of `{base}` containing the commits "
        "of {inputs}, in that order.\n\n"
        "sbx-tui has created `{result}` at `{base}` in this worktree and started "
        "cherry-picking each branch's own commits; see the state below (`git status` "
        "shows a cherry-pick in progress).\n\n"
        "1. Resolve the conflict and `git cherry-pick --continue`; then cherry-pick the "
        "remaining commits listed below, oldest first, resolving as you go.\n"
        "2. Review the stack: each commit should still make sense on its new base.\n"
        "3. Run the test suite on the final commit and fix breakage.\n\n"
        "Do not modify the branches {inputs}, and do not push.\n"
        "Finish with a short summary of conflicts, resolutions and follow-ups."
    ),
}


def render_prompt(template: str, plan: OctopusPlan) -> str:
    """Fill ``{inputs}``, ``{inputs_plain}``, ``{result}``, ``{base}``, ``{first}``, ``{rest}``."""
    values = {
        "inputs": ", ".join(f"`{i}`" for i in plan.inputs),
        "inputs_plain": " ".join(plan.inputs),
        "result": plan.result,
        "base": plan.base or "(none)",
        "first": plan.inputs[0] if plan.inputs else "",
        "rest": ", ".join(f"`{i}`" for i in plan.inputs[1:]),
    }
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


# ---------------------------------------------------------------- validation


def validate(plan: OctopusPlan, existing: set[str]) -> None:
    if len(plan.inputs) < 2:
        raise OctopusError("select at least two branches")
    if len(set(plan.inputs)) != len(plan.inputs):
        raise OctopusError("the same branch is selected twice")
    missing = [i for i in plan.inputs if i not in existing]
    if missing:
        raise OctopusError(f"no such branch: {', '.join(missing)}")
    error = validate_branch_name(plan.result)
    if error:
        raise OctopusError(error)
    if plan.result in existing:
        raise OctopusError(f"{plan.result} already exists; pick another result name")
    if plan.operation == "rebase" and not plan.base:
        raise OctopusError("a rebase needs a base")


# ---------------------------------------------------------------- helpers


def _run(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError as exc:
        raise OctopusError(f"{args[0]} not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise OctopusError(f"{' '.join(args[:4])} timed out") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise OctopusError(detail[-1] if detail else f"{' '.join(args[:4])} failed")
    return proc


def _revset_symbol(name: str) -> str:
    """Quote a bookmark name for a jj revset (``/`` and ``-`` are fine, ``+`` is not)."""
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------- jj


def _jj(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["jj", "--no-pager", "--color=never", *args], repo, check=check)


def _jj_ids(repo: Path, revset: str) -> list[str]:
    out = _jj(repo, "log", "--no-graph", "-r", revset, "-T", 'commit_id ++ "\\n"').stdout
    return [line for line in out.splitlines() if line]


def _jj_conflicted_paths(repo: Path, rev: str) -> list[str]:
    proc = _jj(repo, "resolve", "--list", "-r", rev, check=False)
    if proc.returncode != 0:  # "No conflicts found at this revision"
        return []
    return [line.split()[0] for line in proc.stdout.splitlines() if line.strip()]


def _jj_new_heads(repo: Path, before: set[str], within: str) -> list[str]:
    return [h for h in _jj_ids(repo, f"heads({within})") if h not in before]


def _jj_merge(repo: Path, plan: OctopusPlan) -> OctopusState:
    symbols = [_revset_symbol(i) for i in plan.inputs]
    common = " & ".join(f"children({s})" for s in symbols)
    before = set(_jj_ids(repo, f"heads({common})"))
    message = f"Merge {', '.join(plan.inputs)} into {plan.result}"
    _jj(repo, "new", "--no-edit", "-m", message, *symbols)
    created = _jj_new_heads(repo, before, common)
    if len(created) != 1:
        raise OctopusError("could not identify the new merge commit")
    merge = created[0]
    _jj(repo, "bookmark", "create", plan.result, "-r", merge)
    conflicts = _jj_conflicted_paths(repo, merge)
    state = OctopusState(plan, "jj", clean=not conflicts, conflicts=conflicts)
    state.steps.append(
        f"Created merge commit {merge[:12]} of {', '.join(f'`{i}`' for i in plan.inputs)}"
        f" and bookmark `{plan.result}` on it."
    )
    state.steps.append(
        f"{len(conflicts)} conflicted path(s) recorded in the merge commit."
        if conflicts
        else "The merge has no conflicts."
    )
    return state


def _jj_duplicate(repo: Path, revset: str, onto: str) -> None:
    proc = _jj(repo, "duplicate", "--onto", onto, revset, check=False)
    if proc.returncode != 0 and "--onto" in (proc.stderr or ""):
        _jj(repo, "duplicate", "-d", onto, revset)  # jj < 0.3x spelled it --destination
    elif proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise OctopusError(detail[-1] if detail else "jj duplicate failed")


def _jj_rebase(repo: Path, plan: OctopusPlan) -> OctopusState:
    base = _revset_symbol(plan.base)
    if not _jj_ids(repo, base):
        raise OctopusError(f"no such revision: {plan.base}")
    tip = _jj_ids(repo, base)[0]
    state = OctopusState(plan, "jj", clean=True)
    done: list[str] = []
    for name in plan.inputs:
        own = f"({base}..{_revset_symbol(name)})"
        if done:  # skip commits an earlier input already brought in
            own += " ~ ::(" + " | ".join(done) + ")"
        if not _jj_ids(repo, own):
            state.steps.append(f"`{name}` has no commits of its own on top of `{plan.base}`.")
            done.append(_revset_symbol(name))
            continue
        before = set(_jj_ids(repo, f"heads(descendants({tip}))"))
        _jj_duplicate(repo, own, tip)
        created = _jj_new_heads(repo, before, f"descendants({tip})")
        if len(created) != 1:
            raise OctopusError(f"could not identify the duplicates of {name}")
        count = len(_jj_ids(repo, f"{tip}..{created[0]}"))
        state.steps.append(f"Duplicated {count} commit(s) of `{name}` onto {tip[:12]}.")
        tip = created[0]
        done.append(_revset_symbol(name))
    _jj(repo, "bookmark", "create", plan.result, "-r", tip)
    conflicted = _jj_ids(repo, f"{base}::{tip} & conflicts()")
    state.conflicts = [c[:12] for c in conflicted]
    state.clean = not conflicted
    state.steps.append(f"Bookmark `{plan.result}` points at {tip[:12]}.")
    if conflicted:
        state.steps.append(f"{len(conflicted)} commit(s) in the stack have conflicts.")
    return state


# ---------------------------------------------------------------- git


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run([*_GIT, *args], path, check=check)


def _git_unmerged(path: Path) -> list[str]:
    out = _git(path, "diff", "--name-only", "--diff-filter=U").stdout
    return [line for line in out.splitlines() if line]


def _git_worktree(repo: Path, plan: OctopusPlan, start: str, worktree_dir: Path | None) -> Path:
    path = workspace_path(repo, "git", plan.result, worktree_dir)
    if path.exists():
        raise OctopusError(f"{path} already exists; remove it or pick another result name")
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-b", plan.result, str(path), start)
    return path


def _git_merge(repo: Path, plan: OctopusPlan, worktree_dir: Path | None) -> OctopusState:
    first, rest = plan.inputs[0], plan.inputs[1:]
    path = _git_worktree(repo, plan, first, worktree_dir)
    state = OctopusState(plan, "git", clean=True, workspace=path)
    state.steps.append(f"Created branch `{plan.result}` at `{first}` in worktree {path}.")
    message = f"Merge {', '.join(rest)} into {plan.result}"
    if len(rest) > 1:
        octopus = _git(path, "merge", "--no-ff", "--no-edit", "-m", message, *rest, check=False)
        if octopus.returncode == 0:
            state.steps.append(f"Octopus-merged {', '.join(f'`{r}`' for r in rest)} cleanly.")
            return state
        # The octopus strategy refuses conflicts; start over one branch at a time.
        _git(path, "merge", "--abort", check=False)
        _git(path, "reset", "--hard", "HEAD")
        state.steps.append("The octopus merge had conflicts; merging one branch at a time.")
    for index, name in enumerate(rest):
        merged = _git(path, "merge", "--no-ff", "--no-edit", name, check=False)
        if merged.returncode == 0:
            state.steps.append(f"Merged `{name}` cleanly.")
            continue
        state.clean = False
        state.conflicts = _git_unmerged(path)
        state.pending = rest[index + 1 :]
        state.steps.append(f"Merging `{name}` stopped with conflicts (merge in progress).")
        return state
    return state


def _git_rebase(repo: Path, plan: OctopusPlan, worktree_dir: Path | None) -> OctopusState:
    if _git(
        repo, "rev-parse", "--verify", "--quiet", plan.base + "^{commit}", check=False
    ).returncode:
        raise OctopusError(f"no such revision: {plan.base}")
    path = _git_worktree(repo, plan, plan.base, worktree_dir)
    state = OctopusState(plan, "git", clean=True, workspace=path)
    state.steps.append(f"Created branch `{plan.result}` at `{plan.base}` in worktree {path}.")
    picked: list[str] = []
    queue: list[tuple[str, list[str]]] = []
    for name in plan.inputs:
        exclude = [f"^{p}" for p in picked]
        rev_range = f"{plan.base}..{name}"
        out = _git(repo, "rev-list", "--reverse", "--no-merges", rev_range, *exclude).stdout
        queue.append((name, out.split()))
        picked.append(name)
    for index, (name, commits) in enumerate(queue):
        if not commits:
            state.steps.append(f"`{name}` has no commits of its own on top of `{plan.base}`.")
            continue
        for position, commit in enumerate(commits):
            res = _git(path, "cherry-pick", commit, check=False)
            if res.returncode == 0:
                continue
            if not _git_unmerged(path):
                # Nothing conflicts: the change is already on the stack (empty pick).
                _git(path, "cherry-pick", "--skip", check=False)
                state.steps.append(f"Skipped {commit[:12]} of `{name}`: already applied.")
                continue
            state.clean = False
            state.conflicts = _git_unmerged(path)
            later = [c[:12] + f" ({name})" for c in commits[position + 1 :]]
            later += [c[:12] + f" ({n})" for n, cs in queue[index + 1 :] for c in cs]
            state.pending = later
            state.steps.append(
                f"Cherry-picking {commit[:12]} of `{name}` stopped with conflicts "
                "(cherry-pick in progress)."
            )
            return state
        state.steps.append(f"Cherry-picked {len(commits)} commit(s) of `{name}`.")
    return state


# ---------------------------------------------------------------- entry point


def prepare(
    kind: Kind, repo: Path, plan: OctopusPlan, worktree_dir: Path | None = None
) -> OctopusState:
    """Do the mechanical merge / rebase on the host. Raises OctopusError before
    anything is changed for invalid input; VCS failures later surface as
    OctopusError too (the result branch may then exist and can be inspected)."""
    try:
        if kind == "jj":
            return _jj_merge(repo, plan) if plan.operation == "merge" else _jj_rebase(repo, plan)
        if plan.operation == "merge":
            return _git_merge(repo, plan, worktree_dir)
        return _git_rebase(repo, plan, worktree_dir)
    except VcsError as exc:
        raise OctopusError(str(exc)) from exc
