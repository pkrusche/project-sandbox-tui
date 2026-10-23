from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from project_sandbox_tui import config as c


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def candidate(repo: Path) -> c.Config:
    """Load the repository's project config as it would be reviewed."""
    return c.load_candidate(c.ProjectTrust.check(repo))


def test_defaults_without_files(tmp_path: Path) -> None:
    cfg = c.load(tmp_path)
    assert cfg.defaults.agent == "claude"
    assert cfg.sources == []
    assert not cfg.has_image_source()


def test_project_overrides_global(tmp_path: Path) -> None:
    g = write(
        c.global_config_path(),
        '[defaults]\nagent = "codex"\neffort = "high"\n'
        '[network]\ninternet_proxy = "http://proxy:3128"\n'
        '[models]\ncodex = ["gpt-a", "gpt-b"]\n',
    )
    repo = tmp_path / "repo"
    write(
        repo / c.PROJECT_CONFIG_NAME,
        '[defaults]\neffort = "low"\n[project]\nbase_image = "debian:stable"\n',
    )
    c.ProjectTrust.check(repo).trust(repo)
    cfg = c.load(repo)
    assert cfg.sources == [g, c.trusted_config_path(repo)]
    assert cfg.defaults.agent == "codex"  # from global
    assert cfg.defaults.effort == "low"  # project wins
    assert cfg.network.internet_proxy == "http://proxy:3128"
    assert cfg.project.base_image == "debian:stable"
    assert cfg.models == {"codex": ["gpt-a", "gpt-b"]}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[defaults]\nagnet = 'x'\n", "unknown key"),
        ("[nope]\n", "unknown section"),
        ("[defaults]\nagent = 'gpt'\n", "defaults.agent"),
        ("[defaults]\neffort = 'extreme'\n", "defaults.effort"),
        ("[ui]\nbell = 'yes'\n", "ui.bell must be true or false"),
        ("[network]\nca_certs = '/x.pem'\n", "list of strings"),
        ("[project]\nbase_image = 'a'\nbuild = 'python-uv'\n", "only one of"),
        ("[project]\nbuild = 'go'\n", "project.build"),
        ("[models]\ngpt = ['x']\n", "unknown agent"),
        ("not toml = = \n", "Expected"),
    ],
)
def test_invalid_config(tmp_path: Path, text: str, message: str) -> None:
    write(tmp_path / c.PROJECT_CONFIG_NAME, text)
    with pytest.raises(c.ConfigError, match=message):
        candidate(tmp_path)


def test_templates_parse_and_validate() -> None:
    for template in (c.GLOBAL_TEMPLATE, c.PROJECT_TEMPLATE):
        cfg = c.from_dict(tomllib.loads(template))
        cfg.validate()


def test_last_used_updates_defaults(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = c.Config()
    cfg.defaults.model = "sonnet"
    cfg.models = {"codex": ["gpt-x"]}

    first = c.resolve_launch_defaults(cfg, c.LastUsed.load(repo))
    assert (first.agent, first.model, first.effort) == ("claude", "sonnet", "")

    c.remember(repo, "codex", "gpt-y", "high", 600)
    again = c.resolve_launch_defaults(cfg, c.LastUsed.load(repo))
    assert (again.agent, again.model, again.effort, again.timeout) == (
        "codex",
        "gpt-y",
        "high",
        600,
    )
    # switching agent in the form falls back per agent
    last = c.LastUsed.load(repo)
    assert c.resolve_model(cfg, last, "claude") == "sonnet"
    assert c.resolve_model(cfg, last, "pi") == ""

    # an interactive launch (timeout=None) keeps the remembered timeout
    c.remember(repo, "claude", "opus", "", None)
    final = c.resolve_launch_defaults(cfg, c.LastUsed.load(repo))
    assert (final.agent, final.model, final.effort, final.timeout) == ("claude", "opus", "", 600)


def test_state_dir_is_per_repo(tmp_path: Path) -> None:
    a = c.repo_state_dir(tmp_path / "x" / "repo")
    b = c.repo_state_dir(tmp_path / "y" / "repo")
    assert a != b
    assert a.parent == c.state_home()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[sandbox]\ncommand = " "\n', "sandbox.command"),
        ('[sandbox]\ncommand = "\'unclosed"\n', "sandbox.command"),
        ("[sandbox]\ncpus = -1\n", "sandbox.cpus"),
        ("[ui]\nrefresh_seconds = nan\n", "ui.refresh_seconds"),
        ("[ui]\nrefresh_seconds = inf\n", "ui.refresh_seconds"),
        ('[ui]\nterminal_command = "\'unclosed"\n', "ui.terminal_command"),
        ('[ui]\nterminal = "kitty"\n', "ui.terminal must be one of"),
        ('[ui]\nterminal = "custom"\n', "needs ui.terminal_command"),
        ('[ui]\nterminal = "tmux"\nterminal_command = "x {cmd}"\n', "only used with"),
        ('[ui]\neditor_command = " "\n', "ui.editor_command"),
    ],
)
def test_invalid_commands_and_numeric_settings(tmp_path: Path, text: str, message: str) -> None:
    write(tmp_path / c.PROJECT_CONFIG_NAME, text)
    with pytest.raises(c.ConfigError, match=message):
        candidate(tmp_path)


def test_terminal_mode_resolution(tmp_path: Path) -> None:
    ui = c.UI()
    assert ui.terminal_mode() == "inline"
    ui.terminal_command = "wezterm start -- sh -c {cmd}"
    assert ui.terminal_mode() == "custom"  # older configs keep working
    for mode in ("inline", "tmux", "zellij", "screen"):
        write(tmp_path / c.PROJECT_CONFIG_NAME, f'[ui]\nterminal = "{mode}"\n')
        assert candidate(tmp_path).ui.terminal_mode() == mode


def test_untrusted_project_config_is_ignored(tmp_path: Path) -> None:
    project = write(tmp_path / c.PROJECT_CONFIG_NAME, '[sandbox]\ncommand = "evil"\n')
    trust = c.ProjectTrust.check(tmp_path)
    assert trust.needs_review and trust.trusted is None
    assert "not trusted yet" in trust.describe()
    assert '+command = "evil"' in trust.diff()
    assert c.load(tmp_path).sandbox.command == "project-sandbox"
    assert c.load(tmp_path).sources == []

    trust.trust(tmp_path)
    assert not c.ProjectTrust.check(tmp_path).needs_review
    assert c.load(tmp_path).sandbox.command == "evil"

    project.unlink()  # no project config: the trusted copy is not used either
    assert not c.ProjectTrust.check(tmp_path).needs_review
    assert c.load(tmp_path).sandbox.command == "project-sandbox"


def test_changed_project_config_keeps_the_trusted_copy(tmp_path: Path) -> None:
    project = write(tmp_path / c.PROJECT_CONFIG_NAME, '[project]\nbase_image = "a"\n')
    c.ProjectTrust.check(tmp_path).trust(tmp_path)
    project.write_text('[project]\nbase_image = "b"\n')
    trust = c.ProjectTrust.check(tmp_path)
    assert trust.needs_review
    assert "changed since it was trusted" in trust.describe()
    assert '-base_image = "a"' in trust.diff() and '+base_image = "b"' in trust.diff()
    assert c.load(tmp_path).project.base_image == "a"
    assert c.load_candidate(trust).project.base_image == "b"

    # Trusting stores what was reviewed, not what the file says by then.
    project.write_text('[project]\nbase_image = "c"\n')
    trust.trust(tmp_path)
    assert c.load(tmp_path).project.base_image == "b"
    assert c.ProjectTrust.check(tmp_path).needs_review
