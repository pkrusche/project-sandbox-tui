"""Deploying the bundled and configured skills into bookmark workspaces."""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from project_sandbox_tui import workspace_skills as ws
from project_sandbox_tui.config import (
    CONFIG_SKILL_TARGETS,
    Config,
    ConfigError,
    WorkspaceSkill,
    from_dict,
)
from project_sandbox_tui.jobs import BusyError, JobManager
from project_sandbox_tui.queue import TaskQueue
from project_sandbox_tui.sandbox import LaunchSpec
from project_sandbox_tui.vcs import JjBackend, workspace_path
from project_sandbox_tui.workspace_skills import PreparationError, WorkspacePreparer
from tests.conftest import add_bookmark, jj

BUNDLED = list(CONFIG_SKILL_TARGETS)
CUSTOM = ".agents/skills/release-review"


def _preparer(repo: Path) -> WorkspacePreparer:
    return WorkspacePreparer(repo, JjBackend(repo))


def _git_dir(repo: Path) -> Path:
    return Path(jj(repo, "git", "root").strip())


def _excludes(repo: Path) -> list[str]:
    path = _git_dir(repo) / "info" / "exclude"
    return path.read_text().splitlines() if path.exists() else []


def _snapshot(workspace: Path) -> list[str]:
    """Files jj tracks in the workspace after snapshotting it."""
    return jj(workspace, "file", "list").splitlines()


def _skill(tmp_path: Path, name: str = "release-review") -> Path:
    source = tmp_path / "skills" / name
    (source / "scripts").mkdir(parents=True)
    (source / "SKILL.md").write_text(f"---\nname: {name}\n---\nRelease review.\n")
    helper = source / "scripts" / "check.sh"
    helper.write_text("#!/bin/sh\necho checked\n")
    helper.chmod(0o755)
    (source / "notes.txt").write_text("notes\n")
    return source


def _ignore_on_bookmark(repo: Path, bookmark: str, *rules: str) -> None:
    """Commit a .gitignore with ``rules`` on top of ``bookmark`` and move it there."""
    jj(repo, "new", "--quiet", bookmark)
    (repo / ".gitignore").write_text("".join(f"{rule}\n" for rule in rules))
    jj(repo, "commit", "--quiet", "-m", "ignore rules")
    jj(repo, "bookmark", "move", "--quiet", bookmark, "--to", "@-")
    jj(repo, "new", "--quiet", "main")


def _custom_cfg(source: Path, *targets: str) -> Config:
    cfg = Config()
    cfg.workspace_skills = [WorkspaceSkill(str(source), list(targets or [CUSTOM]))]
    cfg.validate()
    return cfg


# ---------------------------------------------------------------- configuration


def test_config_parses_workspace_skills_and_opt_out() -> None:
    cfg = from_dict(
        {
            "project": {"deploy_config_skill": False},
            "workspace_skills": [
                {"source": "~/skills/a", "targets": [".agents/skills/a", ".claude/skills/a"]},
                # The bundled skill's targets are free once it is disabled.
                {"source": "mine", "targets": [".claude/skills/sbx-tui-config"]},
            ],
        }
    )
    cfg.validate()
    assert not cfg.project.deploy_config_skill
    assert [s.targets for s in cfg.workspace_skills][0] == [".agents/skills/a", ".claude/skills/a"]
    assert Config().project.deploy_config_skill
    assert Config().workspace_skills == []


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"workspace_skills": {"source": "a"}}, "array of [[workspace_skills]]"),
        ({"workspace_skills": ["a"]}, "must be a table"),
        ({"workspace_skills": [{"source": "a", "target": []}]}, "unknown key(s): target"),
        ({"workspace_skills": [{"source": "a", "targets": "x"}]}, "list of strings"),
        ({"workspace_skills": [{"source": "", "targets": [CUSTOM]}]}, "source must name"),
        ({"workspace_skills": [{"source": "a", "targets": []}]}, "at least one target"),
        ({"workspace_skills": [{"source": "a", "targets": ["skills/a"]}]}, "directly in"),
        ({"workspace_skills": [{"source": "a", "targets": [".agents/skills"]}]}, "directly in"),
        (
            {"workspace_skills": [{"source": "a", "targets": [".agents/skills/a/b"]}]},
            "directly in",
        ),
        ({"workspace_skills": [{"source": "a", "targets": [".agents/skills/.."]}]}, "plain"),
        (
            {"workspace_skills": [{"source": "a", "targets": [CUSTOM, CUSTOM]}]},
            "also used by workspace_skills[0]",
        ),
        (
            {"workspace_skills": [{"source": "a", "targets": [".agents/skills/sbx-tui-config"]}]},
            "also used by project.deploy_config_skill",
        ),
    ],
)
def test_config_rejects_invalid_workspace_skills(data: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=None) as caught:
        from_dict(data).validate()
    assert message in str(caught.value)


