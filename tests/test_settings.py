from __future__ import annotations

from pathlib import Path

import pytest

from project_sandbox_tui import config
from project_sandbox_tui import settings as s
from project_sandbox_tui.cli import main


def cli(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


@pytest.mark.parametrize(
    ("key", "text", "expected"),
    [
        ("sandbox.cpus", "6", 6),
        ("ui.refresh_seconds", "1.5", 1.5),
        ("ui.bell", "false", False),
        ("ui.bell", "true", True),
        ("defaults.fast", "true", True),
        ("defaults.fast", "false", False),
        ("project.base_image", "[x]", "[x]"),
        ("models.codex", '["a", "b"]', ["a", "b"]),
        ("models.codex", "[]", []),
        ("commands.test", "uv run pytest", "uv run pytest"),
        ("leader.t", "test", "test"),
    ],
)
def test_parse_key_and_value(key: str, text: str, expected: object) -> None:
    parsed = s.parse_key(key)
    assert str(parsed) == key
    value = s.parse_value(parsed, text)
    assert value == expected and type(value) is type(expected)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("ui.bell", "yes", "true or false"),
        ("sandbox.cpus", "many", "an integer"),
        ("ui.refresh_seconds", "soon", "a number"),
        ("network.extra_domains", "a.com", "TOML list"),
        ("network.extra_domains", "[1]", "TOML list"),
    ],
)
def test_parse_invalid_value(key: str, value: str, message: str) -> None:
    with pytest.raises(config.ConfigError, match=message):
        s.parse_value(s.parse_key(key), value)


@pytest.mark.parametrize("key", ["nope.x", "ui.nope", "models.GPT", "ui", "commands", "leader."])
def test_parse_invalid_key(key: str) -> None:
    with pytest.raises(config.ConfigError, match="unknown key"):
        s.parse_key(key)


