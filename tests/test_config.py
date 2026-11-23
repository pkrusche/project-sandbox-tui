from __future__ import annotations

import json
import os
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


def test_commit_summary_profile() -> None:
    cfg = c.from_dict({"defaults": {"agent": "claude", "effort": "high"}})
    assert cfg.commit_summary_profile() == (
        "commit-reviewer on Codex",
        c.AgentProfile(agent="codex", model="gpt-6-luna", effort="low"),
    )
    cfg = c.from_dict(
        {
            "commit_review": {"profile": "Deep review"},
            "profiles": {"Deep review": {"agent": "codex", "model": "model", "fast": True}},
        }
    )
    assert cfg.commit_summary_profile() == (
        "Deep review",
        c.AgentProfile(agent="codex", model="model", fast=True),
    )
    cfg.profiles["Deep review"].agent = "bash"
    with pytest.raises(c.ConfigError, match="uses bash"):
        cfg.commit_summary_profile()
    del cfg.profiles["Deep review"]
    with pytest.raises(c.ConfigError, match="'Deep review' is missing"):
        cfg.commit_summary_profile()


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


def test_unknown_project_keys_warn(tmp_path: Path) -> None:
    path = write(
        c.project_config_path(tmp_path),
        "future = true\n"
        "[defaults]\nagent = 'codex'\nagnet = 'x'\n"
        "[actions.review]\nprompt = 'Review'\nmode = 'x'\n"
        "[profiles.deep]\neffort = 'high'\nextra = 1\n"
        "[feedback]\nenabled = true\n"
        "[workflows.flow]\nplanning_action = 'review'\nsteps = ['review']\nextra = 1\n"
        "[[workspace_skills]]\nsource = 'skill'\ntargets = ['.agents/skills/skill']\nextra = 1\n",
    )
    cfg = candidate(tmp_path)
    assert cfg.defaults.agent == "codex"
    assert cfg.actions["review"].prompt == "Review"
    assert cfg.profiles["deep"].effort == "high"
    assert cfg.workflows["flow"].steps == ["review"]
    assert cfg.workspace_skills[0].source == "skill"
    assert cfg.warnings == [
        f"{path}: {message} (ignored)"
        for message in (
            "unknown section(s): future",
            "[defaults] has unknown key(s): agnet",
            "[actions.review] has unknown key(s): mode",
            "[profiles.deep] has unknown key(s): extra",
            "[workflows.flow] has unknown key(s): extra",
            "workspace_skills[0] has unknown key(s): extra",
        )
    ]
    assert not c.load(tmp_path).warnings
    c.ProjectTrust.check(tmp_path).trust(tmp_path)
    assert c.load(tmp_path).warnings == cfg.warnings


def test_unknown_global_key_is_still_an_error(tmp_path: Path) -> None:
    text = "[defaults]\nagnet = 'x'\n"
    write(c.global_config_path(), text)
    write(c.project_config_path(tmp_path), text)
    with pytest.raises(c.ConfigError, match=r"unknown key\(s\): agnet"):
        candidate(tmp_path)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[defaults]\nagent = 'claude code'\nagnet = 'x'\n", "defaults.agent"),
        ("[defaults]\neffort = 'extreme'\n", "defaults.effort"),
        ("[defaults]\nfast = 'true'\n", "defaults.fast must be true or false"),
        ("[ui]\nbell = 'yes'\n", "ui.bell must be true or false"),
        ("[network]\nca_certs = '/x.pem'\n", "list of strings"),
        ("[project]\nbase_image = 'a'\nbuild = 'python-uv'\n", "only one of"),
        ("[project]\nbuild = 'go'\n", "project.build"),
        ("[models]\nGPT = ['x']\n", "models.GPT"),
        ("ui = []\n", "must be a table"),
        ("models = []\n", "must be a table"),
        ("[defaults]\nmodel = 1\n", "must be a string"),
        ("[defaults]\ntimeout = true\n", "must be an integer"),
        ("[defaults]\ntimeout = -1\n", "must be >= 0"),
        ("[sandbox]\ncpus = 1.5\n", "must be an integer"),
        ("[sandbox]\nruntime = 'Docker Desktop'\n", "sandbox.runtime"),
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
        ("[actions.review]\nprompt = 'x'\nagent = 'g p t'\n", "actions.review.agent"),
        ("[actions.review]\nprompt = 'x'\neffort = 'huge'\n", "actions.review.effort"),
        ("[actions.review]\nprompt = 'x'\ntimeout = -1\n", "actions.review.timeout"),
        ("[actions.review]\nprompt = 'x'\ntimeout = '1'\n", "must be an integer"),
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
    assert action.render("feature/{main}", "main").startswith("Review feature/{main} against main.")
    assert action.render("b", "m", " look at x ").endswith("Keep {braces}.\n\nlook at x\n")
    assert action.to_dict() == {"prompt": action.prompt, "effort": "high", "timeout": 0}