def test_config_list_shows_workspace_skills(tmp_path: Path) -> None:
    from project_sandbox_tui import settings

    cfg = _custom_cfg(tmp_path / "s")
    flat = dict(settings.flatten(settings.effective(cfg)))
    assert flat["workspace_skills[0].targets"] == [CUSTOM]
    assert flat["project.deploy_config_skill"] is True


def test_planned_destinations_for_previews(repo: Path, tmp_path: Path) -> None:
    cfg = _custom_cfg(tmp_path / "s")
    root = workspace_path(repo, "feature/x", None)
    assert ws.planned_destinations(cfg, repo, "feature/x") == [
        root / BUNDLED[0],
        root / BUNDLED[1],
        root / CUSTOM,
    ]
    assert ws.planned_destinations(cfg, repo, "@") == []
    assert ws.planned_destinations(cfg, repo, "feature/x", ["--worktree-dir=/x"]) == []
    cfg.project.worktree_dir = str(tmp_path / "trees")
    assert ws.planned_destinations(cfg, repo, "a")[0] == tmp_path / "trees" / "a" / BUNDLED[0]
    assert not root.exists()  # no I/O


# ---------------------------------------------------------------- bundled skill


def test_bundled_skill_deploys_to_new_workspace_and_stays_out_of_snapshots(repo: Path) -> None:
    """An uninitialized project: no .gitignore at all."""
    preparer = _preparer(repo)
    path = preparer.prepare(Config(), "feature/x", sandbox=True)
    assert path == workspace_path(repo, "feature/x", None)
    assert path.name.startswith("feature-x-")
    names = jj(repo, "workspace", "list", "-T", 'stringify(name) ++ "\\n"').split()
    assert path.name in names and "feature/x" not in names
    assert not (path / ".git").exists()
    bundled = (Path(ws.__file__).parent / "skills" / "sbx-tui-config" / "SKILL.md").read_text()
    for target in BUNDLED:
        assert (path / target / "SKILL.md").read_text() == bundled
    assert _excludes(repo).count("/.agents/skills/sbx-tui-config/") == 1
    assert _excludes(repo).count("/.claude/skills/sbx-tui-config/") == 1
    assert not (repo / ".gitignore").exists()
    # An unrelated skill stays trackable; staging leaves nothing behind.
    (path / ".agents" / "skills" / "other").mkdir()
    (path / ".agents" / "skills" / "other" / "SKILL.md").write_text("other\n")
    assert _snapshot(path) == [".agents/skills/other/SKILL.md", "a.txt", "b.txt"]
    assert not [p for p in (path / ".jj").iterdir() if p.name.startswith(ws.STAGING_PREFIX)]
    notes = [n.message for n in preparer.take_notes()]
    assert notes == [
        f"feature/x: created workspace {path}",
        "feature/x: deployed skills to " + ", ".join(BUNDLED),
    ]

    # Repeated deployment keeps a local edit and adds no rules.
    (path / BUNDLED[0] / "SKILL.md").write_text("local edit\n")
    preparer.prepare(Config(), "feature/x", sandbox=True)
    assert (path / BUNDLED[0] / "SKILL.md").read_text() == "local edit\n"
    assert _excludes(repo).count("/.agents/skills/sbx-tui-config/") == 1
    assert preparer.take_notes() == []

    # Removing a copy while idle refreshes it on the next use.
    import shutil

    shutil.rmtree(path / BUNDLED[0])
    preparer.prepare(Config(), "feature/x", sandbox=True)
    assert (path / BUNDLED[0] / "SKILL.md").read_text() == bundled


