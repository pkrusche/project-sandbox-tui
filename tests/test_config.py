from __future__ import annotations

import json
import stat
import tomllib
from pathlib import Path

import pytest

from project_sandbox_tui import config as c
from project_sandbox_tui.jobs import JobManager, JobRecord
from project_sandbox_tui.sandbox import LaunchSpec


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
        ("[defaults]\nfast = 'true'\n", "defaults.fast must be true or false"),
        ("[ui]\nbell = 'yes'\n", "ui.bell must be true or false"),
        ("[network]\nca_certs = '/x.pem'\n", "list of strings"),
        ("[project]\nbase_image = 'a'\nbuild = 'python-uv'\n", "only one of"),
        ("[project]\nbuild = 'go'\n", "project.build"),
        ("[models]\ngpt = ['x']\n", "unknown agent"),
        ("ui = []\n", "must be a table"),
        ("models = []\n", "must be a table"),
        ("[defaults]\nmodel = 1\n", "must be a string"),
        ("[defaults]\ntimeout = true\n", "must be an integer"),
        ("[defaults]\ntimeout = -1\n", "must be >= 0"),
        ("[sandbox]\ncpus = 1.5\n", "must be an integer"),
        ("[sandbox]\nruntime = 'unknown'\n", "sandbox.runtime"),
        ("[ui]\nrefresh_seconds = true\n", "must be a number"),
        ("[ui]\nrefresh_seconds = 0\n", "must be finite and > 0"),
        ("[network]\nca_certs = ['valid', 1]\n", "list of strings"),
        ("[project]\ndocker_context = '.'\n", "needs project.dockerfile"),
        ("[project]\nbuild = 'node-npm'\nbuild_version = '22'\n", "build_version needs"),
        ("not toml = = \n", "Expected"),
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
        ('[ui]\nhost_agent_command = " "\n', "ui.host_agent_command"),
        ('[ui]\nhost_agent_command = "\'unclosed"\n', "ui.host_agent_command"),
        ("commands = []\n", "must be a table of name"),
        ("[commands]\ntest = 1\n", "commands.test must be a string"),
        ('[commands]\nhelp = "x"\n', "built-in"),
        ('[leader]\nt = "test"\n', "unknown command :test"),
        ('[ui]\nleader = ""\n[leader]\nt = "q"\n', "need ui.leader"),
        ('[ui]\nleader = ",,"\n', "ui.leader"),
        (f"[ui]\nrefresh_seconds = {10**400}\n", "ui.refresh_seconds"),
        ("actions = 1\n", r"must be a table of \[actions.NAME\]"),
        ("[actions]\nreview = 'x'\n", "must be a table, e.g."),
        ("[actions.review]\nagent = 'claude'\n", "needs a prompt"),
        ("[actions.review]\nprompt = 'x'\nagent = 'gpt'\n", "actions.review.agent"),
        ("[actions.review]\nprompt = 'x'\neffort = 'huge'\n", "actions.review.effort"),
        ("[actions.review]\nprompt = 'x'\ntimeout = -1\n", "actions.review.timeout"),
        ("[actions.review]\nprompt = 'x'\ntimeout = '1'\n", "must be an integer"),
        ("[actions.review]\nprompt = 'x'\nmode = 'x'\n", "unknown key"),
        ("[actions.help]\nprompt = 'x'\n", "built-in"),
        ("[commands]\nreview = 'x'\n[actions.review]\nprompt = 'x'\n", "also in"),
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


def test_template_command_examples_validate() -> None:
    """The commented-out [commands] / [leader] examples work once uncommented."""
    for template in (c.GLOBAL_TEMPLATE, c.PROJECT_TEMPLATE):
        lines = template.splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith("# [commands]"))
        example = "\n".join(line.removeprefix("# ") for line in lines[start:])
        cfg = c.from_dict(tomllib.loads(example))
        cfg.validate()
        assert cfg.commands and cfg.leader and cfg.actions