def test_action_profile(tmp_path: Path) -> None:
    write(
        tmp_path / c.PROJECT_CONFIG_NAME,
        "[actions.review]\nprompt = 'Review {bookmark}'\nprofile = 'Deep review'\ntimeout = 600\n"
        "[actions.check]\nprompt = 'Check'\nprofile = 'not configured'\n",
    )
    cfg = candidate(tmp_path)  # unknown profiles warn where used, not at load
    review = cfg.actions["review"]
    assert (review.profile, review.timeout, review.has_own_settings) == ("Deep review", 600, False)
    assert review.to_dict() == {
        "prompt": "Review {bookmark}",
        "profile": "Deep review",
        "timeout": 600,
    }
    assert cfg.actions["check"].profile == "not configured"


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ("profile = 'deep'\nagent = 'claude'\n", "sets profile and agent"),
        ("profile = 'deep'\nmodel = 'opus'\neffort = 'high'\n", "sets profile and model, effort"),
        ("profile = 'Custom'\n", "actions.review.profile: profile name 'custom' is reserved"),
        ("profile = ' deep'\n", "actions.review.profile: profile name must contain"),
    ],
)
def test_action_profile_rejected(tmp_path: Path, fields: str, message: str) -> None:
    write(tmp_path / c.PROJECT_CONFIG_NAME, f"[actions.review]\nprompt = 'Review'\n{fields}")
    with pytest.raises(c.ConfigError, match=message):
        candidate(tmp_path)


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


def test_older_review_cannot_replace_newer_approval(tmp_path: Path) -> None:
    project = write(tmp_path / c.PROJECT_CONFIG_NAME, '[defaults]\nagent = "bash"\n')
    pending = c.ProjectTrust.check(tmp_path)
    project.write_text('[defaults]\nagent = "codex"\n')
    c.ProjectTrust.check(tmp_path).trust(tmp_path)
    with pytest.raises(c.ConfigError, match="review the diff again"):
        pending.trust(tmp_path)
    assert c.load(tmp_path).defaults.agent == "codex"


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
                "agent": "not an agent",
                "effort": "extreme",
                "timeout": timeout,
                "provider": [],
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


def test_simultaneous_launches_retain_each_agents_model(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(c.AGENTS)) as pool:
        list(pool.map(lambda agent: c.remember(tmp_path, agent, agent, "", None), c.AGENTS))
    assert c.LastUsed.load(tmp_path).model_by_agent == {agent: agent for agent in c.AGENTS}


def profiles_cfg() -> c.Config:
    cfg = c.Config()
    cfg.defaults.timeout = 900
    cfg.profiles = {
        "fast": c.AgentProfile(agent="claude", model="haiku", effort="low"),
        "deep": c.AgentProfile(
            agent="codex", model="gpt-x", effort="high", fast=True, extra_args=["--memory=16g"]
        ),
        "shell": c.AgentProfile(agent="bash", model="ignored"),
    }
    return cfg


def test_remembered_profile_matches_launch_form_rules() -> None:
    cfg = profiles_cfg()
    assert c.remembered_profile(cfg, c.LastUsed()) == "fast"  # nothing chosen: first profile
    assert c.remembered_profile(cfg, c.LastUsed(profile="deep")) == "deep"
    assert c.remembered_profile(cfg, c.LastUsed(profile="")) == ""  # Custom
    assert c.remembered_profile(cfg, c.LastUsed(profile="gone")) == ""  # removed: Custom
    assert c.remembered_profile(c.Config(), c.LastUsed()) == ""  # no profiles: Custom


