from __future__ import annotations

from pathlib import Path

import pytest

from project_sandbox_tui import config
from project_sandbox_tui.cli import main


def test_missing_repo_path_is_an_error(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # a typo inside a repo must not silently open the surrounding repository
    assert main([str(repo / "typo"), "--print-config"]) == 2
    assert "no such directory" in capsys.readouterr().err


def test_print_config_uses_the_working_copy(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / ".project-sandbox-tui.toml").write_text('[project]\nbase_image = "img"\n')
    config.ProjectTrust.check(repo).trust(repo)
    assert main([str(repo), "--print-config"]) == 0
    out = capsys.readouterr().out
    command = out.split("# interactive command for @\n", 1)[1]
    assert "--branch" not in command and "--agent claude" in command


def test_explicit_missing_config_is_an_error(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([str(repo), "--config", str(repo / "missing.toml"), "--print-config"]) == 2
    assert "config file does not exist" in capsys.readouterr().err


def test_print_config_reports_vcs_errors(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from project_sandbox_tui.vcs import JjBackend, VcsError

    def broken_list(self):
        raise VcsError("cannot list branches")

    monkeypatch.setattr(JjBackend, "list_branches", broken_list)
    assert main([str(repo), "--print-config"]) == 2
    assert "cannot list branches" in capsys.readouterr().err


def test_untrusted_project_config_warns_and_is_ignored(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / ".project-sandbox-tui.toml").write_text('[project]\nbase_image = "img"\n')
    assert main([str(repo), "--print-config"]) == 0
    out, err = capsys.readouterr()
    assert "not trusted yet" in err and "--trust-config" in err
    assert "cannot build command: no image configured" in out


@pytest.mark.parametrize(("answer", "code"), [("y", 0), ("n", 1), ("", 1)])
def test_trust_config_asks(
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
    code: int,
) -> None:
    (repo / ".project-sandbox-tui.toml").write_text('[project]\nbase_image = "img"\n')
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)
    assert main([str(repo), "--trust-config"]) == code
    assert '+base_image = "img"' in capsys.readouterr().out
    assert config.ProjectTrust.check(repo).needs_review == (code != 0)


def test_trust_config_refuses_an_invalid_file(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / ".project-sandbox-tui.toml").write_text("[nope]\n")
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert main([str(repo), "--trust-config"]) == 2
    assert "unknown section" in capsys.readouterr().err
    assert config.ProjectTrust.check(repo).needs_review


def test_init_trusts_the_template_it_writes(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("project_sandbox_tui.models.load_store", lambda: object())
    assert main([str(repo), "--init"]) == 0
    assert not config.ProjectTrust.check(repo).needs_review
    assert config.load(repo).project.base_image == "python:3.12-slim"
