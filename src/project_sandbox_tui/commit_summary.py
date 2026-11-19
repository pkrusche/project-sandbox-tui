"""Prepare the patches pinned by a commit review."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from project_sandbox_tui.vcs import JjBackend, ReviewCommit


def stage_patches(backend: JjBackend, commits: Sequence[ReviewCommit], directory: Path) -> None:
    directory.mkdir()
    manifest = []
    for number, commit in enumerate(commits, start=1):
        patch = f"{number}.patch"
        (directory / patch).write_bytes(backend.review_commit_patch(commit))
        manifest.append({"commit_id": commit.commit, "subject": commit.subject, "patch": patch})
    (directory / "commits.json").write_text(json.dumps(manifest), encoding="utf-8")
