"""Project detection and local agent guidance for ``sbx-tui --init`` and ``:init``."""

import contextlib
import errno
import os
import stat
from collections.abc import Generator
from importlib.resources import files
from pathlib import Path
from typing import Literal, TextIO

from project_sandbox_tui import config as config_mod
from project_sandbox_tui.config import (
    CONFIG_SKILLS,
    PROJECT_TEMPLATE,
    SKILL_ROOTS,
    repo_state_dir,
    state_lock,
)
from project_sandbox_tui.feedback_transport import SKILL_NAME as FEEDBACK_SKILL

FEEDBACK_DIRS = tuple(Path(root) / FEEDBACK_SKILL for root in SKILL_ROOTS)
GUIDANCE_START = "<!-- sbx-tui feedback:start -->"
GUIDANCE_END = "<!-- sbx-tui feedback:end -->"
GUIDANCE = f"""{GUIDANCE_START}
When launched with sbx feedback context, follow the `sbx-run-feedback` skill and
publish progress and a final task outcome. In a sandbox, read
`/run/sbx-feedback/skill/SKILL.md` and `/run/sbx-feedback/context.json`.
For host runs, use the skill and context in `$SBX_FEEDBACK_DIR`.
Without reporting context, no feedback report is required.
{GUIDANCE_END}"""


def initialize(repo: Path, global_path: Path | None = None) -> Generator[str]:
    """Write missing configuration and guidance, trusting only our new template."""
    for path, template in (
        (global_path or config_mod.global_config_path(), config_mod.GLOBAL_TEMPLATE),
        (config_mod.project_config_path(repo), project_template(repo)),
    ):
        try:
            with config_mod.config_lock():
                written = config_mod.write_template(path, template)
                if written and path == config_mod.project_config_path(repo):
                    config_mod.write_trusted_config(repo, template)
        except (OSError, config_mod.ConfigError) as exc:
            raise config_mod.ConfigError(f"could not write {path}: {exc}") from exc
        yield f"{'wrote' if written else 'exists'}: {path}"
    try:
        yield from install_feedback(repo, config_skill=True)
    except (OSError, ValueError) as exc:
        raise config_mod.ConfigError(f"could not install configuration skill: {exc}") from exc


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


def skill_template(name: str) -> str:
    return (
        files("project_sandbox_tui")
        .joinpath("skills", name, "SKILL.md")
        .read_text(encoding="utf-8")
    )


@contextlib.contextmanager
def _local_file(repo: Path, relative: Path, mode: Literal["a", "r", "x"]) -> Generator[TextIO]:
    """Open a guidance file beneath the workspace, without traversing symlinks."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    with contextlib.ExitStack() as stack:
        parent = os.open(repo, flags)
        stack.callback(os.close, parent)
        for index, component in enumerate(relative.parts[:-1], start=1):
            try:
                if mode == "x":
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(component, dir_fd=parent)
                parent = os.open(component, flags, dir_fd=parent)
            except OSError as exc:
                path = repo.joinpath(*relative.parts[:index])
                raise OSError(exc.errno, exc.strerror, str(path)) from exc
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
    patterns = [f"/{target}/" for targets in CONFIG_SKILLS.values() for target in targets]
    patterns.extend(f"/{directory.as_posix()}/" for directory in FEEDBACK_DIRS)
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
    with state_lock(repo_state_dir(repo) / "guidance.lock"):
        return _install(repo, guidance=guidance, config_skill=config_skill)


def _install(repo: Path, *, guidance: bool, config_skill: bool) -> list[str]:
    names = (*CONFIG_SKILLS, FEEDBACK_SKILL) if config_skill else (FEEDBACK_SKILL,)
    templates = {name: skill_template(name) for name in names}
    updated = ignore_skills(repo)
    messages = [f"{'updated' if updated else 'exists'}: {repo / '.gitignore'}"]
    skipped = []
    for root in SKILL_ROOTS:
        try:
            for name, template in templates.items():
                messages.append(_install_skill(repo, Path(root) / name, template))
        except OSError as exc:
            path = repo / root
            if exc.errno not in (errno.ENOTDIR, errno.ELOOP) or exc.filename != str(path):
                raise
            skipped.append(str(path))
            messages.append(f"skipped: {path} (expected a directory without symlinks)")
    # Host launches stage a copy of the feedback skill with each run.
    if config_skill and len(skipped) == len(SKILL_ROOTS):
        paths = ", ".join(skipped)
        raise ValueError(f"expected a directory without symlinks at one of: {paths}")
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
