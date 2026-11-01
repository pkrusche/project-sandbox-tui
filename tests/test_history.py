from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def test_simultaneous_adds_do_not_lose_prompts(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)
    path = tmp_path / "h.json"
    histories = [PromptHistory(path), PromptHistory(path)]

    def add(index: int) -> None:
        barrier.wait(timeout=5)
        histories[index].add(f"prompt {index}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(add, range(2)))
    entries = PromptHistory(path).entries
    assert len(entries) == 2 and set(entries) == {"prompt 0", "prompt 1"}


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        "[]",
        "null",
        '"text"',
        '{"prompts": null}',
        '{"prompts": "text"}',
        '{"prompts": [3, null, "  "]}',
    ],
)
def test_corrupt_file_is_empty(tmp_path: Path, raw: str) -> None:
    path = tmp_path / "h.json"
    path.write_text(raw)
    assert PromptHistory(path).entries == []


def test_missing_file_is_empty(tmp_path: Path) -> None:
    assert PromptHistory(tmp_path / "missing.json").entries == []


def test_mixed_entries_are_filtered(tmp_path: Path) -> None:
    path = tmp_path / "h.json"
    path.write_text('{"prompts": ["ok", 3, "  ", null]}')
    assert PromptHistory(path).entries == ["ok"]


def test_blank_prompt_does_not_create_files(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "h.json"
    history = PromptHistory(path)
    history.add(" \n\t")
    assert history.entries == []
    assert not path.parent.exists()


@pytest.mark.parametrize("operation", ["dump", "replace"])
def test_failed_write_preserves_history_and_cleans_temporary_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    path = tmp_path / "h.json"
    history = PromptHistory(path)
    history.add("saved")

    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    with monkeypatch.context() as patch:
        # dump: writing fails once the temporary file exists.
        patch.setattr(*((json, "dump") if operation == "dump" else (Path, "replace")), fail)
        history.add("unsaved")
    assert history.entries == ["saved"]
    assert PromptHistory(path).entries == ["saved"]
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("limit", [0, -1])
def test_nonpositive_history_limit_is_rejected(tmp_path: Path, limit: int) -> None:
    with pytest.raises(ValueError, match="max_entries"):
        PromptHistory(tmp_path / "h.json", max_entries=limit)


def test_queries_respect_prefix_case_and_require_all_search_terms(tmp_path: Path) -> None:
    history = PromptHistory(tmp_path / "h.json")
    history.add("Fix café tests")
    assert history.suggest("fix") is None
    assert history.with_prefix("fix") == []
    assert history.search("CAFÉ missing") == []
    assert history.search("CAFÉ tests") == ["Fix café tests"]
    newest = history.newest_first()
    newest.clear()
    assert history.entries == ["Fix café tests"]


def test_unreadable_history_is_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "h.json"
    history = PromptHistory(path)
    history.add("saved")
    saved = path.read_bytes()
    real = Path.read_bytes

    def flaky(self: Path) -> bytes:
        if self == path:
            raise PermissionError(13, "Permission denied")
        return real(self)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", flaky)
        history.add("new")  # a convenience: not saved, but not lost either
    assert path.read_bytes() == saved
    assert PromptHistory(path).entries == ["saved"]