def test_bundled_skill_uses_in_tree_rules_and_preserves_excludes(repo: Path) -> None:
    exclude = _git_dir(repo) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("# mine\n*.log")  # no trailing newline
    _ignore_on_bookmark(repo, "feature/x", "/.agents/", "/.claude/")
    path = _preparer(repo).prepare(Config(), "feature/x", sandbox=False)
    assert exclude.read_text() == "# mine\n*.log"  # in-tree rules already cover it
    assert all((path / t / "SKILL.md").is_file() for t in BUNDLED)

    add_bookmark(repo, "other", "o.txt", "o\n")
    _preparer(repo).prepare(Config(), "other", sandbox=False)
    assert exclude.read_text().splitlines() == [
        "# mine",
        "*.log",
        "/.agents/skills/sbx-tui-config/",
        "/.claude/skills/sbx-tui-config/",
    ]


def test_bundled_skill_problems_are_reported_without_stopping(repo: Path) -> None:
    # In-tree negation outranks local excludes; a tracked copy is a collision.
    jj(repo, "new", "--quiet", "feature/x")
    (repo / ".gitignore").write_text("!/.agents/skills/sbx-tui-config/\n")
    tracked = repo / ".claude" / "skills" / "sbx-tui-config"
    tracked.mkdir(parents=True)
    (tracked / "SKILL.md").write_text("tracked\n")
    jj(repo, "commit", "--quiet", "-m", "negation and tracked skill")
    jj(repo, "bookmark", "move", "--quiet", "feature/x", "--to", "@-")
    jj(repo, "new", "--quiet", "main")
    preparer = _preparer(repo)
    path = preparer.prepare(Config(), "feature/x", sandbox=True)
    assert not (path / BUNDLED[0]).exists()
    assert (path / BUNDLED[1] / "SKILL.md").read_text() == "tracked\n"
    warnings = [n.message for n in preparer.take_notes() if n.warning]
    assert len(warnings) == 2
    assert "re-included by .gitignore:1: !/.agents/skills/sbx-tui-config/" in warnings[0]
    assert f"{path / BUNDLED[0]}" in warnings[0]
    assert ".claude/skills/sbx-tui-config/SKILL.md is tracked in the bookmark" in warnings[1]


def test_opt_out_stops_deployment_but_keeps_copies(repo: Path) -> None:
    preparer = _preparer(repo)
    path = preparer.prepare(Config(), "feature/x", sandbox=True)
    rules = _excludes(repo)
    cfg = Config()
    cfg.project.deploy_config_skill = False
    add_bookmark(repo, "other", "o.txt", "o\n")
    other = preparer.prepare(cfg, "other", sandbox=True)
    assert not other.exists()  # project-sandbox creates it
    assert preparer.prepare(cfg, "other", sandbox=False) == other
    assert other.is_dir() and not (other / ".agents").exists()
    preparer.prepare(cfg, "feature/x", sandbox=True)
    assert all((path / t).is_dir() for t in BUNDLED)
    assert _excludes(repo) == rules


