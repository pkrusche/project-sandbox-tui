"""Prompt history for unsupervised runs (and ``:`` command lines), kept per repository.

Stored as JSON (prompts are often multi-line) in the repo's state directory,
oldest first. Every write re-reads the file under a lock so two TUI instances
on the same repository merge their histories instead of overwriting each other.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
from collections.abc import Generator
from pathlib import Path
from typing import Any

from project_sandbox_tui.config import (
    STATE_VERSION,
    private_dir,
    read_state_for_update,
    repo_state_dir,
    write_state,
)

MAX_ENTRIES = 500


class PromptHistory:
    def __init__(self, path: Path, max_entries: int = MAX_ENTRIES) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self.path = path
        self.max_entries = max_entries
        self.entries: list[str] = self._read()  # oldest → newest

    @classmethod
    def for_repo(cls, repo: Path, name: str = "prompt_history.json") -> PromptHistory:
        return cls(repo_state_dir(repo) / name)

    # ------------------------------------------------------------ storage

    def _read(self) -> list[str]:
        try:
            return self._parse(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return []

    @staticmethod
    def _parse(raw: Any) -> list[str]:
        prompts = raw.get("prompts") if isinstance(raw, dict) else None
        if not isinstance(prompts, list):
            return []
        return [p for p in prompts if isinstance(p, str) and p.strip()]

    @contextlib.contextmanager
    def _locked(self) -> Generator[None, None, None]:
        private_dir(self.path.parent)
        with self.path.with_name(self.path.name + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def add(self, prompt: str) -> None:
        """Record ``prompt`` as the newest entry (moving an existing duplicate)."""
        text = prompt.strip()
        if not text:
            return
        try:
            with self._locked():
                # Strict: an unreadable file must not be replaced by this prompt alone.
                stored = self._parse(read_state_for_update(self.path))
                entries = [e for e in stored if e.strip() != text]
                entries.append(text)
                entries = entries[-self.max_entries :]
                write_state(self.path, {"version": STATE_VERSION, "prompts": entries})
        except OSError:
            return  # history is a convenience, never fatal
        self.entries = entries

    # ------------------------------------------------------------ queries

    def newest_first(self) -> list[str]:
        return list(reversed(self.entries))

    def suggest(self, typed: str) -> str | None:
        """Most recent entry that extends ``typed`` (fish-style autosuggestion)."""
        if not typed.strip():
            return None
        for entry in reversed(self.entries):
            if entry.startswith(typed) and entry != typed:
                return entry
        return None

    def with_prefix(self, prefix: str) -> list[str]:
        """Entries starting with ``prefix``, newest first (for ctrl+p / ctrl+n)."""
        return [e for e in reversed(self.entries) if e.startswith(prefix)]

    def search(self, query: str) -> list[str]:
        """Entries containing every word of ``query`` (case-insensitive), newest first."""
        words = query.lower().split()
        return [e for e in reversed(self.entries) if all(w in e.lower() for w in words)]
