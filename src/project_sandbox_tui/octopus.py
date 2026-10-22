"""Octopus merge / stacked rebase of several branches into a new result branch.

The TUI does the mechanical part on the host before any agent runs, then hands
the result branch to a normal session whose prompt asks the agent to resolve
what is left and review the integration. The ``bash`` agent instead summarizes
changes and fails when conflicts remain.

- merge:  ``jj new --no-edit A B C`` + bookmark ``result`` on it. jj records
  conflicts in the commit; the session starts on top of it.
- rebase: ``jj duplicate`` each input's own commits onto the previous tip
  (starting at ``base``), bookmark ``result`` on the last duplicate.

Inputs are never rewritten.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from project_sandbox_tui.vcs import validate_branch_name

Operation = Literal["merge", "rebase"]
OPERATIONS: tuple[str, ...] = ("merge", "rebase")


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
    clean: bool
    steps: list[str] = field(default_factory=list)  # what was done, in order
    conflicts: list[str] = field(default_factory=list)  # paths or commits with conflicts

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


DEFAULT_TEMPLATES: dict[str, str] = {
    "merge": (
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
    "rebase": (
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
}


def bash_prompt(plan: OctopusPlan) -> str:
    """Summarize the integration without resolving it; fail on conflicts."""
    baseline = plan.base if plan.operation == "rebase" else plan.inputs[0]
    # Include conflicts in intermediate commits, even if the final tree is clean.
    return (
        "jj status &&\n"
        f"jj diff --stat --from {shlex.quote(baseline)} --to @- &&\n"
        "conflicts=$(jj log --no-graph -r '::@- & conflicts()' "
        "-T 'commit_id.short()') &&\n"
        'printf "Conflicted commits:\n%s\n" "$conflicts" &&\n'
        '[ -z "$conflicts" ]'
    )


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


def _merge(repo: Path, plan: OctopusPlan) -> OctopusState:
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
    state = OctopusState(plan, clean=not conflicts, conflicts=conflicts)
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


def _rebase(repo: Path, plan: OctopusPlan) -> OctopusState:
    base = _revset_symbol(plan.base)
    if not _jj_ids(repo, base):
        raise OctopusError(f"no such revision: {plan.base}")
    tip = _jj_ids(repo, base)[0]
    state = OctopusState(plan, clean=True)
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


# ---------------------------------------------------------------- entry point


def prepare(repo: Path, plan: OctopusPlan) -> OctopusState:
    """Do the mechanical merge / rebase on the host. Raises OctopusError before
    anything is changed for invalid input; jj failures later surface as
    OctopusError too (the result bookmark may then exist and can be inspected)."""
    return _merge(repo, plan) if plan.operation == "merge" else _rebase(repo, plan)
