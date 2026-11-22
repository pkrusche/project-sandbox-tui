"""``:reorganise``: rebuild a bookmark's changes as a series of smaller commits.

The run goes to a new bookmark (``<bookmark>-reorganised`` by default) created
from main, so the original commits stay untouched for comparison. The agent
reads ``main..<bookmark>``, finds the specification or task list the changes
implement, and recreates the same final tree as one commit per task.
"""

from __future__ import annotations

import shlex
from collections.abc import Collection

from project_sandbox_tui.vcs import revset_symbol

SUFFIX = "-reorganised"


def result_name(source: str, existing: Collection[str]) -> str:
    """``<source>-reorganised``, numbered (``-reorganised-2``…) if that bookmark exists."""
    name = f"{source}{SUFFIX}"
    number = 2
    while name in existing:
        name = f"{source}{SUFFIX}-{number}"
        number += 1
    return name


def _revset(text: str) -> str:
    """A revset quoted for the shell commands shown in the prompt."""
    return shlex.quote(text)


def prompt(source: str, main: str, result: str) -> str:
    """The pre-filled prompt for reorganising ``main..source`` into ``result``."""
    src, base, dest = revset_symbol(source), revset_symbol(main), revset_symbol(result)
    commits = _revset(f"{base}..{src}")
    lines = [
        f"Reorganise the changes in {main}..{source} into a series of smaller commits on the "
        f"bookmark {result}, each aligned with one task of the specification or implementation "
        "plan those changes implement. Follow AGENTS.md.",
        "",
        "1. Read the existing commits and their combined diff: "
        f"`jj log -r {commits}` and `jj diff --from {_revset(base)} --to {_revset(src)} --stat`.",
        "2. Find the specification or plan: an OpenSpec change (proposal, design, specs and "
        "tasks.md), a Markdown plan or checklist, or the commit descriptions. Use its tasks to "
        "choose and order the commits. Without one, group the changes into coherent logical "
        "steps.",
        f"3. {result} starts at {main} in this workspace. Build the series on it with jj, one "
        "commit per task or step: for example the specification or plan first, then each "
        "implementation task with its tests and documentation. Take content from "
        f"{source} with `jj restore --from {_revset(src)} -- PATHS`, and split or edit files "
        "where one file holds changes for several tasks. Describe each commit with the task it "
        "implements. Prefer commits that build and pass their targeted tests on their own.",
        f"4. Do not change behaviour: the final tree must equal {source}, so "
        f"`jj diff --from {_revset(src)} --to {_revset(dest)}` must print nothing. Point "
        f"{result} at the last commit of the series with "
        f"`jj bookmark set {_revset(dest)} -r <last commit>`.",
        f"5. Leave {source}, {main} and every other bookmark and their commits unchanged.",
        "6. Run the project's checks on the final commit and report the commit list with each "
        "commit's task. If jj cannot create commits here (read-only or hidden VCS access), "
        "report blocked without making changes.",
    ]
    return "\n".join(lines) + "\n"
