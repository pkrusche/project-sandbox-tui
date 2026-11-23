"""Key references must not drift from the bindings: help, cheat-sheet and docs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from textual.binding import Binding

from project_sandbox_tui import commands
from project_sandbox_tui.app import CHEATSHEET, MAIN_BINDINGS
from project_sandbox_tui.screens.command import CommandScreen
from project_sandbox_tui.screens.dialogs import HELP, CleanupScreen
from project_sandbox_tui.screens.history import HistorySearchScreen
from project_sandbox_tui.screens.launch import LaunchScreen
from project_sandbox_tui.screens.log import LogScreen
from project_sandbox_tui.screens.oplog import OpLogScreen
from project_sandbox_tui.screens.profiles import ProfilesScreen
from project_sandbox_tui.screens.queue import QueueScreen
from project_sandbox_tui.screens.review import CommitReviewScreen, DiffReviewScreen
from project_sandbox_tui.screens.tasks import TasksScreen
from project_sandbox_tui.screens.workflow import WorkflowProfilesScreen
from tests.test_docs_examples import section

DOCS = Path(__file__).parent.parent / "docs"
# Textual key names as the references spell them.
SPELLING = {
    "up": "↑",
    "down": "↓",
    "left_square_bracket": "[",
    "right_square_bracket": "]",
    "slash": "/",
    "equals_sign": "=",
    "at": "@",
    "question_mark": "?",
    "escape": "esc",
    "colon": ":",
    "apostrophe": "'",
}
PREFIXES = {"g": "gg", "d": "dd"}  # bound per key, typed twice


def bound_keys(bindings: list[Binding]) -> set[str]:
    keys = {key for binding in bindings for key in binding.key.split(",")}
    return {PREFIXES.get(key, SPELLING.get(key, key)) for key in keys}


def reference_keys(cells: list[str]) -> set[str]:
    """Keys in key columns such as ``u / ctrl+r`` or ``ctrl+d / u`` (ctrl+u)."""
    found: set[str] = set()
    for cell in cells:
        modifier = ""
        for token in re.split(r"\s+/\s+|\s+", cell.strip()):
            if token.startswith(("ctrl+", "shift+")):
                modifier = token.split("+")[0] + "+"
                found.add(token)
            elif modifier and len(token) == 1 and token.islower():
                found.add(modifier + token)  # ctrl+d / u
            else:
                found.add({"^": "ctrl+"}.get(token, token))
    return found


def key_column(doc: str, heading: str, column: str = "Key") -> set[str]:
    table = next(
        block
        for block in section(doc, heading).strip().split("\n\n")
        if block.startswith(f"| {column} |")
    )
    cells = [line.split("|")[1] for line in table.splitlines()[2:]]
    return {key for cell in cells for key in re.findall(r"`([^`]+)`", cell)}


def main_bindings() -> list[Binding]:
    return [b for b in MAIN_BINDINGS if isinstance(b, Binding)]


def test_help_lists_every_bookmark_list_key() -> None:
    help_keys = reference_keys([key for column in HELP for _t, keys in column for key, _ in keys])
    descriptions = " ".join(what for column in HELP for _t, keys in column for _k, what in keys)
    missing = bound_keys(main_bindings()) - help_keys - {"esc"}
    assert not missing, f"keys missing from the help: {sorted(missing)}"
    assert "esc" in descriptions


def test_cheatsheet_lists_every_bookmark_list_key_but_esc() -> None:
    symbols = {"^": "ctrl+", "⏎": "enter", "␣": "space"}
    cells = [
        "".join(symbols.get(ch, ch) for ch in key) for _title, keys in CHEATSHEET for key, _ in keys
    ]
    missing = bound_keys(main_bindings()) - reference_keys(cells) - {"esc"}
    assert not missing, f"keys missing from the cheat-sheet: {sorted(missing)}"


@pytest.mark.parametrize(
    ("bindings", "heading"),
    [
        (main_bindings(), "## Bookmark list"),
        (LogScreen.BINDINGS, "## Log viewer"),
        (OpLogScreen.BINDINGS, "## Op history"),
        (TasksScreen.BINDINGS, "## Tasks"),
        (QueueScreen.BINDINGS, "## Prompt queue"),
        (DiffReviewScreen.BINDINGS, "## Diff review"),
        (CommitReviewScreen.BINDINGS, "## Diff review"),
        (LaunchScreen.BINDINGS, "## Launch form"),
        (ProfilesScreen.BINDINGS, "### Profile editor"),
        (WorkflowProfilesScreen.BINDINGS, "### Workflow setup"),
        (CommandScreen.BINDINGS, "## Command line"),
        (HistorySearchScreen.BINDINGS, "### History search"),
    ],
)
def test_docs_list_every_key(bindings: list[Binding], heading: str) -> None:
    missing = bound_keys([b for b in bindings if isinstance(b, Binding)]) - key_column(
        "usage.md", heading
    )
    assert not missing, f"keys missing from {heading}: {sorted(missing)}"


def test_cleanup_keys() -> None:
    assert bound_keys([b for b in CleanupScreen.BINDINGS if isinstance(b, Binding)]) <= key_column(
        "cleanup.md", "## Cleanup keys"
    )


def test_command_table_lists_builtins() -> None:
    entries = key_column("usage.md", "## Command line", "Command")
    names = {entry.split()[0][1:] for entry in entries if entry.startswith(":")}
    assert (commands.BUILTINS.keys() | commands.ALIASES.keys()) <= names
