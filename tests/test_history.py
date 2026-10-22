from __future__ import annotations

from pathlib import Path

from project_sandbox_tui.history import PromptHistory


def test_add_dedupes_and_caps(tmp_path: Path) -> None:
    h = PromptHistory(tmp_path / "h.json", max_entries=3)
    for p in ("fix the tests", "  add docs  ", "", "fix the tests", "refactor", "bump deps"):
        h.add(p)
    # blank ignored, whitespace stripped, duplicate moved to newest, capped at 3
    assert h.entries == ["fix the tests", "refactor", "bump deps"]
    assert PromptHistory(tmp_path / "h.json").entries == h.entries  # persisted


def test_queries(tmp_path: Path) -> None:
    h = PromptHistory(tmp_path / "h.json")
    for p in (
        "fix the flaky test",
        "fix lint",
        "Write the README\nwith examples",
        "fix lint again",
    ):
        h.add(p)
    assert h.suggest("fix") == "fix lint again"  # newest extension wins
    assert h.suggest("fix the") == "fix the flaky test"
    assert h.suggest("fix lint again") is None  # nothing longer
    assert h.suggest("   ") is None
    assert h.with_prefix("fix l") == ["fix lint again", "fix lint"]
    assert h.with_prefix("") == h.newest_first()
    assert h.search("readme EXAMPLES") == ["Write the README\nwith examples"]  # all words, any case
    assert h.search("") == h.newest_first()


def test_instances_merge_instead_of_overwriting(tmp_path: Path) -> None:
    a = PromptHistory(tmp_path / "h.json")
    b = PromptHistory(tmp_path / "h.json")  # second TUI on the same repo
    a.add("from a")
    b.add("from b")
    assert PromptHistory(tmp_path / "h.json").entries == ["from a", "from b"]


def test_corrupt_or_missing_file_is_empty(tmp_path: Path) -> None:
    assert PromptHistory(tmp_path / "missing.json").entries == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert PromptHistory(bad).entries == []
    bad.write_text('{"prompts": ["ok", 3, "  ", null]}')
    assert PromptHistory(bad).entries == ["ok"]
