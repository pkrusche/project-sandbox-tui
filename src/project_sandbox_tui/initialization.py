"""Project detection and local agent guidance for ``sbx-tui --init``."""

from importlib.resources import files
from pathlib import Path

from project_sandbox_tui.config import PROJECT_TEMPLATE

SKILL_NAME = "sbx-tui-config"
SKILL_DIRS = tuple(Path(root) / "skills" / SKILL_NAME for root in (".agents", ".claude"))


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


def ignore_skills(repo: Path) -> bool:
    """Append only our skill directories, preserving existing ignore rules."""
    path = repo / ".gitignore"
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    patterns = [f"/{directory.as_posix()}/" for directory in SKILL_DIRS]
    missing = [pattern for pattern in patterns if pattern not in existing.splitlines()]
    if not missing:
        return False
    with path.open("a", encoding="utf-8") as stream:
        if existing and not existing.endswith("\n"):
            stream.write("\n")
        stream.write("\n# sbx-tui — local configuration skill\n")
        stream.write("\n".join(missing) + "\n")
    return True
