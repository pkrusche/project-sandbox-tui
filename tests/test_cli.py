from __future__ import annotations

from pathlib import Path

import pytest

from project_sandbox_tui.cli import main


def test_missing_repo_path_is_an_error(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # a typo inside a repo must not silently open the surrounding repository
    assert main([str(repo / "typo"), "--print-config"]) == 2
    assert "no such directory" in capsys.readouterr().err


def test_print_config_uses_the_working_copy(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / ".project-sandbox-tui.toml").write_text('[project]\nbase_image = "img"\n')
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
