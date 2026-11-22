"""The TOML examples in the configuration docs load as documented."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from project_sandbox_tui import config as c

DOCS = Path(__file__).resolve().parents[1] / "docs"


def section(doc: str, heading: str) -> str:
    """The text under ``heading`` up to the next heading of the same or a higher level."""
    level = len(heading.split(" ", 1)[0])
    lines = (DOCS / doc).read_text().splitlines(keepends=True)
    start = lines.index(f"{heading}\n")
    out, in_code = [], False
    for line in lines[start + 1 :]:
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code and (match := re.match(r"(#+) ", line)) and len(match[1]) <= level:
            break
        out.append(line)
    return "".join(out)


def toml_blocks(text: str) -> list[str]:
    return re.findall(r"```toml\n(.*?)```", text, re.S)


def load_pair(tmp_path: Path, global_text: str, project_text: str) -> c.Config:
    tmp_path.mkdir(parents=True, exist_ok=True)
    global_path = tmp_path / "config.toml"
    global_path.write_text(global_text)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / c.PROJECT_CONFIG_NAME).write_text(project_text)
    c.ProjectTrust.check(repo).trust(repo)
    return c.load(repo, global_path)


def test_catalogue_and_personas_example(tmp_path: Path) -> None:
    (block,) = toml_blocks(section("configuration.md", "## Model catalogue and personas"))
    cfg = load_pair(tmp_path, block, "")
    assert cfg.catalogue.default_provider == "claude-code"
    assert cfg.catalogue.models["gateway-anthropic"] == {"high": "claude-opus-5", "frontier": ""}
    assert cfg.personas["reviewer"].tier == "frontier"
    assert cfg.personas["triage"].level == 3


def test_security_levels_example(tmp_path: Path) -> None:
    (block,) = toml_blocks(section("configuration.md", "## Security levels"))
    project, global_text = block.split("# ~/.config/project-sandbox-tui/config.toml")
    cfg = load_pair(tmp_path, global_text, project)
    assert (cfg.security.level, cfg.security.egress) == (3, "sidecar")
    assert cfg.network.build_proxy == "http://proxy.example:3128"
    with pytest.raises(c.ConfigError, match="network.internet_proxy"):
        load_pair(
            tmp_path / "swapped", "", project + global_text.replace("[security]\negress", "[x]\ny")
        )


def test_commit_review_example(tmp_path: Path) -> None:
    (block,) = toml_blocks(section("configuration.md", "## Commit review"))
    assert load_pair(tmp_path, block, "").commit_review.provider == "claude-code"


def test_agent_action_examples(tmp_path: Path) -> None:
    blocks = toml_blocks(section("configuration.md", "## Agent actions"))
    profiles = '[profiles."Deep review"]\nagent = "codex"\n'
    for index, block in enumerate(blocks):
        load_pair(tmp_path / str(index), profiles + block, "")


def test_settings_reference_example(tmp_path: Path) -> None:
    (block,) = toml_blocks(section("configuration.md", "## Settings reference"))
    data = tomllib.loads(block)
    data["security"].pop("egress")  # global-only: the reference shows it for completeness
    cfg = c.from_dict(data)
    cfg.validate()
    assert cfg.actions["review"].persona == "reviewer"


def test_workflow_example_with_project_actions(tmp_path: Path) -> None:
    (block,) = toml_blocks(section("feedback.md", "## Project workflows"))
    cfg = load_pair(tmp_path, "", block)
    workflow = cfg.workflows["spec"]
    steps = [workflow.planning_action, *workflow.steps]
    assert steps == ["spec-plan", "spec-check", "spec-implement", "spec-chores"]
    assert [cfg.actions[s].persona for s in steps] == [
        "planner",
        "checker",
        "implementer",
        "chore-runner",
    ]
    assert workflow.pass_state_keys == ["spec_path"] and not workflow.result_state_keys


def test_project_workflow_matches_documented_example() -> None:
    project = DOCS.parent / c.PROJECT_CONFIG_NAME
    if (DOCS.parent / "PKG-INFO").is_file() and not project.exists():
        pytest.skip("checkout-local configuration is excluded from the source distribution")
    (block,) = toml_blocks(section("feedback.md", "## Project workflows"))
    example = tomllib.loads(block)
    cfg = c.from_dict(tomllib.loads(project.read_text()))
    cfg.validate()
    assert cfg.workflows["spec"] == c.from_dict(example).workflows["spec"]
    for name, action in example["actions"].items():
        assert cfg.actions[name].persona == action["persona"]
    assert "spec-commits" not in cfg.actions