def test_actions(tmp_path: Path) -> None:
    write(
        tmp_path / c.PROJECT_CONFIG_NAME,
        "[actions.review]\n"
        'prompt = """Review {bookmark} against {main}.\nKeep {braces}.\n"""\n'
        "effort = 'high'\ntimeout = 0\n"
        "[leader]\nrv = 'review'\n",
    )
    cfg = candidate(tmp_path)
    action = cfg.actions["review"]
    assert (action.agent, action.effort, action.timeout) == ("", "high", 0)
    assert action.render("feature/x", "main") == "Review feature/x against main.\nKeep {braces}.\n"
    assert action.render("b", "m", " look at x ").endswith("Keep {braces}.\n\nlook at x\n")
    assert action.to_dict() == {"prompt": action.prompt, "effort": "high", "timeout": 0}


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


def test_fast_mode_defaults_and_remembered_choices(tmp_path: Path) -> None:
    write(c.global_config_path(), "[defaults]\nfast = true\n")
    cfg = c.load(tmp_path)
    assert c.resolve_launch_defaults(cfg, c.LastUsed.load(tmp_path)).fast
    c.remember(tmp_path, "claude", "", "", None, fast=False)
    c.remember(tmp_path, "codex", "", "", None, fast=True)
    c.remember(tmp_path, "bash", "", "", None)
    last = c.LastUsed.load(tmp_path)
    assert last.fast_by_agent == {"claude": False, "codex": True}
    assert not c.resolve_fast(cfg, last, "claude")  # explicit off overrides config
    assert c.resolve_fast(cfg, last, "codex")
    assert not c.resolve_launch_defaults(cfg, last).fast
    for agent in ("pi", "opencode", "bash"):
        assert not c.resolve_fast(cfg, last, agent)


def test_remembered_host_command_keeps_sandbox_choices_separate(tmp_path: Path) -> None:
    command = ["/opt/my agent", "--model", "a b", "", "$HOME; echo literal"]
    last = c.LastUsed(host_command=command)
    last.save(tmp_path)
    c.remember(tmp_path, "codex", "sandbox-model", "high", 300)
    loaded = c.LastUsed.load(tmp_path)
    assert loaded.host_command == command
    assert loaded.agent == "codex" and loaded.model_by_agent == {"codex": "sandbox-model"}


@pytest.mark.parametrize("command", [None, "codex", [], [""], [2], ["codex", None], ["bad\0"]])
def test_invalid_remembered_host_command_is_ignored(tmp_path: Path, command: object) -> None:
    write(
        c.repo_state_dir(tmp_path) / "last_used.json",
        json.dumps({"agent": "codex", "host_command": command}),
    )
    assert c.LastUsed.load(tmp_path) == c.LastUsed(agent="codex")


def test_state_dir_is_per_repo(tmp_path: Path) -> None:
    a = c.repo_state_dir(tmp_path / "x" / "repo")
    b = c.repo_state_dir(tmp_path / "y" / "repo")
    assert a != b
    assert a.parent == c.state_home()


def test_state_dir_is_private(tmp_path: Path) -> None:
    """Transcripts, prompts and job argv: owner only, also for an existing dir."""
    repo = tmp_path / "repo"
    state = c.repo_state_dir(repo)
    state.mkdir(parents=True)
    state.chmod(0o755)
    c.LastUsed(agent="claude").save(repo)
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    jobs = JobManager(repo, c.Config())
    jobs.save(JobRecord("b", LaunchSpec(branch="b", agent="bash"), pid=1, started_at=""))
    assert stat.S_IMODE(jobs.jobs_dir.stat().st_mode) == 0o700


def test_commands_and_leader_merge(tmp_path: Path) -> None:
    write(
        c.global_config_path(),
        '[commands]\ntest = "pytest"\nlint = "ruff check"\n[leader]\nt = "test"\n',
    )
    repo = tmp_path / "repo"
    write(
        repo / c.PROJECT_CONFIG_NAME,
        "[ui]\nleader = ','\n[commands]\ntest = \"uv run pytest\"\n[leader]\ns = '!jj st'\n",
    )
    c.ProjectTrust.check(repo).trust(repo)
    cfg = c.load(repo)
    assert cfg.ui.leader == ","
    assert cfg.commands == {**c.DEFAULT_COMMANDS, "test": "uv run pytest", "lint": "ruff check"}
    assert cfg.leader == {"t": "test", "s": "!jj st"}
    assert c.load(tmp_path).ui.leader == "\\"  # vim's default


