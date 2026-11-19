"""Project detection and local agent guidance for ``sbx-tui --init``."""

import contextlib
import os
import stat
from collections.abc import Generator
from importlib.resources import files
from pathlib import Path
from typing import Literal, TextIO

from project_sandbox_tui.config import (
    CONFIG_SKILL_NAME,
    CONFIG_SKILL_TARGETS,
    PROJECT_TEMPLATE,
)
from project_sandbox_tui.feedback_transport import SKILL_NAME as FEEDBACK_SKILL

SKILL_NAME = CONFIG_SKILL_NAME
SKILL_DIRS = tuple(Path(target) for target in CONFIG_SKILL_TARGETS)
FEEDBACK_DIRS = tuple(Path(root) / "skills" / FEEDBACK_SKILL for root in (".agents", ".claude"))
GUIDANCE_START = "<!-- sbx-tui feedback:start -->"
GUIDANCE_END = "<!-- sbx-tui feedback:end -->"
GUIDANCE = f"""{GUIDANCE_START}
When launched with sbx feedback context, follow the `sbx-run-feedback` skill and
publish progress and a final task outcome. In a sandbox, read
`/run/sbx-feedback/skill/SKILL.md` and `/run/sbx-feedback/context.json`.
For host runs, use the skill and context in `$SBX_FEEDBACK_DIR`.
Without reporting context, no feedback report is required.
{GUIDANCE_END}"""


def project_template(repo: Path) -> str:
    """Select a starter image from root-level files, with deterministic precedence."""
    # Empty values override any incompatible image settings in the global config.
    image = dict.fromkeys(
        ("base_image", "dockerfile", "docker_context", "build", "build_version"), ""
    )
    if (repo / "Dockerfile").is_file():
        image.update(dockerfile="Dockerfile", docker_context=".")
    elif any((repo / name).is_file() for name in ("pyproject.toml", "uv.lock")):
        image["build"] = "python-uv"
    elif (repo / "Cargo.toml").is_file():
        image["build"] = "rust-cargo"
    elif (repo / "package.json").is_file():
        image["build"] = "node-npm"
    else:
        image["base_image"] = "python:3.12-slim"
    source = "# Empty values clear inherited global image settings.\n"
    source += "\n".join(f'{key} = "{value}"' for key, value in image.items())
    return PROJECT_TEMPLATE.replace('base_image = "python:3.12-slim"', source, 1)


def skill_template() -> str:
    return (
        files("project_sandbox_tui")
        .joinpath("skills", SKILL_NAME, "SKILL.md")
        .read_text(encoding="utf-8")
    )


@contextlib.contextmanager
def _local_file(repo: Path, relative: Path, mode: Literal["a", "r", "x"]) -> Generator[TextIO]:
    """Open a guidance file beneath the workspace, without traversing symlinks."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    with contextlib.ExitStack() as stack:
        parent = os.open(repo, flags)
        stack.callback(os.close, parent)
        for component in relative.parts[:-1]:
            if mode == "x":
                with contextlib.suppress(FileExistsError):
                    os.mkdir(component, dir_fd=parent)
            parent = os.open(component, flags, dir_fd=parent)
            stack.callback(os.close, parent)

        def opener(name: str, flags: int) -> int:
            return os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o666, dir_fd=parent)

        with open(relative.name, mode, encoding="utf-8", opener=opener) as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError(f"{repo / relative} must be a regular file")
            yield stream


def _existing(repo: Path, relative: Path) -> str:
    try:
        with _local_file(repo, relative, "r") as stream:
            return stream.read()
    except FileNotFoundError:
        return ""


def _install_skill(repo: Path, directory: Path, template: str) -> str:
    relative = directory / "SKILL.md"
    try:
        with _local_file(repo, relative, "x") as stream:
            stream.write(template)
    except FileExistsError:
        if _existing(repo, relative) != template:
            return (
                f"preserved customized or older skill: {repo / relative}; "
                "remove it and rerun --init to refresh"
            )
        return f"exists: {repo / relative}"
    return f"wrote: {repo / relative}"


def ignore_skills(repo: Path) -> bool:
    """Append only our skill directories, preserving existing ignore rules."""
    path = Path(".gitignore")
    existing = _existing(repo, path)
    patterns = [f"/{directory.as_posix()}/" for directory in (*SKILL_DIRS, *FEEDBACK_DIRS)]
    missing = [pattern for pattern in patterns if pattern not in existing.splitlines()]
    if not missing:
        return False
    with _local_file(repo, path, "a") as stream:
        if existing and not existing.endswith("\n"):
            stream.write("\n")
        stream.write("\n# sbx-tui — local configuration skill\n")
        stream.write("\n".join(missing) + "\n")
    return True


def install_feedback(repo: Path, *, guidance: bool = True, config_skill: bool = False) -> list[str]:
    """Install missing resources while preserving customized guidance across launches."""
    messages = []
    template = (
        files("project_sandbox_tui").joinpath("skills", FEEDBACK_SKILL, "SKILL.md").read_text()
    )
    updated = ignore_skills(repo)
    messages.append(f"{'updated' if updated else 'exists'}: {repo / '.gitignore'}")
    if config_skill:
        config_template = skill_template()
        messages.extend(
            _install_skill(repo, directory, config_template) for directory in SKILL_DIRS
        )
    messages.extend(_install_skill(repo, directory, template) for directory in FEEDBACK_DIRS)
    if not guidance:
        return messages
    path = repo / "AGENTS.md"
    existing = _existing(repo, Path("AGENTS.md"))
    if GUIDANCE_START in existing or GUIDANCE_END in existing:
        if GUIDANCE not in existing:
            messages.append(
                f"preserved customized or incomplete feedback block: {path}; reconcile it manually"
            )
        else:
            messages.append(f"exists: feedback guidance in {path}")
    else:
        with _local_file(repo, Path("AGENTS.md"), "a") as stream:
            stream.write(("\n\n" if existing else "") + GUIDANCE + "\n")
        messages.append(f"updated: {path}")
    return messages
