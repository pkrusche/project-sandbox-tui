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


def test_parse_key_and_value() -> None:
    assert str(s.parse_key("sandbox.cpus")) == "sandbox.cpus"
    assert s.parse_value(s.parse_key("sandbox.cpus"), "6") == 6
    assert s.parse_value(s.parse_key("ui.refresh_seconds"), "1.5") == 1.5
    assert s.parse_value(s.parse_key("ui.bell"), "false") is False
    assert s.parse_value(s.parse_key("project.base_image"), "[x]") == "[x]"
    assert s.parse_value(s.parse_key("models.codex"), '["a", "b"]') == ["a", "b"]
    for key, value, message in (
        ("ui.bell", "yes", "true or false"),
        ("sandbox.cpus", "many", "an integer"),
        ("network.extra_domains", "a.com", "TOML list"),
        ("network.extra_domains", "[1]", "TOML list"),
    ):
        with pytest.raises(config.ConfigError, match=message):
            s.parse_value(s.parse_key(key), value)
    for key in ("nope.x", "ui.nope", "models.gpt", "ui"):
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