def test_non_colocated_repository(tmp_path: Path) -> None:
    repo = tmp_path / "plain"
    repo.mkdir()
    jj(repo, "git", "init", "--quiet", "--no-colocate")
    (repo / "a.txt").write_text("a\n")
    jj(repo, "commit", "--quiet", "-m", "first")
    jj(repo, "bookmark", "create", "--quiet", "main", "-r", "@-")
    jj(repo, "bookmark", "create", "--quiet", "feature/y", "-r", "@-")
    cfg = Config()
    cfg.project.worktree_dir = str(tmp_path / "trees")
    path = _preparer(repo).prepare(cfg, "feature/y", sandbox=True)
    assert path.parent == tmp_path / "trees"
    assert not (repo / ".git").exists()
    assert "/.agents/skills/sbx-tui-config/" in _excludes(repo)
    assert _snapshot(path) == ["a.txt"]


# ---------------------------------------------------------------- configured skills


def test_custom_skill_needs_ignore_rule_in_the_bookmark(repo: Path, tmp_path: Path) -> None:
    source = _skill(tmp_path)
    cfg = _custom_cfg(source)
    preparer = _preparer(repo)
    with pytest.raises(PreparationError) as caught:
        preparer.prepare(cfg, "feature/x", sandbox=True)
    path = workspace_path(repo, "feature/x", None)
    message = str(caught.value)
    assert message.startswith(f"feature/x: cannot deploy {source} to {path / CUSTOM}")
    assert "add /.agents/skills/release-review/ to its .gitignore" in message
    assert not (path / CUSTOM).exists()
    assert "/.agents/skills/release-review/" not in _excludes(repo)
    # The workspace was created and is reused once the bookmark ignores the skill.
    assert path.is_dir()
    (path / ".gitignore").write_text("/.agents/skills/release-review/\n")
    preparer.prepare(cfg, "feature/x", sandbox=True)
    assert (path / CUSTOM / "SKILL.md").is_file()
    helper = path / CUSTOM / "scripts" / "check.sh"
    assert os.access(helper, os.X_OK)
    assert stat.S_IMODE((path / CUSTOM / "notes.txt").stat().st_mode) & 0o111 == 0
    assert subprocess.run([helper], capture_output=True, text=True).stdout == "checked\n"
    assert _snapshot(path) == [".gitignore", "a.txt", "b.txt"]


def test_custom_skill_relative_source_and_both_roots(repo: Path) -> None:
    source = repo.parent / "shared-skills" / "review"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("review\n")
    targets = [".agents/skills/review", ".claude/skills/review"]
    _ignore_on_bookmark(repo, "feature/x", *[f"/{t}/" for t in targets])
    cfg = Config()
    cfg.workspace_skills = [WorkspaceSkill("../shared-skills/review", targets)]
    path = _preparer(repo).prepare(cfg, "feature/x", sandbox=True)
    assert all((path / t / "SKILL.md").read_text() == "review\n" for t in targets)
    assert _snapshot(path) == [".gitignore", "a.txt", "b.txt"]


def _replace_with_symlink(path: Path) -> None:
    import shutil

    shutil.rmtree(path)
    path.symlink_to(path.parent.parent, target_is_directory=True)


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda src, ws_: (src / "link").symlink_to(src / "notes.txt"), "is a symlink"),
        (lambda src, ws_: os.mkfifo(src / "fifo"), "is not a regular file"),
        (lambda src, ws_: (src / "SKILL.md").unlink(), "has no regular SKILL.md"),
        (lambda src, ws_: _replace_with_symlink(ws_ / ".agents"), ".agents is a symlink"),
        (
            lambda src, ws_: (ws_ / CUSTOM).mkdir(parents=True),
            "exists without a SKILL.md",
        ),
    ],
)
def test_custom_skill_rejects_unsafe_sources_and_destinations(
    repo: Path, tmp_path: Path, setup: Callable[[Path, Path], object], reason: str
) -> None:
    source = _skill(tmp_path)
    _ignore_on_bookmark(repo, "feature/x", f"/{CUSTOM}/")
    path = _preparer(repo).prepare(Config(), "feature/x", sandbox=False)
    setup(source, path)
    preparer = _preparer(repo)
    with pytest.raises(PreparationError, match="cannot deploy") as caught:
        preparer.prepare(_custom_cfg(source), "feature/x", sandbox=True)
    assert reason in str(caught.value)
    assert not (path / CUSTOM / "SKILL.md").exists()


