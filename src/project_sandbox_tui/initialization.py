"""Project detection and local agent guidance for ``sbx-tui --init``."""

from importlib.resources import files
from pathlib import Path

from project_sandbox_tui.config import PROJECT_TEMPLATE
from project_sandbox_tui.feedback_transport import SKILL_NAME as FEEDBACK_SKILL

SKILL_NAME = "sbx-tui-config"
SKILL_DIRS = tuple(Path(root) / "skills" / SKILL_NAME for root in (".agents", ".claude"))
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


def ignore_skills(repo: Path) -> bool:
    """Append only our skill directories, preserving existing ignore rules."""
    path = repo / ".gitignore"
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    patterns = [f"/{directory.as_posix()}/" for directory in (*SKILL_DIRS, *FEEDBACK_DIRS)]
    missing = [pattern for pattern in patterns if pattern not in existing.splitlines()]
    if not missing:
        return False
    with path.open("a", encoding="utf-8") as stream:
        if existing and not existing.endswith("\n"):
            stream.write("\n")
        stream.write("\n# sbx-tui — local configuration skill\n")
        stream.write("\n".join(missing) + "\n")
    return True


def install_feedback(repo: Path, *, guidance: bool = True) -> list[str]:
    """Install missing resources; never overwrite a customized skill or block.

    Like the configuration skill, updates require removing the old skill first.
    Drift is reported so an intentional edit cannot silently disappear.
    """
    from project_sandbox_tui.config import write_template

    messages = []
    template = (
        files("project_sandbox_tui").joinpath("skills", FEEDBACK_SKILL, "SKILL.md").read_text()
    )
    ignore_skills(repo)
    for directory in FEEDBACK_DIRS:
        path = repo / directory / "SKILL.md"
        written = write_template(path, template)
        if not written and path.read_text() != template:
            messages.append(
                f"preserved customized or older skill: {path}; "
                "remove it and rerun --init to refresh"
            )
        else:
            messages.append(f"{'wrote' if written else 'exists'}: {path}")
    if not guidance:
        return messages
    path = repo / "AGENTS.md"
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    if GUIDANCE_START in existing or GUIDANCE_END in existing:
        if GUIDANCE not in existing:
            messages.append(
                f"preserved customized or incomplete feedback block: {path}; reconcile it manually"
            )
        else:
            messages.append(f"exists: feedback guidance in {path}")
    else:
        path.write_text(existing + ("\n\n" if existing else "") + GUIDANCE + "\n", encoding="utf-8")
        messages.append(f"updated: {path}")
    return messages