def test_default_commands(tmp_path: Path) -> None:
    assert c.load(tmp_path).commands["abandon-empty"] == "jj abandon -r '(~empty())..'"
    write(c.global_config_path(), '[commands]\nabandon-empty = "true"\n')
    assert c.load(tmp_path).commands["abandon-empty"] == "true"


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


def test_explicit_empty_launch_defaults_override_config(tmp_path: Path) -> None:
    cfg = c.Config(defaults=c.Defaults(model="opus", effort="high", timeout=600))
    c.remember(tmp_path, "claude", "", "", 0)
    assert c.resolve_launch_defaults(cfg, c.LastUsed.load(tmp_path)) == c.LaunchDefaults(
        "claude", "", "", 0
    )


@pytest.mark.parametrize("timeout", [True, -1, "60", None])
def test_last_used_ignores_invalid_fields_without_losing_valid_ones(
    tmp_path: Path, timeout: object
) -> None:
    write(
        c.repo_state_dir(tmp_path) / "last_used.json",
        json.dumps(
            {
                "agent": "unknown",
                "effort": "extreme",
                "timeout": timeout,
                "model_by_agent": {"codex": "custom", "claude": 42},
                "fast_by_agent": {"codex": "true", "claude": 1, "bash": True},
            }
        ),
    )
    assert c.LastUsed.load(tmp_path) == c.LastUsed(model_by_agent={"codex": "custom"})


@pytest.mark.parametrize("raw", ["{", "[]", "null"])
def test_last_used_corrupt_file_uses_defaults(tmp_path: Path, raw: str) -> None:
    write(c.repo_state_dir(tmp_path) / "last_used.json", raw)
    assert c.LastUsed.load(tmp_path) == c.LastUsed()


@pytest.mark.parametrize("operation", ["write_text", "replace"])
def test_failed_last_used_save_preserves_defaults_and_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    c.LastUsed(agent="codex").save(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(Path, operation, fail)
        c.LastUsed(agent="claude").save(tmp_path)
    assert c.LastUsed.load(tmp_path).agent == "codex"
    assert list(c.repo_state_dir(tmp_path).glob("*.tmp")) == []


def test_failed_trust_write_keeps_review_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = write(tmp_path / c.PROJECT_CONFIG_NAME, '[defaults]\nagent = "codex"\n')
    c.ProjectTrust.check(tmp_path).trust(tmp_path)
    project.write_text('[defaults]\nagent = "bash"\n')

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(c.ConfigError, match="cannot write"):
        c.ProjectTrust.check(tmp_path).trust(tmp_path)
    assert c.ProjectTrust.check(tmp_path).needs_review
    assert c.load(tmp_path).defaults.agent == "codex"
    assert list(c.repo_state_dir(tmp_path).glob("*.tmp")) == []


@pytest.mark.parametrize(
    ("key", "placeholder"),
    [
        ("terminal_command", "branch"),
        ("notify_command", "message"),
        ("editor_command", "path"),
        ("host_agent_command", "path"),
        ("host_agent_command", "agent"),
        ("host_agent_command", "branch"),
    ],
)
def test_config_rejects_placeholders_embedded_in_shell_code(key: str, placeholder: str) -> None:
    cfg = c.Config()
    setattr(cfg.ui, key, f"sh -c 'printf %s \"{{{placeholder}}}\"'")
    with pytest.raises(c.ConfigError, match=rf"ui\.{key}: .*must occupy a whole argument"):
        cfg.validate()


def test_template_does_not_overwrite_a_file_created_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    mkdir = Path.mkdir

    def concurrent_create(directory: Path, *args, **kwargs) -> None:
        mkdir(directory, *args, **kwargs)
        path.write_text("existing settings\n")

    monkeypatch.setattr(Path, "mkdir", concurrent_create)
    assert not c.write_template(path, "template\n")
    assert path.read_text() == "existing settings\n"


def test_trust_reports_an_unwritable_state_directory(tmp_path: Path) -> None:
    write(tmp_path / c.PROJECT_CONFIG_NAME, c.PROJECT_TEMPLATE)
    trust = c.ProjectTrust.check(tmp_path)
    write(c.repo_state_dir(tmp_path), "not a directory")
    with pytest.raises(c.ConfigError, match="cannot write"):
        trust.trust(tmp_path)
