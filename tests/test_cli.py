from __future__ import annotations

from pathlib import Path

import pytest

from project_sandbox_tui import config
from project_sandbox_tui.cli import main
from tests.conftest import jj


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
    assert "--branch" not in command and "--agent=claude" in command


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


@pytest.mark.parametrize(("answer", "code"), [("y", 0), (" YES ", 0), ("n", 1), ("", 1)])
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


def test_init_preserves_existing_untrusted_config(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = repo / config.PROJECT_CONFIG_NAME
    text = '[sandbox]\ncommand = "unreviewed-command"\n'
    project.write_text(text)
    monkeypatch.setattr("project_sandbox_tui.models.load_store", lambda: object())
    assert main([str(repo), "--init"]) == 0
    assert project.read_text() == text
    assert config.ProjectTrust.check(repo).needs_review
    assert config.load(repo).sandbox.command == "project-sandbox"


@pytest.mark.parametrize(
    ("markers", "source", "value"),
    [
        (["Dockerfile"], "dockerfile", "Dockerfile"),
        (["pyproject.toml"], "build", "python-uv"),
        (["uv.lock"], "build", "python-uv"),
        (["Cargo.toml"], "build", "rust-cargo"),
        (["package.json"], "build", "node-npm"),
        (
            ["Dockerfile", "pyproject.toml", "Cargo.toml", "package.json"],
            "dockerfile",
            "Dockerfile",
        ),
        (["pyproject.toml", "Cargo.toml", "package.json"], "build", "python-uv"),
        (["Cargo.toml", "package.json"], "build", "rust-cargo"),
    ],
)
def test_init_detects_project_from_repo_root(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    markers: list[str],
    source: str,
    value: str,
) -> None:
    for marker in markers:
        (repo / marker).touch()
    nested = repo / "nested"
    nested.mkdir()
    monkeypatch.setattr("project_sandbox_tui.models.load_store", lambda: object())
    assert main([str(nested), "--init"]) == 0
    trust = config.ProjectTrust.check(repo)
    assert not trust.needs_review
    assert trust.current == trust.trusted
    cfg = config.load(repo)
    assert getattr(cfg.project, source) == value
    assert cfg.project.base_image == ""
    assert cfg.project.docker_context == ("." if source == "dockerfile" else "")
    assert cfg.project.build_version == ""
    assert not config.project_config_path(nested).exists()


def test_init_ignores_directories_and_nested_project_markers(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "Dockerfile").mkdir()
    (repo / "pyproject.toml").mkdir()
    nested = repo / "nested"
    nested.mkdir()
    (nested / "Cargo.toml").touch()
    monkeypatch.setattr("project_sandbox_tui.models.load_store", lambda: object())
    assert main([str(repo), "--init"]) == 0
    assert config.load(repo).project.base_image == "python:3.12-slim"


@pytest.mark.parametrize(
    "marker", ["Dockerfile", "pyproject.toml", "Cargo.toml", "package.json", ""]
)
@pytest.mark.parametrize(
    "global_project",
    [
        'base_image = "global-image"',
        'dockerfile = "global.Dockerfile"\ndocker_context = "global-context"',
        'build = "rust-cargo"\nbuild_version = "1.90"',
    ],
)
def test_init_image_choice_overrides_global_image_settings(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    marker: str,
    global_project: str,
) -> None:
    if marker:
        (repo / marker).touch()
    gpath = repo / "global.toml"
    original = f'[project]\n{global_project}\n[defaults]\nagent = "codex"\nfast = true\n'
    gpath.write_text(original)
    monkeypatch.setattr("project_sandbox_tui.models.load_store", lambda: object())
    assert main([str(repo), "--config", str(gpath), "--init"]) == 0
    cfg = config.load(repo, gpath)
    assert cfg.has_image_source()
    assert cfg.defaults.agent == "codex"
    assert cfg.defaults.fast
    assert cfg.project.build_version == ""
    assert cfg.project.docker_context == ("." if marker == "Dockerfile" else "")
    assert gpath.read_text() == original
    capsys.readouterr()
    assert main([str(repo), "--config", str(gpath), "--print-config"]) == 0
    command = capsys.readouterr().out.split("# interactive command for @\n", 1)[1]
    assert "--agent=codex" in command and "--fast" in command.split()


def test_init_installs_local_skills_and_preserves_edits_on_rerun(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("project_sandbox_tui.models.load_store", lambda: object())
    ignore = repo / ".gitignore"
    ignore.write_text("# keep this rule\ncache/")  # no final newline
    assert main([str(repo), "--init"]) == 0
    paths = [repo / root / "skills/sbx-tui-config/SKILL.md" for root in (".agents", ".claude")]
    assert paths[0].read_text() == paths[1].read_text()
    assert paths[0].read_text().startswith("---\nname: sbx-tui-config\n")
    ignored = ignore.read_text()
    assert ignored.startswith("# keep this rule\ncache/\n")
    # Real jj snapshot: our skills are local, other skills remain trackable.
    other = repo / ".agents/skills/other/SKILL.md"
    other.parent.mkdir(parents=True)
    other.write_text("another skill\n")
    tracked = jj(repo, "file", "list").splitlines()
    assert ".agents/skills/other/SKILL.md" in tracked
    assert config.PROJECT_CONFIG_NAME in tracked
    for path in paths:
        assert path.relative_to(repo).as_posix() not in tracked
        path.write_text("user-edited skill\n")
    project_text = config.project_config_path(repo).read_text()
    global_text = config.global_config_path().read_text()
    (repo / "Dockerfile").touch()  # rerunning must not replace the earlier choice
    assert main([str(repo), "--init"]) == 0
    assert ignore.read_text() == ignored
    assert config.project_config_path(repo).read_text() == project_text
    assert config.global_config_path().read_text() == global_text
    assert all(path.read_text() == "user-edited skill\n" for path in paths)
    assert not config.ProjectTrust.check(repo).needs_review
    # Restore a missing copy without changing the other one.
    paths[0].unlink()
    assert main([str(repo), "--init"]) == 0
    assert paths[0].read_text().startswith("---\n")
    assert paths[1].read_text() == "user-edited skill\n"
    assert ignore.read_text() == ignored


def test_init_reports_skill_install_errors(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / ".agents").write_text("cannot create a directory here\n")
    monkeypatch.setattr("project_sandbox_tui.models.load_store", lambda: object())
    assert main([str(repo), "--init"]) == 2
    assert "could not install configuration skill" in capsys.readouterr().err


def test_init_trusts_only_the_template_even_if_the_file_changes(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_template = config.write_template
    project = config.project_config_path(repo)

    def changed_after_write(path: Path, text: str) -> bool:
        written = write_template(path, text)
        if path == project:
            path.write_text('[sandbox]\ncommand = "unreviewed-command"\n')
        return written

    monkeypatch.setattr(config, "write_template", changed_after_write)
    monkeypatch.setattr("project_sandbox_tui.models.load_store", lambda: object())
    assert main([str(repo), "--init"]) == 0
    assert config.ProjectTrust.check(repo).needs_review
    assert config.load(repo).sandbox.command == "project-sandbox"


def test_trust_config_eof_is_a_decline(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / config.PROJECT_CONFIG_NAME).write_text('[defaults]\nagent = "codex"\n')

    def eof(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert main([str(repo), "--trust-config"]) == 1
    assert "not trusted" in capsys.readouterr().out
    assert config.ProjectTrust.check(repo).needs_review
