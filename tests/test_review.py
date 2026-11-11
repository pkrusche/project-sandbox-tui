from __future__ import annotations

import json
from pathlib import Path

from project_sandbox_tui.review import SeenFiles


def test_marks_persist_across_views(tmp_path: Path) -> None:
    seen = SeenFiles(tmp_path / "state" / "review.json")
    seen.mark(["k1", "k2"], True)
    seen.mark(["k1"], False)
    assert seen.seen() == {"k2"}
    reloaded = SeenFiles(seen.path)
    assert reloaded.seen() == {"k2"}
    reloaded.mark(["k2"], False)
    assert SeenFiles(seen.path).seen() == set()


def test_legacy_bookmark_marks_are_shared_and_migrated(tmp_path: Path) -> None:
    path = tmp_path / "review.json"
    path.write_text(json.dumps({"bookmarks": {"feature/x": ["k1", "k2"], "@": ["k1", "k3"]}}))
    seen = SeenFiles(path)
    assert seen.seen() == {"k1", "k2", "k3"}
    seen.mark(["k1"], False)
    assert SeenFiles(path).seen() == {"k2", "k3"}
    assert "bookmarks" not in json.loads(path.read_text())


def test_marks_cap_drops_oldest(tmp_path: Path) -> None:
    seen = SeenFiles(tmp_path / "review.json", max_marks=3)
    seen.mark(["1", "2", "3"], True)
    seen.mark(["4", "1"], True)  # 1 marked again: newest
    assert seen.marks == ["3", "4", "1"]


def test_instances_merge_instead_of_overwriting(tmp_path: Path) -> None:
    a = SeenFiles(tmp_path / "review.json")
    b = SeenFiles(tmp_path / "review.json")  # second TUI on the same repo
    a.mark(["1"], True)
    b.mark(["2"], True)
    assert SeenFiles(tmp_path / "review.json").seen() == {"1", "2"}
    a.mark(["2"], False)  # re-read shared marks before removing another view's mark
    assert SeenFiles(tmp_path / "review.json").seen() == {"1"}


def test_corrupt_file_and_failed_save(tmp_path: Path) -> None:
    path = tmp_path / "review.json"
    path.write_text("{not json")
    assert SeenFiles(path).marks == []
    path.write_text('{"bookmarks": {"x": ["1", 2], "y": "bad"}}')
    assert SeenFiles(path).marks == ["1"]
    path.write_text('{"marks": ["1", 2, "1"]}')
    assert SeenFiles(path).marks == ["1"]
    blocked = SeenFiles(tmp_path / "file" / "review.json")
    (tmp_path / "file").write_text("not a directory")
    try:
        blocked.mark(["1"], True)
    except OSError:
        pass
    else:
        raise AssertionError("expected OSError")
    assert blocked.seen() == {"1"}  # still applies for this session


def test_corrupt_marks_are_kept_aside_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "review.json"
    path.write_text("{truncated")
    SeenFiles(path).mark(["1"], True)
    [aside] = tmp_path.glob("review.json.corrupt-*")
    assert aside.read_text() == "{truncated"
    assert SeenFiles(path).seen() == {"1"}