def test_set_get_unset_global(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = config.global_config_path()
    path.parent.mkdir(parents=True)
    path.write_text("# my settings\n[ui]\nbell = true  # ring\n")
    assert cli(capsys, "config", "set", "--global", "ui.bell", "false", "-R", str(repo))[0] == 0
    assert path.read_text() == "# my settings\n[ui]\nbell = false  # ring\n"
    assert cli(capsys, "config", "get", "ui.bell", "-R", str(repo))[1] == "false\n"
    assert cli(capsys, "config", "set", "--global", "defaults.agent", "codex")[0] == 0
    assert cli(capsys, "config", "get", "defaults.agent")[1] == "codex\n"
    assert cli(capsys, "config", "unset", "--global", "defaults.agent")[0] == 0
    assert "[defaults]" not in path.read_text()  # emptied tables go
    code, _, err = cli(capsys, "config", "unset", "--global", "defaults.agent")
    assert code == 1 and "is not set" in err
    assert cli(capsys, "config", "path", "--global")[1] == f"{path}\n"


def test_invalid_changes_are_not_written(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = repo / config.PROJECT_CONFIG_NAME
    assert (
        cli(capsys, "config", "set", "--project", "project.build", "python-uv", "-R", str(repo))[0]
        == 0
    )
    before = project.read_text()
    code, _, err = cli(
        capsys, "config", "set", "--project", "project.base_image", "img", "-R", str(repo)
    )
    assert code == 2 and "only one of" in err
    # Validated together with the other file: a global image conflicts too.
    code, _, err = cli(
        capsys, "config", "set", "--global", "project.base_image", "img", "-R", str(repo)
    )
    assert code == 2 and "only one of" in err
    assert project.read_text() == before
    assert not config.global_config_path().exists()
    code, _, err = cli(capsys, "config", "set", "defaults.agent", "bash")
    assert code == 2 and "needs --global or --project" in err


def test_project_edits_keep_trust(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = repo / config.PROJECT_CONFIG_NAME
    r = ("-R", str(repo))
    assert cli(capsys, "config", "set", "--project", "project.build", "python-uv", *r)[0] == 0
    assert not config.ProjectTrust.check(repo).needs_review  # created here: trusted
    assert cli(capsys, "config", "set", "--project", "defaults.effort", "high", *r)[0] == 0
    assert config.load(repo).defaults.effort == "high"  # stays trusted
    code, out, _ = cli(capsys, "config", "list", "--project", *r)
    assert out == 'project.build = "python-uv"\ndefaults.effort = "high"\n'

    project.write_text(project.read_text() + '[sandbox]\ncommand = "evil"\n')  # someone else
    code, _, err = cli(capsys, "config", "set", "--project", "defaults.effort", "low", *r)
    assert code == 0 and "not trusted" in err
    assert config.ProjectTrust.check(repo).needs_review  # a CLI edit does not trust it
    code, out, err = cli(capsys, "config", "get", "defaults.effort", *r)
    assert out == "high\n" and "changed since it was trusted" in err
    code, out, _ = cli(capsys, "config", "list", *r)
    assert 'sandbox.command = "project-sandbox"' in out


def test_project_scope_needs_a_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = cli(capsys, "config", "list", "--project", "-R", str(tmp_path))
    assert code == 2 and "needs a jj repository" in err
    code, _, err = cli(capsys, "config", "list", "-R", str(tmp_path / "missing"))
    assert code == 2 and "no such directory" in err
    code, out, _ = cli(capsys, "config", "list", "-R", str(tmp_path))
    assert code == 0 and 'defaults.agent = "claude"' in out


def test_unset_revalidates_dependent_settings(repo: Path) -> None:
    editor = s.Editor("project", repo)
    editor.set(s.parse_key("project.build"), "python-uv")
    editor.set(s.parse_key("project.build_version"), "3.12")
    before = editor.path.read_bytes()
    with pytest.raises(config.ConfigError, match="build_version needs"):
        editor.unset(s.parse_key("project.build"))
    assert editor.path.read_bytes() == before
    assert not config.ProjectTrust.check(repo).needs_review


def test_failed_edit_preserves_file_and_trusted_copy(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    editor = s.Editor("project", repo)
    key = s.parse_key("defaults.agent")
    editor.set(key, "codex")
    before = editor.path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(config.ConfigError, match="cannot write"):
        editor.set(key, "bash")
    assert editor.path.read_bytes() == before
    assert config.load(repo).defaults.agent == "codex"
    assert not config.ProjectTrust.check(repo).needs_review
    assert list(repo.glob("*.tmp")) == []


def test_set_commands_and_leader_mappings(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = cli(capsys, "config", "set", "--global", "leader.t", "test")
    assert code == 2 and "unknown command :test" in err  # validated before writing
    assert not config.global_config_path().exists()
    assert cli(capsys, "config", "set", "--global", "commands.test", "uv run pytest")[0] == 0
    assert cli(capsys, "config", "set", "--global", "leader.t", "test")[0] == 0
    assert cli(capsys, "config", "get", "commands.test")[1] == "uv run pytest\n"
    listed = cli(capsys, "config", "list")[1]
    assert 'commands.test = "uv run pytest"' in listed and 'leader.t = "test"' in listed
    code, _, err = cli(capsys, "config", "unset", "--global", "commands.test")
    assert code == 2 and "unknown command :test" in err  # [leader] t still uses it


def test_config_set_reports_unwritable_parent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args, **kwargs):
        raise PermissionError("read-only configuration directory")

    monkeypatch.setattr(Path, "mkdir", fail)
    code, _, err = cli(
        capsys,
        "config",
        "set",
        "--global",
        "--config",
        str(tmp_path / "config.toml"),
        "defaults.agent",
        "codex",
        "-R",
        str(tmp_path),
    )
    assert code == 2 and "cannot write" in err


def test_project_edit_reports_unreadable_global_config(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = config.global_config_path()
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff")
    code, _, err = cli(
        capsys, "config", "set", "--project", "defaults.agent", "bash", "-R", str(repo)
    )
    assert code == 2 and "other config file" in err
    assert not config.project_config_path(repo).exists()


def test_set_actions(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = cli(capsys, "config", "set", "--global", "actions.review.agent", "codex")
    assert code == 2 and "needs a prompt" in err  # validated before writing
    code, _, err = cli(capsys, "config", "set", "--global", "actions.review.mode", "x")
    assert (
        code == 2
        and "actions.<name>.<prompt|agent|model|effort|timeout|profile|persona|level>" in err
    )
    prompt = "Review {bookmark}"
    assert cli(capsys, "config", "set", "--global", "actions.review.prompt", prompt)[0] == 0
    assert cli(capsys, "config", "set", "--global", "actions.review.timeout", "60")[0] == 0
    assert cli(capsys, "config", "set", "--global", "leader.rv", "review")[0] == 0
    text = config.global_config_path().read_text()
    assert "[actions.review]" in text and "[actions]\n" not in text
    assert cli(capsys, "config", "get", "actions.review.timeout")[1] == "60\n"
    assert cli(capsys, "config", "get", "actions.other.prompt")[0] == 1
    listed = cli(capsys, "config", "list")[1]
    assert 'actions.review.prompt = "Review {bookmark}"' in listed
    assert "actions.review.timeout = 60" in listed
    code, _, err = cli(capsys, "config", "unset", "--global", "actions.review.prompt")
    assert code == 2 and "needs a prompt" in err
    assert cli(capsys, "config", "unset", "--global", "leader.rv")[0] == 0
    assert cli(capsys, "config", "unset", "--global", "actions.review.timeout")[0] == 0
    assert cli(capsys, "config", "unset", "--global", "actions.review.prompt")[0] == 0
    assert "actions" not in config.global_config_path().read_text()


def test_set_action_profile(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    r = ("-R", str(repo))
    assert cli(capsys, "config", "set", "--project", "actions.review.prompt", "Review", *r)[0] == 0
    assert cli(capsys, "config", "set", "--project", "actions.review.profile", "deep", *r)[0] == 0
    assert 'profile = "deep"' in config.project_config_path(repo).read_text()
    listed = cli(capsys, "config", "list", "--project", *r)[1]
    assert 'actions.review.profile = "deep"' in listed
    code, _, err = cli(capsys, "config", "set", "--project", "actions.review.effort", "high", *r)
    assert code == 2 and "sets profile and effort" in err


def test_concurrent_edits_preserve_distinct_keys_and_trust(repo: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    def change(index: int) -> None:
        editor = s.Editor("project", repo)
        editor.set(s.parse_key(f"commands.test{index}"), f"echo {index}")

    expected = {**config.load(repo).commands, **{f"test{i}": f"echo {i}" for i in range(12)}}
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(change, range(12)))
    assert config.load(repo).commands == expected
    assert not config.ProjectTrust.check(repo).needs_review


@pytest.mark.parametrize("scope", ["project", "global"])
def test_edit_preserves_private_config_permissions(repo: Path, scope: config.ConfigScope) -> None:
    editor = s.Editor(scope, repo)
    editor.path.parent.mkdir(parents=True, exist_ok=True)
    editor.path.write_text("# private\n")
    editor.path.chmod(0o600)
    editor.set(s.parse_key("defaults.agent"), "codex")
    assert editor.path.stat().st_mode & 0o777 == 0o600


def test_set_catalogue_and_personas(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def set_global(key: str, value: str) -> tuple[int, str, str]:
        return cli(capsys, "config", "set", "--global", key, value)

    assert set_global("catalogue.default_provider", "claude-code")[0] == 0
    assert set_global("catalogue.gateway_agent", "opencode")[0] == 0
    assert set_global("catalogue.models.gateway-anthropic.high", "claude-opus-5")[0] == 0
    assert set_global("personas.reviewer.tier", "frontier")[0] == 0
    assert set_global("security.level", "3")[0] == 0
    text = config.global_config_path().read_text()
    assert "[catalogue.models.gateway-anthropic]" in text and "[catalogue.models]" not in text
    assert "[personas.reviewer]" in text and "[personas]\n" not in text
    assert cli(capsys, "config", "get", "catalogue.models.gateway-anthropic.high")[1] == (
        "claude-opus-5\n"
    )
    listed = cli(capsys, "config", "list")[1]
    assert 'catalogue.default_provider = "claude-code"' in listed
    assert 'catalogue.models.gateway-anthropic.high = "claude-opus-5"' in listed
    assert 'personas.reviewer.tier = "frontier"' in listed
    assert "personas.reviewer.level = 4" in listed  # built-in fields shown merged
    assert "personas.planner" not in listed  # only configured personas are listed
    assert "security.level = 3" in listed
    code, _, err = set_global("catalogue.models.gateway-mistral.high", "x")
    assert code == 2 and "catalogue.models.gateway-mistral" in err
    code, _, err = set_global("catalogue.gateway_agent", "codex")
    assert code == 2 and "pi, opencode" in err
    code, _, err = set_global("personas.reviewer.level", "6")
    assert code == 2 and "personas.reviewer.level" in err
    code, _, err = set_global("catalogue.models.codex.ultra", "x")
    assert code == 2 and "unknown key" in err
    code, _, err = set_global("personas.reviewer.mood", "x")
    assert code == 2 and "personas.<name>.<tier|effort|fast|level>" in err
    assert (
        cli(capsys, "config", "unset", "--global", "catalogue.models.gateway-anthropic.high")[0]
        == 0
    )
    assert "gateway-anthropic" not in config.global_config_path().read_text()


def test_set_action_persona_and_level(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    r = ("-R", str(repo))
    assert cli(capsys, "config", "set", "--project", "actions.review.prompt", "Review", *r)[0] == 0
    assert (
        cli(capsys, "config", "set", "--project", "actions.review.persona", "reviewer", *r)[0] == 0
    )
    assert cli(capsys, "config", "set", "--project", "actions.review.level", "5", *r)[0] == 0
    listed = cli(capsys, "config", "list", "--project", *r)[1]
    assert 'actions.review.persona = "reviewer"' in listed and "actions.review.level = 5" in listed
    code, _, err = cli(capsys, "config", "set", "--project", "actions.review.agent", "codex", *r)
    assert code == 2 and "sets persona and agent" in err
    code, _, err = cli(
        capsys, "config", "set", "--project", "actions.review.persona", "auditor", *r
    )
    assert code == 2 and "actions.review.persona" in err
    code, _, err = cli(capsys, "config", "set", "--project", "actions.review.level", "0", *r)
    assert code == 2 and "actions.review.level must be between 1 and 5" in err


def test_namespaced_workflow_settings_are_listed_read_only(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = config.workflow_dir(repo) / "spec.toml"
    path.parent.mkdir(parents=True)
    path.write_text("""
[workflow]
planning_action = "plan"
steps = ["host:test"]
[actions.plan]
prompt = "plan"
[commands]
test = "true"
""")
    config.WorkflowTrust.scan(repo)[0][0].trust()
    code, out, _ = cli(capsys, "config", "list", "-R", str(repo))
    assert code == 0 and "actions.spec/plan.prompt" in out and "commands.spec/test" in out
    code, _, err = cli(
        capsys, "config", "set", "--project", "-R", str(repo), "commands.spec/test", "false"
    )
    assert code == 2 and "read-only" in err