@pytest.mark.parametrize(
    ("action", "last", "expected", "warns"),
    [
        (
            c.Action("p", profile="deep"),
            c.LastUsed(profile="fast"),
            c.StepChoice("profile", "deep"),
            False,
        ),
        (c.Action("p", effort="low"), c.LastUsed(profile="fast"), c.ACTION_SETTINGS, False),
        (c.Action("p", agent="pi"), c.LastUsed(profile="fast"), c.ACTION_SETTINGS, False),
        (c.Action("p"), c.LastUsed(profile="fast"), c.StepChoice("profile", "fast"), False),
        (c.Action("p"), c.LastUsed(), c.StepChoice("profile", "fast"), False),
        (c.Action("p"), c.LastUsed(profile=""), c.CUSTOM, False),
        (c.Action("p"), c.LastUsed(profile="gone"), c.CUSTOM, False),
        (
            c.Action("p", profile="gone"),
            c.LastUsed(profile="deep"),
            c.StepChoice("profile", "deep"),
            True,
        ),
        (c.Action("p", profile="gone"), c.LastUsed(profile=""), c.CUSTOM, True),
    ],
)
def test_initial_choice(
    action: c.Action, last: c.LastUsed, expected: c.StepChoice, warns: bool
) -> None:
    choice, warning = c.initial_choice(profiles_cfg(), last, "review", action)
    assert choice == expected
    if warns:
        assert warning == "actions.review refers to unknown profile 'gone'"
    else:
        assert warning == ""


def test_resolve_action_settings() -> None:
    cfg = profiles_cfg()
    last = c.LastUsed(
        agent="pi", effort="medium", model_by_agent={"pi": "pi-model"}, extra_args=["--cpus=2"]
    )
    action = c.Action("p", timeout=600)

    deep = c.resolve_action_settings(cfg, last, action, c.StepChoice("profile", "deep"))
    assert deep == c.ActionSettings(
        "codex", "gpt-x", "high", True, 600, ["--memory=16g"], profile="deep"
    )
    assert deep.label() == "codex/gpt-x/high/fast"

    shell = c.resolve_action_settings(cfg, last, c.Action("p"), c.StepChoice("profile", "shell"))
    assert (shell.agent, shell.model, shell.timeout) == ("bash", "", 900)

    custom = c.resolve_action_settings(cfg, last, action, c.CUSTOM)
    assert custom == c.ActionSettings("pi", "pi-model", "medium", False, 600, ["--cpus=2"])

    # a profile deleted after it was chosen resolves as Custom
    missing = c.resolve_action_settings(cfg, last, action, c.StepChoice("profile", "gone"))
    assert missing == custom

    own = c.Action("p", agent="claude", effort="low")
    settings = c.resolve_action_settings(cfg, last, own, c.ACTION_SETTINGS)
    # unchanged pre-profile behaviour: no profile args, model only from the same agent
    assert settings == c.ActionSettings("claude", "", "low", False, 900, [])
    same_agent = c.resolve_action_settings(
        cfg, last, c.Action("p", effort="low"), c.ACTION_SETTINGS
    )
    assert (same_agent.agent, same_agent.model, same_agent.effort) == ("pi", "pi-model", "low")


def test_bash_gateway_model_survives_profile_and_custom_action_resolution() -> None:
    cfg = c.Config()
    cfg.network.agent_proxy = "http://localhost:4000/v1"
    cfg.profiles["shell"] = c.AgentProfile("bash", "gateway-model", gateway=True)
    last = c.LastUsed(
        agent="bash", model_by_agent={"bash": "gateway-model"}, gateway_by_agent={"bash": True}
    )
    for choice in (c.CUSTOM, c.StepChoice("profile", "shell")):
        settings = c.resolve_action_settings(cfg, last, c.Action("run"), choice)
        assert settings.model == "gateway-model" and settings.gateway


@pytest.mark.parametrize(
    "text",
    [
        "[profiles.cp]\nagent = 'copilot'\n",
        "[defaults]\nagent = 'newagent'\n",
        "[sandbox]\nruntime = 'chroot'\n",
        "[sandbox]\nruntime = 'colima'\n",
        "[models]\ncopilot = ['gpt-5-mini']\n",
    ],
)
def test_newer_launcher_agents_and_runtimes_load(tmp_path: Path, text: str) -> None:
    """Support is checked by doctor and before launch, not when loading."""
    write(tmp_path / c.PROJECT_CONFIG_NAME, text)
    candidate(tmp_path)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("[profiles.local]\nagent = 'pi'\ngateway = true\n", "profiles.local.gateway"),
        ("[defaults]\ngateway = true\n", "defaults.gateway"),
    ],
)
def test_gateway_needs_agent_proxy(tmp_path: Path, text: str, key: str) -> None:
    write(tmp_path / c.PROJECT_CONFIG_NAME, text)
    with pytest.raises(c.ConfigError, match=rf"{key}.*needs network.agent_proxy"):
        candidate(tmp_path)