def test_custom_skill_tracked_collision_is_preserved(repo: Path, tmp_path: Path) -> None:
    jj(repo, "new", "--quiet", "feature/x")
    (repo / CUSTOM).mkdir(parents=True)
    (repo / CUSTOM / "SKILL.md").write_text("tracked\n")
    jj(repo, "status")  # tracked first: ignoring it later does not untrack it
    (repo / ".gitignore").write_text(f"/{CUSTOM}/\n")
    jj(repo, "commit", "--quiet", "-m", "tracked skill, ignored later")
    jj(repo, "bookmark", "move", "--quiet", "feature/x", "--to", "@-")
    jj(repo, "new", "--quiet", "main")
    with pytest.raises(PreparationError, match="SKILL.md is tracked in the bookmark"):
        _preparer(repo).prepare(_custom_cfg(_skill(tmp_path)), "feature/x", sandbox=True)
    path = workspace_path(repo, "feature/x", None)
    assert (path / CUSTOM / "SKILL.md").read_text() == "tracked\n"


def test_failed_copy_leaves_nothing_and_can_be_retried(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _skill(tmp_path)
    _ignore_on_bookmark(repo, "feature/x", f"/{CUSTOM}/")
    copy = ws._copy_file
    calls = []

    def flaky(src: Path, dst: Path) -> None:
        calls.append(src)
        if src.name == "notes.txt":
            raise OSError(28, "No space left on device")
        copy(src, dst)

    monkeypatch.setattr(ws, "_copy_file", flaky)
    preparer = _preparer(repo)
    with pytest.raises(PreparationError, match="No space left on device"):
        preparer.prepare(_custom_cfg(source), "feature/x", sandbox=True)
    path = workspace_path(repo, "feature/x", None)
    assert not (path / CUSTOM).exists()
    assert not [p for p in (path / ".jj").iterdir() if p.name.startswith(ws.STAGING_PREFIX)]
    # The bundled copies published before the failure are complete and reused.
    assert all((path / t / "SKILL.md").is_file() for t in BUNDLED)
    monkeypatch.setattr(ws, "_copy_file", copy)
    preparer.prepare(_custom_cfg(source), "feature/x", sandbox=True)
    assert (path / CUSTOM / "notes.txt").read_text() == "notes\n"


# ---------------------------------------------------------------- launch options


def test_dry_run_plans_without_creating(repo: Path, tmp_path: Path) -> None:
    cfg = _custom_cfg(_skill(tmp_path))
    cfg.sandbox.extra_args = ["--dry-run"]
    preparer = _preparer(repo)
    path = preparer.prepare(cfg, "feature/x", sandbox=True)
    assert not path.exists()
    [note] = preparer.take_notes()
    assert note.message.startswith("feature/x: dry run, skills not deployed: ")
    assert str(path / CUSTOM) in note.message and str(path / BUNDLED[0]) in note.message
    assert "/.agents/skills/sbx-tui-config/" not in _excludes(repo)


@pytest.mark.parametrize("arg", ["--worktree-dir=/elsewhere", "--branch"])
def test_workspace_overrides_in_extra_args(repo: Path, tmp_path: Path, arg: str) -> None:
    preparer = _preparer(repo)
    path = preparer.prepare(Config(), "feature/x", sandbox=True, extra_args=[arg])
    assert not path.exists()
    [note] = preparer.take_notes()
    assert note.warning and "sbx-tui-config was not deployed" in note.message
    with pytest.raises(PreparationError, match="to deploy workspace skills"):
        preparer.prepare(_custom_cfg(_skill(tmp_path)), "feature/x", sandbox=True, extra_args=[arg])
    assert not path.exists()


def test_legacy_workspace_registration_blocks_sandbox_reuse(repo: Path) -> None:
    path = workspace_path(repo, "feature/x", None)
    path.parent.mkdir()
    jj(repo, "workspace", "add", "--quiet", "--name=feature/x", "-r", "feature/x", str(path))
    preparer = _preparer(repo)
    with pytest.raises(PreparationError, match="registered as 'feature/x'") as caught:
        preparer.prepare(Config(), "feature/x", sandbox=True)
    assert f"jj workspace rename {path.name}" in str(caught.value)
    assert not (path / BUNDLED[0]).exists()
    # Host agents and editors use the directory itself.
    preparer.prepare(Config(), "feature/x", sandbox=False)
    assert (path / BUNDLED[0] / "SKILL.md").is_file()

    stray = workspace_path(repo, "main", None)
    stray.mkdir()
    with pytest.raises(PreparationError, match="not a jj workspace of this repository"):
        preparer.prepare(Config(), "main", sandbox=True)


def test_working_copy_uses_the_checkout(repo: Path) -> None:
    assert _preparer(repo).prepare(Config(), "@", sandbox=True) == repo
    assert not (repo / ".agents").exists()


# ---------------------------------------------------------------- job manager


def _prepared_manager(repo: Path, cfg: Config) -> tuple[JobManager, WorkspacePreparer]:
    jobs = JobManager(repo, cfg)
    preparer = _preparer(repo)

    def prepare(current: Config, spec: LaunchSpec, sandbox: bool) -> None:
        preparer.prepare(current, spec.branch, sandbox=sandbox, extra_args=spec.extra_args)

    jobs.prepare = prepare
    return jobs, preparer


def test_job_manager_prepares_before_spawning(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    cfg = fake_cfg()
    jobs, _preparer_ = _prepared_manager(repo, cfg)
    spec = LaunchSpec("feature/x", "bash", mode="background", prompt_text="sleep:5")
    job = jobs.start_background(spec, {})
    path = workspace_path(repo, "feature/x", None)
    assert all((path / t / "SKILL.md").is_file() for t in BUNDLED)
    # Busy: rejected before preparation touches the workspace.
    jobs.prepare = lambda *_a: pytest.fail("prepared a busy workspace")
    with pytest.raises(BusyError):
        jobs.start_background(spec, {})
    with pytest.raises(BusyError):
        jobs.record_external(LaunchSpec("feature/x", "claude"), ["x"], host=True)
    jobs.cancel("feature/x", {})
    assert job.pid


def test_failed_preparation_starts_nothing_and_pauses_queue(
    repo: Path, tmp_path: Path, fake_cfg: Callable[..., Config]
) -> None:
    cfg = fake_cfg()
    cfg.workspace_skills = [WorkspaceSkill(str(_skill(tmp_path)), [CUSTOM])]
    jobs, _preparer_ = _prepared_manager(repo, cfg)
    spec = LaunchSpec("feature/x", "bash", mode="background", prompt_text="exit:0")
    with pytest.raises(PreparationError):
        jobs.start_background(spec, {})
    assert jobs.jobs == {} and not list(jobs.logs_dir.glob("*.out"))
    with pytest.raises(PreparationError), jobs.interactive(LaunchSpec("feature/x", "bash"), {}):
        pytest.fail("session started")
    with pytest.raises(PreparationError):
        jobs.record_external(LaunchSpec("feature/x", "bash"), ["x"])
    assert jobs.jobs == {}

    queue = TaskQueue(jobs.state_dir)
    queue.add(spec)
    assert queue.dispatch(jobs, dict, lambda _b: None) == []
    state = queue.get("feature/x")
    assert state.paused and "cannot deploy" in state.error and state.tasks
