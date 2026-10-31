from __future__ import annotations

from pathlib import Path

from project_sandbox_tui.review import SeenFiles


def test_marks_persist_per_bookmark(tmp_path: Path) -> None:
    seen = SeenFiles(tmp_path / "state" / "review.json")
    seen.mark("feature/x", ["k1", "k2"], True)
    seen.mark("@", ["k1"], True)
    seen.mark("feature/x", ["k1"], False)
    assert seen.seen("feature/x") == {"k2"}
    reloaded = SeenFiles(tmp_path / "state" / "review.json")
    assert reloaded.seen("feature/x") == {"k2"} and reloaded.seen("@") == {"k1"}
    assert reloaded.seen("other") == set()
    seen.mark("@", ["k1"], False)
    assert "@" not in SeenFiles(seen.path).marks  # empty bookmarks are dropped


def test_marks_cap_drops_oldest(tmp_path: Path) -> None:
    seen = SeenFiles(tmp_path / "review.json", max_marks=3)
    seen.mark("b", ["1", "2", "3"], True)
    seen.mark("b", ["4", "1"], True)  # 1 marked again: newest
    assert seen.marks["b"] == ["3", "4", "1"]


def test_instances_merge_instead_of_overwriting(tmp_path: Path) -> None:
    a = SeenFiles(tmp_path / "review.json")
    b = SeenFiles(tmp_path / "review.json")  # second TUI on the same repo
    a.mark("x", ["1"], True)
    b.mark("x", ["2"], True)
    assert SeenFiles(tmp_path / "review.json").seen("x") == {"1", "2"}


def test_corrupt_file_and_failed_save(tmp_path: Path) -> None:
    path = tmp_path / "review.json"
    path.write_text("{not json")
    assert SeenFiles(path).marks == {}
    path.write_text('{"bookmarks": {"x": ["1", 2], "y": "bad"}}')
    assert SeenFiles(path).marks == {"x": ["1"]}
    blocked = SeenFiles(tmp_path / "file" / "review.json")
    (tmp_path / "file").write_text("not a directory")
    try:
        blocked.mark("x", ["1"], True)
    except OSError:
        pass
    else:
        raise AssertionError("expected OSError")
    assert blocked.seen("x") == {"1"}  # still applies for this session
