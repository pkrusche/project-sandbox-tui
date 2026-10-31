"""Diff review: which file changes have been seen, kept per bookmark.

A mark is a file change's key (both sides' content, type and path), so a file
stays seen while later runs change only other files, and shows up again once
its own diff changes. Stored as JSON in the repo's state directory; every
write re-reads the file under a lock so TUI instances merge their marks.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import uuid
from collections.abc import Collection, Generator
from pathlib import Path

from project_sandbox_tui.config import private_dir, repo_state_dir

MAX_MARKS = 5000  # per bookmark; the oldest go first


class SeenFiles:
    def __init__(self, path: Path, max_marks: int = MAX_MARKS) -> None:
        self.path = path
        self.max_marks = max_marks
        self.marks: dict[str, list[str]] = self._read()  # bookmark → keys, oldest first

    @classmethod
    def for_repo(cls, repo: Path) -> SeenFiles:
        return cls(repo_state_dir(repo) / "diff_review.json")

    def _read(self) -> dict[str, list[str]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        bookmarks = raw.get("bookmarks") if isinstance(raw, dict) else None
        if not isinstance(bookmarks, dict):
            return {}
        return {
            name: [k for k in keys if isinstance(k, str)]
            for name, keys in bookmarks.items()
            if isinstance(name, str) and isinstance(keys, list)
        }

    @contextlib.contextmanager
    def _locked(self) -> Generator[None, None, None]:
        private_dir(self.path.parent)
        with self.path.with_name(self.path.name + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def seen(self, bookmark: str) -> set[str]:
        return set(self.marks.get(bookmark, ()))

    def mark(self, bookmark: str, keys: Collection[str], seen: bool) -> None:
        """Mark (or unmark) file changes on ``bookmark`` as seen.

        Raises OSError if the marks cannot be saved; they still apply in memory.
        """
        try:
            with self._locked():
                marks = self._read()
                self._update(marks, bookmark, keys, seen)
                tmp = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.tmp")
                try:
                    tmp.write_text(
                        json.dumps({"version": 1, "bookmarks": marks}, indent=1), encoding="utf-8"
                    )
                    tmp.replace(self.path)
                finally:
                    with contextlib.suppress(OSError):
                        tmp.unlink(missing_ok=True)
        except OSError:
            self._update(self.marks, bookmark, keys, seen)
            raise
        self.marks = marks

    def _update(
        self, marks: dict[str, list[str]], bookmark: str, keys: Collection[str], seen: bool
    ) -> None:
        changed = dict.fromkeys(keys)  # ordered, fast membership
        kept = [k for k in marks.get(bookmark, []) if k not in changed]
        if seen:
            kept.extend(changed)
        if kept:
            marks[bookmark] = kept[-self.max_marks :]
        else:
            marks.pop(bookmark, None)
