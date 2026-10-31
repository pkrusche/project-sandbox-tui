"""Key references must not drift from the bindings: help, cheat-sheet and docs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from textual.binding import Binding

from project_sandbox_tui.app import CHEATSHEET, MAIN_BINDINGS
from project_sandbox_tui.screens.dialogs import HELP, CleanupScreen
from project_sandbox_tui.screens.log import LogScreen
from project_sandbox_tui.screens.oplog import OpLogScreen

DOCS = Path(__file__).parent.parent / "docs"
# Textual key names as the references spell them.
SPELLING = {
    "slash": "/",
    "equals_sign": "=",
    "at": "@",
    "question_mark": "?",
    "escape": "esc",
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


def docs_keys(name: str) -> set[str]:
    return set(re.findall(r"`([^`\s]+)`", (DOCS / name).read_text()))


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
    ("bindings", "doc"),
    [
        (main_bindings(), "usage.md"),
        (LogScreen.BINDINGS, "usage.md"),
        (OpLogScreen.BINDINGS, "usage.md"),
        (CleanupScreen.BINDINGS, "cleanup.md"),
    ],
    ids=["bookmark-list", "log-viewer", "op-history", "cleanup"],
)
def test_docs_list_every_key(bindings: list[Binding], doc: str) -> None:
    missing = bound_keys([b for b in bindings if isinstance(b, Binding)]) - docs_keys(doc)
    assert not missing, f"keys missing from docs/{doc}: {sorted(missing)}"
