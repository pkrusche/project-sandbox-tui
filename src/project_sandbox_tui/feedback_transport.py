"""Stage run-local feedback resources and construct sandbox mounts."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

from project_sandbox_tui import feedback
from project_sandbox_tui.config import private_dir

SKILL_NAME = "sbx-run-feedback"
ROOT = feedback.SANDBOX_ROOT


def supported(command: list[str], *, cwd: Path | None = None) -> bool:
    """Probe an actual CLI option rather than guessing a minimum release."""
    try:
        result = subprocess.run(
            [*command, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return (
        result.returncode == 0 and re.search(r"(?<!\S)--mount(?:\s|=)", result.stdout) is not None
    )


def prepare(
    root: Path,
    run_id: str,
    task_id: str = "",
    previous_state: dict[str, Any] | None = None,
    *,
    interactive: bool = False,
    planning_prompt: str = "",
    blocked_guidance: str = "",
) -> None:
    private_dir(root.parent)
    private_dir(root)
    private_dir(root / "output")
    (root / "skill").mkdir()
    package = files("project_sandbox_tui")
    (root / "sbx-report").write_text(package.joinpath("feedback.py").read_text(), encoding="utf-8")
    (root / "sbx-report").chmod(0o755)
    (root / "skill" / "SKILL.md").write_text(
        package.joinpath("skills", SKILL_NAME, "SKILL.md").read_text(), encoding="utf-8"
    )
    context = {
        "schema_version": feedback.VERSION,
        "run_id": run_id,
        "task_id": task_id,
        "previous_state": previous_state or {},
        "interactive": interactive,
    }
    if planning_prompt:
        context["planning_prompt"] = planning_prompt
    if blocked_guidance:
        # Leave room for JSON escaping and the rest of the context. The TUI keeps the full report.
        context["blocked_guidance"] = blocked_guidance[:8000]
    feedback.atomic_write(root / "context.json", context)


def mounts(root: Path) -> list[str]:
    source = str(root.resolve())
    if any(c in source for c in (",", "\n", "\0")):
        raise ValueError("feedback mount paths must not contain commas, newlines, or NUL")
    return [
        f"--mount=type=bind,source={source},target={ROOT},readonly",
        f"--mount=type=bind,source={source}/output,target={ROOT}/output",
        f"--mount=type=bind,source={source}/skill,target=/home/agent/.agents/skills/{SKILL_NAME},readonly",
        f"--mount=type=bind,source={source}/skill,target=/home/agent/.claude/skills/{SKILL_NAME},readonly",
    ]


def instruction() -> str:
    return (
        f"\n\nRun reporting: read {ROOT}/skill/SKILL.md and {ROOT}/context.json. "
        f"Use python3 {ROOT}/sbx-report to publish progress and a final task outcome."
    )


def host_argv(root: Path, argv: list[str]) -> list[str]:
    # env applies only to this launch, including launches in external terminals.
    return [
        "env",
        f"SBX_FEEDBACK_DIR={root}",
        f"PATH={root}{os.pathsep}{os.environ.get('PATH', '')}",
        sys.executable,
        str(files("project_sandbox_tui").joinpath("feedback_host.py")),
        str(root),
        *argv,
    ]