def test_project_profile_uses_gateway_defined_globally(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write(c.global_config_path(), "[network]\nagent_proxy = 'http://127.0.0.1:4000/v1'\n")
    write(repo / c.PROJECT_CONFIG_NAME, "[profiles.local]\nagent = 'pi'\ngateway = true\n")
    cfg = candidate(repo)
    assert cfg.profiles["local"].gateway


def test_gateway_resolution_and_memory(tmp_path: Path) -> None:
    cfg = c.Config()
    cfg.defaults.gateway = True
    assert not c.resolve_gateway(cfg, c.LastUsed(), "pi")  # no gateway configured
    cfg.network.agent_proxy = "http://127.0.0.1:4000/v1"
    assert c.resolve_gateway(cfg, c.LastUsed(), "pi")
    assert not c.resolve_gateway(cfg, c.LastUsed(), "claude")
    c.remember(tmp_path, "pi", "gpt-5-mini", "", None, gateway=False)
    c.remember(tmp_path, "claude", "", "", None, gateway=True)  # not a gateway agent
    last = c.LastUsed.load(tmp_path)
    assert last.gateway_by_agent == {"pi": False}
    assert not c.resolve_gateway(cfg, last, "pi")


def test_personas_merge_across_files(tmp_path: Path) -> None:
    global_path = write(tmp_path / "g.toml", '[personas.reviewer]\ntier = "frontier"\n')
    write(c.project_config_path(tmp_path / "repo"), "[personas.reviewer]\nlevel = 5\n")
    c.ProjectTrust.check(tmp_path / "repo").trust(tmp_path / "repo")
    cfg = c.load(tmp_path / "repo", global_path)
    assert (cfg.personas["reviewer"].tier, cfg.personas["reviewer"].level) == ("frontier", 5)
    assert cfg.personas["reviewer"].effort == "high"  # unset fields keep built-in values


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[personas.triage]\neffort = "low"\n', "personas.triage.tier"),
        ("[personas.reviewer]\nlevel = 6\n", "personas.reviewer.level"),
        ('[catalogue.models.gateway-mistral]\nhigh = "x"\n', "catalogue.models.gateway-mistral"),
        ('[catalogue]\ngateway_agent = "codex"\n', "catalogue.gateway_agent"),
        ('[actions.review]\nprompt = "x"\npersona = "auditor"\n', "actions.review.persona"),
        (
            '[actions.review]\nprompt = "x"\npersona = "commit-reviewer"\n',
            "actions.review.persona",
        ),
        (
            '[actions.review]\nprompt = "x"\npersona = "reviewer"\nprofile = "deep"\n',
            r"\[actions.review\] sets persona and profile",
        ),
        (
            '[actions.review]\nprompt = "x"\npersona = "reviewer"\nmodel = "m"\n',
            r"\[actions.review\] sets persona and model",
        ),
        ('[actions.review]\nprompt = "x"\nlevel = 6\n', "actions.review.level"),
        ('[commit_review]\nprofile = "a"\nprovider = "codex"\n', "commit_review"),
        ('[commit_review]\nprovider = "mistral"\n', "commit_review.provider"),
    ],
)
def test_invalid_persona_and_catalogue_config(tmp_path: Path, text: str, message: str) -> None:
    path = write(tmp_path / "g.toml", text)
    with pytest.raises(c.ConfigError, match=message):
        c.load(tmp_path, path)


def test_action_persona_with_level_loads(tmp_path: Path) -> None:
    path = write(
        tmp_path / "g.toml", '[actions.review]\nprompt = "x"\npersona = "reviewer"\nlevel = 5\n'
    )
    action = c.load(tmp_path, path).actions["review"]
    assert (action.persona, action.level) == ("reviewer", 5)
    assert action.to_dict() == {"prompt": "x", "persona": "reviewer", "level": 5}


def test_older_last_used_loads_without_selection(tmp_path: Path) -> None:
    path = c.repo_state_dir(tmp_path) / "last_used.json"
    write(path, json.dumps({"agent": "codex", "profile": "", "extra_args": ["--memory=12g"]}))
    last = c.LastUsed.load(tmp_path)
    assert (last.selection, last.provider, last.persona) == (None, "", "")
    assert last.agent == "codex" and last.extra_args == ["--memory=12g"]
    write(path, json.dumps({"selection": "x", "provider": "mistral", "persona": 7}))
    last = c.LastUsed.load(tmp_path)
    assert (last.selection, last.provider, last.persona) == (None, "", "")


def test_remembered_selection() -> None:
    cfg = c.Config()
    implementer = c.StepChoice("persona", persona="implementer")
    assert c.remembered_selection(cfg, c.LastUsed()) == implementer
    remembered = c.LastUsed(selection="persona", provider="claude-code", persona="debugger")
    assert c.remembered_selection(cfg, remembered) == c.StepChoice("persona", persona="debugger")
    assert c.remembered_provider(cfg, remembered) == "claude-code"
    cfg.profiles["deep"] = c.AgentProfile(agent="codex")
    assert c.remembered_selection(cfg, c.LastUsed()) == c.StepChoice("profile", "deep")
    assert c.remembered_selection(cfg, c.LastUsed(profile="")) == c.CUSTOM
    gone = c.LastUsed(selection="persona", persona="gone")
    assert c.remembered_selection(cfg, gone) == implementer


def test_default_provider() -> None:
    cfg = c.from_dict({"catalogue": {"default_provider": "claude-code"}})
    assert c.remembered_provider(cfg, c.LastUsed()) == "claude-code"
    assert c.remembered_provider(c.Config(), c.LastUsed()) == "codex"
    unavailable = {"codex", "claude-code"}
    assert (
        c.remembered_provider(c.Config(), c.LastUsed(), lambda row: row not in unavailable)
        == "gateway-openai"
    )


def test_persona_launch_keeps_custom_settings(tmp_path: Path) -> None:
    c.remember(tmp_path, "pi", "gpt-5-mini", "low", 60, gateway=False, extra_args=["--memory=12g"])
    c.remember(
        tmp_path,
        "pi",
        "gpt-6-luna",
        "medium",
        90,
        gateway=True,
        extra_args=[],
        provider="gateway-openai",
        persona="implementer",
    )
    last = c.LastUsed.load(tmp_path)
    assert (last.selection, last.provider, last.persona) == (
        "persona",
        "gateway-openai",
        "implementer",
    )
    assert last.extra_args == ["--memory=12g"] and last.profile == ""
    assert last.gateway_by_agent == {"pi": False}
    assert last.model_by_agent == {"pi": "gpt-5-mini"} and last.effort == "low"
    assert last.timeout == 90
    c.remember(tmp_path, "codex", "", "", None, profile="")
    last = c.LastUsed.load(tmp_path)
    assert last.selection is None and last.provider == "gateway-openai"  # provider kept


def test_private_dir_creates_missing_ancestors_owner_only(tmp_path: Path) -> None:
    # project-sandbox refuses lock storage under a group-writable $XDG_STATE_HOME.
    target = tmp_path / "state" / "app" / "repo"
    old = os.umask(0o002)
    try:
        c.private_dir(target)
    finally:
        os.umask(old)
    for path in (tmp_path / "state", tmp_path / "state" / "app", target):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, path


def test_workflow_trust_scan(tmp_path: Path) -> None:
    directory = c.workflow_dir(tmp_path)
    path = write(directory / "spec.toml", "first")
    write(directory / "2bad.toml", "invalid")
    (directory / "link.toml").symlink_to(path)
    (directory / "folder.toml").mkdir()
    files, warnings = c.WorkflowTrust.scan(tmp_path)
    assert len(files) == 1 and len(warnings) == 3
    assert files[0].needs_review and files[0].trusted is None
    files[0].trust()
    assert stat.S_IMODE(files[0].trusted_path.parent.stat().st_mode) == 0o700
    files, _ = c.WorkflowTrust.scan(tmp_path)
    assert not files[0].needs_review and files[0].trusted == "first"
    path.write_text("changed")
    files, _ = c.WorkflowTrust.scan(tmp_path)
    assert files[0].needs_review and files[0].trusted == "first"
    path.unlink()
    files, _ = c.WorkflowTrust.scan(tmp_path)
    assert files == []


def test_parse_workflow_file() -> None:
    path = Path("spec.toml")
    workflow, actions, commands, warnings = c.parse_workflow_file(
        '[workflow]\nplanning_action = "plan"\nextra = true\n'
        '[actions.plan]\nprompt = "hello"\npersona = "reviewer"\n'
        '[commands]\ntest = "echo ok"\n[unknown]\nx = 1\n',
        path,
    )
    assert workflow.planning_action == "plan"
    assert actions["plan"].persona == "reviewer"
    assert commands["test"] == "echo ok"
    assert len(warnings) == 2
    with pytest.raises(c.ConfigError, match="missing \\[workflow\\]"):
        c.parse_workflow_file("[actions.plan]\nprompt = 'x'", path)
    for key in ("profile", "agent", "model", "effort"):
        with pytest.raises(c.ConfigError, match="persona only"):
            c.parse_workflow_file(f"[workflow]\n[actions.plan]\n{key} = 'x'", path)
