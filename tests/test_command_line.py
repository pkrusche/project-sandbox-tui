"""The ``:`` command line and leader-key mappings in the running app."""

from __future__ import annotations

import contextlib
import json
import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, Static

from project_sandbox_tui import config, doctor, models
from project_sandbox_tui.app import SandboxTUI
from project_sandbox_tui.config import Config
from project_sandbox_tui.screens.command import CommandScreen
from project_sandbox_tui.screens.dialogs import ConfirmScreen, HelpScreen, MessageScreen
from project_sandbox_tui.vcs import VcsError
from tests.test_app import run_app, wait_for_screen, wait_until


def messages(app: SandboxTUI) -> list[str]:
    return [str(n.message) for n in app._notifications]


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Headless drivers cannot suspend; collect the Enter prompts instead."""
    prompts: list[str] = []
    monkeypatch.setattr("project_sandbox_tui.terminal.preserved", contextlib.nullcontext)
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "")
    return prompts


async def type_line(pilot: Any, line: str) -> None:
    await pilot.press("colon")
    assert isinstance(pilot.app.screen, CommandScreen)
    await pilot.press(*line)


@pytest.mark.parametrize("catalog_exists", [False, True])
def test_init_sets_up_repository_and_reloads_configuration(
    repo: Path,
    fake_cfg: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
    catalog_exists: bool,
) -> None:
    (repo / "pyproject.toml").touch()
    global_path = repo.parent / "custom-global.toml"
    global_path.write_text('[defaults]\nagent = "codex"\n[ui]\nnotify_command = "true"\n')
    sources: list[str] = []

    def update(source: str) -> Any:
        sources.append(source)
        raise models.ModelsError("offline")

    monkeypatch.setattr(models, "load_store", lambda: object() if catalog_exists else None)
    monkeypatch.setattr(models, "update", update)

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app.config_path = global_path
        app.marked = ["feature/x"]  # initialization always uses the repository root
        await type_line(pilot, "ini")
        assert isinstance(app.screen, CommandScreen) and "init" in app.screen.names
        await pilot.press("tab")
        assert app.screen.input.value == "init"
        await pilot.press("enter")
        report = await wait_for_screen(app, pilot, MessageScreen)
        await wait_until(pilot, lambda: "Configuration reloaded" in messages(app))
        assert report.title_text == "initialization"
        assert f"wrote: {config.project_config_path(repo)}" in report.body
        assert app.cfg.project.build == "python-uv"
        assert app.cfg.defaults.agent == "codex"
        assert app.jobs.cfg is app.cfg
        assert not config.ProjectTrust.check(repo).needs_review
        assert not config.global_config_path().exists()
        assert "feedback:start" in (repo / "AGENTS.md").read_text()
        for root in (".agents", ".claude"):
            for skill in ("sbx-tui-config", "sbx-run-feedback"):
                assert (repo / root / "skills" / skill / "SKILL.md").is_file()
        if not catalog_exists:
            await wait_until(pilot, lambda: "models: offline" in messages(app))
        assert sources == ([] if catalog_exists else [models.DEFAULT_SOURCE])
        await pilot.press("escape")
        assert app.screen is app.main and app.is_running

    run_app(repo, fake_cfg(), scenario)


def test_init_preserves_existing_config_and_customized_skills(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(models, "load_store", lambda: object())

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        project = config.project_config_path(repo)
        original = '[sandbox]\ncommand = "unreviewed-command"\n'
        project.write_text(original)
        skill = repo / ".agents/skills/sbx-tui-config/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("custom instructions\n")
        await type_line(pilot, "init")
        await pilot.press("enter")
        report = await wait_for_screen(app, pilot, MessageScreen)
        await wait_until(pilot, lambda: "Configuration reloaded" in messages(app))
        assert project.read_text() == original
        assert config.ProjectTrust.check(repo).needs_review
        assert app.cfg.sandbox.command == "project-sandbox"
        assert skill.read_text() == "custom instructions\n"
        assert "preserved customized or older skill" in report.body

    run_app(repo, fake_cfg(), scenario)


def test_init_reports_installation_failure(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(models, "load_store", lambda: object())
    (repo / ".agents").write_text("not a directory\n")
    cfg = fake_cfg()

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await type_line(pilot, "init")
        await pilot.press("enter")
        report = await wait_for_screen(app, pilot, MessageScreen)
        await wait_until(pilot, lambda: "Initialization failed:" in report.body)
        assert "could not install configuration skill" in report.body
        assert app.cfg is cfg
        assert "Configuration reloaded" not in messages(app)
        await pilot.press("escape")
        assert app.screen is app.main and app.is_running

    run_app(repo, cfg, scenario)


def test_doctor_explains_displayed_comparison_failures(
    repo: Path,
    fake_cfg: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = repo.parent / "doctor.toml"

    def run_checks(path: Path, global_path: Path | None) -> list[doctor.Check]:
        assert (path, global_path) == (repo, config_path)
        return [doctor.Check("container runtime", "fail", "cannot connect", "start Docker")]

    monkeypatch.setattr(doctor, "run_checks", run_checks)
    assert doctor.main(repo, config_path) == 1
    cli_output = capsys.readouterr().out.rstrip("\n")

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app.config_path = config_path
        app._refresh_timer.pause()
        original = app.backend._jj

        def fail_diff(*args: str) -> str:
            if args[0] == "diff":
                raise VcsError("failed", detail="jj diff: transient [error]\nfull diagnostic")
            return original(*args)

        app.backend._comparisons.clear()
        monkeypatch.setattr(app.backend, "_jj", fail_diff)
        await app.refresh_data()
        assert str(app.table.get_cell("feature/x", "vs main")) == "?"
        await type_line(pilot, "doctor")
        assert isinstance(app.screen, CommandScreen) and "doctor" in app.screen.names
        await pilot.press("enter")
        report = await wait_for_screen(app, pilot, MessageScreen)
        assert "? feature/x" in report.body
        assert "jj diff: transient [error]\nfull diagnostic" in report.body
        assert "main (" in report.body and str(repo) in report.body
        assert "R or :refresh retries" in report.body

        # The report keeps the failure even if the next periodic refresh succeeds.
        monkeypatch.setattr(app.backend, "_jj", original)
        await app.refresh_data()
        assert str(app.table.get_cell("feature/x", "vs main")) != "?"
        await wait_until(pilot, lambda: report.body.endswith(cli_output))
        assert cli_output in str(report.query_one("#message-text", Static).render())
        assert "transient [error]" in report.body
        await pilot.press("q")
        await type_line(pilot, "doctor")
        await pilot.press("enter")
        report = await wait_for_screen(app, pilot, MessageScreen)
        await wait_until(pilot, lambda: report.body.endswith(cli_output))
        assert "3/3 revisions available; 0 show ?" in report.body
        assert "transient" not in report.body
        await pilot.press("escape")
        assert app.screen is app.main

    run_app(repo, fake_cfg(), scenario)


def test_shell_command_runs_in_the_repository(
    repo: Path,
    fake_cfg: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
    terminal: list[str],
) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        monkeypatch.setattr(app, "_suspended", contextlib.nullcontext)
        await pilot.press("j")  # feature/x
        await type_line(pilot, '!printf "%s\\n" "$SBX_BOOKMARK" "$SBX_REPO" "$SBX_WORKSPACE" >out')
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is app.main
        assert (repo / "out").read_text().splitlines() == ["feature/x", str(repo), ""]
        assert terminal == ["\n── exited with 0; press Enter to return to sbx-tui ──"]

        await pilot.press("g", "g")  # the working copy's workspace is the repository
        await type_line(pilot, "!echo $SBX_WORKSPACE >out; exit 3")
        await pilot.press("enter")
        await pilot.pause()
        assert (repo / "out").read_text() == f"{repo}\n"
        assert any("exited with 3" in m for m in messages(app))
        assert app.command_history.newest_first()[:2] == [
            "!echo $SBX_WORKSPACE >out; exit 3",
            '!printf "%s\\n" "$SBX_BOOKMARK" "$SBX_REPO" "$SBX_WORKSPACE" >out',
        ]

    run_app(repo, fake_cfg(), scenario)


def test_configured_commands_take_arguments(
    repo: Path,
    fake_cfg: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
    terminal: list[str],
) -> None:
    cfg = fake_cfg()
    cfg.commands = {"save": "printf '%s\\n' >out"}

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        monkeypatch.setattr(app, "_suspended", contextlib.nullcontext)
        await type_line(pilot, "save a 'b c'")
        await pilot.press("enter")
        await pilot.pause()
        assert (repo / "out").read_text() == "a\nb c\n"

        await type_line(pilot, "nope")
        await pilot.press("enter")
        await pilot.pause()
        assert any("unknown command :nope" in m for m in messages(app))

        await type_line(pilot, "help")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        assert app.screen.extra == (("configured commands", ((":save", "printf '%s\\n' >out"),)),)

    run_app(repo, cfg, scenario)


@pytest.mark.parametrize("mode", ["inline", "custom"])
def test_host_command_launches_cli_with_arguments(
    repo: Path,
    fake_cfg: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
    terminal: list[str],
    mode: str,
) -> None:
    from project_sandbox_tui import launchers
    from project_sandbox_tui.config import LastUsed, remember

    cfg = fake_cfg()
    cfg.ui.terminal = mode
    cfg.ui.terminal_command = "terminal {cmd}"
    cfg.ui.host_agent_command = "unused-wrapper {agent}"
    script = repo.parent / "fake host agent.py"
    script.write_text(
        "import json, os, sys\n"
        "with open('host-args.json', 'w') as out:\n"
        "    json.dump([os.getcwd(), sys.argv[1:]], out)\n"
    )
    argv = [sys.executable, str(script), "--model", "my model", "", "$HOME; echo literal"]
    opened: list[tuple[launchers.ExternalLaunch, Path]] = []
    monkeypatch.setattr(launchers, "start", lambda launch, cwd: opened.append((launch, cwd)))
    remember(repo, "claude", "sandbox-model", "high", 300)
    last = LastUsed.load(repo)

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        monkeypatch.setattr(app, "_suspended", contextlib.nullcontext)
        await pilot.press("j")  # feature/x; create its workspace on demand
        path = app._workspace_dir("feature/x")
        assert not path.exists()
        await type_line(pilot, "host " + shlex.join(argv))
        await pilot.press("enter")
        await pilot.pause()
        job = app.jobs.jobs["feature/x"]
        assert job.host and job.external == (mode == "custom")
        assert job.spec.agent == sys.executable and job.argv == argv
        assert path.is_dir()
        remembered = LastUsed.load(repo)
        assert remembered.host_command == argv
        remembered.host_command = []
        assert remembered == last
        assert Path(argv[0]).name == app.main.cheatsheet().host_agent
        assert app.tasks.tasks == {}
        if mode == "inline":
            assert job.exit_code == 0 and not job.running
            assert json.loads((path / "host-args.json").read_text()) == [str(path), argv[2:]]
        else:
            launch, cwd = opened.pop()
            assert cwd == path
            assert launch.argv == [
                "terminal",
                shlex.join(launchers.host_command(job.launch_argv(), job.summary_path)),
            ]
            # The command uses the same busy reservation as the shortcut.
            await app.run_command("host other-agent")
            assert not opened
            assert app.jobs.jobs["feature/x"] is job
            assert LastUsed.load(repo).host_command == argv
            job.status = "completed"
            app.jobs.save(job)
        # I reuses the exact command, including quoted and empty arguments,
        # even with a configured wrapper. It also survives a TUI restart.
        await pilot.press("I")
        await pilot.pause()
        repeated = app.jobs.jobs["feature/x"]
        assert repeated is not job
        assert repeated.argv == argv and repeated.spec.agent == argv[0]
        assert LastUsed.load(repo).host_command == argv
        repeated.status = "completed"
        app.jobs.save(repeated)

    run_app(repo, cfg, scenario)

    async def restarted(app: SandboxTUI, pilot: Any) -> None:
        from project_sandbox_tui.screens.dialogs import MessageScreen

        monkeypatch.setattr(app, "_suspended", contextlib.nullcontext)
        assert Path(argv[0]).name == app.main.cheatsheet().host_agent
        await pilot.press("j", "P")
        assert isinstance(app.screen, MessageScreen)
        assert f"host agent command (I)\n{shlex.join(argv)}" in app.screen.body
        await pilot.press("escape", "I")
        assert app.jobs.jobs["feature/x"].argv == argv
        app.jobs.jobs["feature/x"].status = "completed"
        app.jobs.save(app.jobs.jobs["feature/x"])

    run_app(repo, cfg, restarted)


def test_host_command_uses_marked_targets(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[tuple[str, tuple[str, ...]]] = []

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        monkeypatch.setattr(app, "_open_host_agent", lambda name, argv: opened.append((name, argv)))
        app.marked = ["@", "feature/x"]
        await type_line(pilot, "host my-agent --option 'two words'")
        await pilot.press("enter")
        await pilot.pause()
        assert opened == [
            ("@", ("my-agent", "--option", "two words")),
            ("feature/x", ("my-agent", "--option", "two words")),
        ]

    run_app(repo, fake_cfg(), scenario)


def test_default_command_warns_before_running(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    from project_sandbox_tui.config import DEFAULT_COMMANDS
    from project_sandbox_tui.screens.dialogs import ConfirmScreen

    cfg = fake_cfg()
    cfg.commands = {**DEFAULT_COMMANDS, "plain": "true"}
    ran: list[str] = []

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        monkeypatch.setattr(app, "_run_shell", lambda command: ran.append(command.script))
        monkeypatch.setattr(app, "_run_background", lambda command: ran.append(command.line))
        await type_line(pilot, "abandon-empty")
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ConfirmScreen) and screen.title_text == "run :abandon-empty?"
        assert "all workspaces are inactive" in screen.body
        await pilot.press("n")
        await pilot.pause()
        assert ran == []

        await type_line(pilot, "&abandon-empty")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await pilot.pause()
        assert ran == ["&abandon-empty"]

        await type_line(pilot, "plain")  # commands without a warning run at once
        await pilot.press("enter")
        await pilot.pause()
        assert ran == ["&abandon-empty", "true"]

        app.cfg.commands["abandon-empty"] = "true"  # redefined: no warning
        await type_line(pilot, "abandon-empty")
        await pilot.press("enter")
        await pilot.pause()
        assert ran == ["&abandon-empty", "true", "true"]

    run_app(repo, cfg, scenario)


def test_command_line_completion_and_history(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    cfg = fake_cfg()
    cfg.commands = {"test": "true", "tidy": "true"}

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app.command_history.add("tidy now")
        app.command_history.add("!jj st")
        app.command_history.add("test -x")
        await type_line(pilot, "t")
        screen = app.screen
        assert isinstance(screen, CommandScreen)
        box = screen.query_one(Input)
        matches = screen.query_one("#matches", Static)
        await pilot.press("tab")
        assert box.value == "test"
        assert matches.has_class("visible")
        await pilot.press("tab")
        assert box.value == "tidy"
        await pilot.press("tab")
        assert box.value == "t"  # past the last match: what was typed
        await pilot.press("shift+tab")
        assert box.value == "tidy"
        await pilot.press("space")
        assert not matches.has_class("visible")  # typing ends the cycle

        await pilot.press("ctrl+u")  # Input: delete to the start
        await pilot.press("r", "e", "l", "tab")
        assert box.value == "reload"  # one match: completed at once

        box.value = "t"
        await pilot.pause()
        await pilot.press("up")
        assert box.value == "test -x"
        await pilot.press("up")
        assert box.value == "tidy now"  # only lines starting with "t"
        await pilot.press("up")
        assert box.value == "tidy now"  # the oldest match stays
        await pilot.press("down", "down")
        assert box.value == "t"

        box.value = ""
        await pilot.pause()
        await pilot.press("backspace")  # over the colon: cancel
        assert app.screen is app.main

        await type_line(pilot, "x")
        await pilot.press("escape")
        assert app.screen is app.main
        assert app.command_history.newest_first()[0] == "test -x"  # nothing ran

    run_app(repo, cfg, scenario)


def test_leader_mappings(
    repo: Path,
    fake_cfg: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
    terminal: list[str],
) -> None:
    cfg = fake_cfg()
    cfg.commands = {"mark": "echo marked >out"}
    cfg.leader = {"t": "mark", "gs": "!echo gs >out", "h": "help"}

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        monkeypatch.setattr(app, "_suspended", contextlib.nullcontext)
        header = app.main.query_one("#header", Static)
        await pilot.press("backslash")
        assert "\\…" in str(header.content)
        await pilot.press("t")
        await pilot.pause()
        assert (repo / "out").read_text() == "marked\n"
        assert app.command_history.entries == []  # mappings are not typed lines

        await pilot.press("backslash", "g")
        assert "\\g…" in str(header.content)
        await pilot.press("s")
        await pilot.pause()
        assert (repo / "out").read_text() == "gs\n"

        await pilot.press("backslash", "q")  # unmapped: q must not quit
        await pilot.pause()
        assert app.is_running and app.screen is app.main
        assert any("no leader mapping \\q" in m for m in messages(app))

        await pilot.press("backslash", "escape", "j")  # esc gives up; j moves again
        assert app.selected_branch_name() == "feature/x"

        await pilot.press("backslash", "h")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        assert (
            "leader mappings",
            (("\\t", ":mark"), ("\\gs", ":!echo gs >out"), ("\\h", ":help")),
        ) in app.screen.extra

    run_app(repo, cfg, scenario)


def mark(app: SandboxTUI, *names: str) -> None:
    app.marked = list(names)
    app._refresh_marks()


def test_each_runs_in_every_selected_workspace(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    from project_sandbox_tui.screens.dialogs import MessageScreen
    from project_sandbox_tui.screens.log import LogScreen
    from tests.test_app import row_texts, wait_until

    script = (
        'printf "%s\\n" "$SBX_BOOKMARK" "$SBX_WORKSPACE" "$PWD"; exit $((${#SBX_BOOKMARK} - 1))'
    )

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        mark(app, "@", "feature/x")
        await type_line(pilot, f"*!{script}")
        await pilot.press("enter")
        await wait_until(pilot, lambda: len(app.tasks.tasks) == 2)
        await wait_until(pilot, lambda: not app.tasks.running_count())
        await app.refresh_data()
        wc, feature = app.tasks.latest("@"), app.tasks.latest("feature/x")
        assert wc is not None and feature is not None
        assert Path(wc.out_path).read_text().splitlines() == ["@", str(repo), str(repo)]
        workspace = app._workspace_dir("feature/x")
        assert workspace.is_dir() and workspace != repo  # created on demand, like o
        lines = Path(feature.out_path).read_text().splitlines()
        assert lines == ["feature/x", str(workspace), str(workspace)]
        assert (wc.status, feature.status, feature.exit_code) == ("completed", "failed", 8)
        names = [r.branch.name for r in app.visible_rows()]
        rows = dict(zip(names, (r[-1] for r in row_texts(app)), strict=True))
        assert rows["@"].endswith(":printf ✓") and rows["feature/x"].endswith(":printf ✗8")
        assert any("feature/x: :*!" in m and "failed (8)" in m for m in messages(app))

        await pilot.press("escape")  # clear the marks; l and P act on the row
        app.table.move_cursor(row=[r.branch.name for r in app.visible_rows()].index("feature/x"))
        await pilot.press("l")
        assert isinstance(app.screen, LogScreen) and app.screen.current == Path(feature.out_path)
        await pilot.press("q", "P")
        assert isinstance(app.screen, MessageScreen)
        assert "tasks (:& / :*)" in app.screen.body and "failed (8)" in app.screen.body

    run_app(repo, fake_cfg(), scenario)


def test_each_cancel_and_leader(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    from tests.test_app import wait_until

    cfg = fake_cfg()
    cfg.commands = {"wait": "echo ready; exec sleep 30"}
    cfg.leader = {"W": "*wait"}

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j")  # feature/x
        await pilot.press("backslash", "W")
        await wait_until(pilot, lambda: app.tasks.running_count() == 1)
        await wait_until(
            pilot, lambda: "1 task" in str(app.main.query_one("#header", Static).content)
        )
        task = app.tasks.latest("feature/x")
        assert task is not None
        output = Path(task.out_path)
        await wait_until(pilot, lambda: output.read_text() == "ready\n")
        await pilot.press("x")
        await wait_until(pilot, lambda: not app.tasks.running_count())
        task = app.tasks.latest("feature/x")
        assert task is not None and task.status == "interrupted"
        assert any("sent SIGINT to :*wait" in m for m in messages(app))

    run_app(repo, cfg, scenario)


@pytest.mark.parametrize("configured", [False, True])
def test_background_runs_once_in_repository(
    repo: Path, fake_cfg: Callable[..., Config], configured: bool
) -> None:
    from project_sandbox_tui.screens.log import LogScreen
    from project_sandbox_tui.screens.tasks import TasksScreen
    from tests.test_app import wait_until

    script = 'printf "%s\\n" "$SBX_REPO" "$SBX_BOOKMARK" "$SBX_WORKSPACE" "$PWD"; exit 7'
    cfg = fake_cfg()
    cfg.commands = {"check": script}

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j")  # environment still describes the highlighted bookmark
        mark(app, "@", "feature/x")
        line = "&check" if configured else f"&!{script}"
        await type_line(pilot, line)
        await pilot.press("enter")
        await wait_until(pilot, lambda: len(app.tasks.tasks) == 1)
        assert app.screen is app.main  # no suspension or Enter prompt
        await wait_until(pilot, lambda: not app.tasks.running_count())
        task = app.tasks.latest("@")
        assert task is not None and task.line == line
        assert task.cwd == str(repo)
        assert (task.status, task.exit_code) == ("failed", 7)
        assert task.name == ("check" if configured else "printf")
        assert Path(task.out_path).read_text().splitlines() == [
            str(repo),
            "feature/x",
            "",
            str(repo),
        ]
        assert not app._workspace_dir("feature/x").exists()
        assert not app.tasks.for_bookmark("feature/x")
        assert app.command_history.newest_first()[0] == line
        await pilot.press("T")
        assert isinstance(app.screen, TasksScreen)
        await pilot.press("enter")
        assert isinstance(app.screen, LogScreen) and app.screen.current == Path(task.out_path)

    run_app(repo, cfg, scenario)


def test_background_leader_task_can_be_cancelled(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    from tests.test_app import wait_until

    cfg = fake_cfg()
    cfg.commands = {"wait": "echo ready; sleep 30"}
    cfg.leader = {"b": "&wait"}

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j", "backslash", "b")
        await wait_until(pilot, lambda: app.tasks.latest("@") is not None)
        task = app.tasks.latest("@")
        assert task is not None
        await wait_until(pilot, lambda: "ready" in Path(task.out_path).read_text())
        await pilot.press("T", "x")
        await wait_until(pilot, lambda: not app.tasks.running_count())
        assert task.status == "interrupted"
        assert any("sent SIGINT to :&wait" in m for m in messages(app))

    run_app(repo, cfg, scenario)


def test_each_rejects_builtins(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await type_line(pilot, "*refresh")
        await pilot.press("enter")
        await pilot.pause()
        assert any("cannot run per workspace" in m for m in messages(app))
        assert not app.tasks.tasks

    run_app(repo, fake_cfg(), scenario)


def test_actions_start_a_run_on_each_target(
    repo: Path, fake_cfg: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    from project_sandbox_tui.config import Action, LastUsed
    from tests.test_app import wait_until

    cfg = fake_cfg()
    cfg.defaults.agent = "bash"
    cfg.actions = {"chores": Action(prompt="chores on {bookmark} vs {main}", effort="low")}
    cfg.leader = {"rc": "chores sleep:1"}

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        # Launches may outlast a notification's display time on parallel runs.
        notices: list[str] = []
        notify = app.notify

        def record_notify(message: str, **kwargs: Any) -> None:
            notices.append(message)
            notify(message, **kwargs)

        monkeypatch.setattr(app, "notify", record_notify)
        mark(app, "@", "main", "feature/x")
        await type_line(pilot, "chores sleep:0.2")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await wait_until(pilot, lambda: "feature/x" in app.jobs.jobs)
        assert set(app.jobs.jobs) == {"@", "feature/x"}
        assert any("skipped: main is protected; create a bookmark" in m for m in notices)
        working_copy = app.jobs.jobs["@"]
        assert working_copy.spec.main_checkout
        assert working_copy.spec.prompt_text == "chores on @ vs main\n\nsleep:0.2\n"
        assert not any(arg.startswith("--branch") for arg in working_copy.argv)
        job = app.jobs.jobs["feature/x"]
        assert job.mode == "background" and (job.spec.agent, job.spec.effort) == ("bash", "low")
        assert job.spec.prompt_text == "chores on feature/x vs main\n\nsleep:0.2\n"
        assert app.selection() == []  # cleared, as after a merge
        assert app.prompt_history.newest_first() == []  # actions are not typed prompts
        assert LastUsed.load(repo).agent == ""  # nor launch-form choices
        await wait_until(pilot, lambda: not app.jobs.running_count(), timeout=15)

        # One target that is not main: no question. Busy targets accept queued actions.
        names = [r.branch.name for r in app.visible_rows()]
        app.table.move_cursor(row=names.index("feature/x"))
        await pilot.press("backslash", "r", "c")
        await wait_until(
            pilot, lambda: app.jobs.jobs["feature/x"].spec.prompt_text.endswith("sleep:1\n")
        )
        assert app.jobs.jobs["feature/x"].running
        app.queue.pause("feature/x", True)
        await pilot.press("backslash", "r", "c")
        await pilot.pause()
        assert any("feature/x: prompt queued" in m for m in notices)
        assert len(app.queue.get("feature/x").tasks) == 1

        # The main bookmark is blocked even when it is the only target.
        await wait_until(pilot, lambda: not app.jobs.running_count(), timeout=15)
        app.table.move_cursor(row=names.index("main"))
        notices.clear()
        await pilot.press("backslash", "r", "c")
        await pilot.pause()
        assert app.screen is app.main
        assert any("main is protected; create a bookmark" in m for m in notices)
        assert "main" not in app.jobs.jobs

    run_app(repo, cfg, scenario)


def test_action_profile_and_remembered_fallback(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    from project_sandbox_tui.config import Action, AgentProfile, LastUsed
    from project_sandbox_tui.screens.launch import LaunchScreen
    from project_sandbox_tui.widgets import CycleField

    cfg = fake_cfg()
    cfg.sandbox.extra_args = ["--shared-flag"]
    cfg.profiles = {
        "fast": AgentProfile(agent="bash", extra_args=["--fast-flag"]),
        "deep": AgentProfile(agent="bash", effort="high", extra_args=["--memory=16g"]),
    }
    cfg.actions = {
        "review": Action(prompt="review {bookmark}", profile="deep", timeout=600),
        "check": Action(prompt="check {bookmark}"),
        "broken": Action(prompt="broken {bookmark}", profile="gone"),
    }
    LastUsed(profile="fast").save(repo)

    async def run(app: SandboxTUI, pilot: Any, line: str) -> list[str]:
        await wait_until(pilot, lambda: not app.jobs.running_count(), timeout=15)
        app.jobs.jobs.pop("feature/x", None)
        await app.run_command(line)
        await wait_until(pilot, lambda: "feature/x" in app.jobs.jobs)
        return app.jobs.jobs["feature/x"].argv

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        mark(app, "@", "feature/x")
        await app.run_command(":review")
        confirm = await wait_for_screen(app, pilot, ConfirmScreen)
        assert "unsupervised bash/high run with profile deep" in confirm.body
        await pilot.press("n")
        await pilot.press("escape")
        app.clear_branch_selection()
        await pilot.press("j")
        assert app.selected_branch_name() == "feature/x"

        argv = await run(app, pilot, ":review")
        spec = app.jobs.jobs["feature/x"].spec
        assert (spec.agent, spec.effort, spec.timeout, spec.profile) == (
            "bash",
            "high",
            600,
            "deep",
        )
        assert argv.index("--shared-flag") < argv.index("--memory=16g")

        argv = await run(app, pilot, ":check")
        assert app.jobs.jobs["feature/x"].spec.profile == "fast" and "--fast-flag" in argv

        argv = await run(app, pilot, ":broken")
        assert any(
            "actions.broken refers to unknown profile 'gone'; using fast" in m
            for m in messages(app)
        )
        assert app.jobs.jobs["feature/x"].spec.profile == "fast" and "--fast-flag" in argv

        # Action runs leave the launch forms' remembered profile alone.
        await wait_until(pilot, lambda: not app.jobs.running_count(), timeout=15)
        assert LastUsed.load(repo).profile == "fast"
        await pilot.press("r")
        form = await wait_for_screen(app, pilot, LaunchScreen)
        assert form.query_one("#profile", CycleField).value == "fast"

    run_app(repo, cfg, scenario)


def test_open_editor_on_each_selected_workspace(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    from tests.conftest import wait_for

    cfg = fake_cfg()
    cfg.ui.editor_command = "sh -c 'touch \"$1/opened\"' sh {path}"

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        mark(app, "@", "feature/x")
        await pilot.press("o")
        targets = [repo, app._workspace_dir("feature/x")]
        wait_for(lambda: all((p / "opened").exists() for p in targets))

    run_app(repo, cfg, scenario)


def test_delete_and_move_ask_while_tasks_run(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    from project_sandbox_tui.screens.dialogs import ConfirmScreen
    from project_sandbox_tui.vcs import detect
    from tests.test_app import wait_until

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        names = [r.branch.name for r in app.visible_rows()]
        mark(app, "@", "feature/x")
        await type_line(pilot, "*!sleep 30")
        await pilot.press("enter")
        await wait_until(pilot, lambda: app.tasks.running_count() == 2)

        app.table.move_cursor(row=names.index("feature/x"))
        await pilot.press("escape", "d", "d")
        screen = app.screen
        assert isinstance(screen, ConfirmScreen) and screen.title_text == "delete feature/x?"
        assert "feature/x  :*!sleep 30" in screen.body
        await pilot.press("n")  # anything but y keeps it
        assert "feature/x" in {b.name for b in detect(repo).list_branches()}

        await pilot.press("at")  # @'s task runs in the repository
        screen = app.screen
        assert isinstance(screen, ConfirmScreen) and "@  :*!sleep 30" in screen.body
        await pilot.press("n")

        await pilot.press("d", "d")
        await pilot.press("y")
        await wait_until(
            pilot, lambda: "feature/x" not in {b.name for b in detect(repo).list_branches()}
        )
        assert app.tasks.running("feature/x")  # the task is left alone
        for name in ("@", "feature/x"):
            app.tasks.cancel(name)
        await wait_until(pilot, lambda: not app.tasks.running_count())  # refreshed on a timer

    run_app(repo, fake_cfg(), scenario)


def test_tasks_view(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    from project_sandbox_tui.screens.dialogs import MessageScreen
    from project_sandbox_tui.screens.log import LogScreen
    from project_sandbox_tui.screens.tasks import TasksScreen
    from tests.test_app import wait_until

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("T")  # nothing yet
        screen = app.screen
        assert isinstance(screen, TasksScreen)
        assert screen.query_one("#tasks-empty", Static).display
        await pilot.press("q")

        mark(app, "@")
        await type_line(pilot, "*!echo done")
        await pilot.press("enter")
        await wait_until(pilot, lambda: bool(app.tasks.tasks) and not app.tasks.running_count())
        mark(app, "feature/x")
        await type_line(pilot, "*!sleep 30")
        await pilot.press("enter")
        await wait_until(pilot, lambda: app.tasks.running_count() == 1)
        await pilot.press("escape")
        names = [r.branch.name for r in app.visible_rows()]
        app.table.move_cursor(row=names.index("@"))

        await pilot.press("T")
        screen = app.screen
        assert isinstance(screen, TasksScreen)
        table = screen.table
        rows = [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]
        assert [r[2:] for r in rows] == [
            ["feature/x", "running", ":*!sleep 30"],  # running first
            ["@", "completed", ":*!echo done"],
        ]
        assert "1 running in 1 workspace · 1 finished" in str(
            screen.query_one("#tasks-title", Static).content
        )
        assert screen.selected_task() is app.tasks.latest("@")  # the highlighted row's

        await pilot.press("l")
        log = app.screen
        assert isinstance(log, LogScreen) and log.current is not None
        await wait_until(pilot, lambda: "done" in log.current.read_text())
        await pilot.press("q", "P")
        assert isinstance(app.screen, MessageScreen)
        assert f"directory   {repo}" in app.screen.body
        await pilot.press("q")

        await pilot.press("k", "x")  # the running task: SIGINT
        await wait_until(pilot, lambda: not app.tasks.running_count())
        assert app.tasks.latest("feature/x").status == "interrupted"
        done = app.tasks.latest("@")
        assert done is not None
        app.screen.table.move_cursor(
            row=[str(k.value) for k in app.screen.table.rows].index(done.id)
        )
        await pilot.press("x")  # a finished task: forgotten, with its output
        assert app.tasks.latest("@") is None and not Path(done.out_path).exists()
        await pilot.press("T")
        assert app.screen is app.main

    run_app(repo, fake_cfg(), scenario)


def test_task_table_keeps_viewport_on_refresh_and_resize(
    repo: Path, fake_cfg: Callable[..., Config]
) -> None:
    from project_sandbox_tui.screens.dialogs import MessageScreen
    from project_sandbox_tui.screens.tasks import TasksScreen
    from project_sandbox_tui.tasks import TaskRecord
    from project_sandbox_tui.widgets import TABLE_PREVIEW_WIDTH

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        app._refresh_timer.pause()
        await app._refreshed.wait()
        tasks = app.tasks.tasks
        for i in range(24):
            name = f"task-{i:02}"
            command = "echo " + "x" * (140 if i == 0 else 53)
            tasks[name] = TaskRecord(
                name,
                name,
                f"&!{command}",
                command,
                str(repo),
                1,
                f"2026-01-01T00:00:{23 - i:02}+00:00",
                "",
                "",
                status="completed",
            )

        await pilot.press("T")
        screen = await wait_for_screen(app, pilot, TasksScreen)
        table = screen.table
        preview = table.get_cell("task-00", "command")
        assert preview.cell_len == TABLE_PREVIEW_WIDTH and preview.plain.endswith("…")
        await pilot.press("s")
        modal = await wait_for_screen(app, pilot, MessageScreen)
        assert modal.body == f":{tasks['task-00'].line}"
        await pilot.press("q")
        await pilot.press("G", "k", "k")
        selected = screen.selected_id()
        assert selected is not None
        offset = table.scroll_y
        assert offset > 0
        table.scroll_to(x=8, animate=False)
        tasks[selected].status = "failed"
        screen._render_rows()
        await pilot.pause()
        assert screen.selected_id() == selected
        assert table.scroll_y == offset
        assert table.scroll_x == table.scroll_target_x == 8

        for size in ((65, 12), (80, 12), (80, 7)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
            assert screen.selected_id() == selected
            assert table.scroll_x == table.scroll_target_x == 8
            if size[1] == 12:
                assert table.scroll_y == offset
            line = table.cursor_row - table.scroll_offset.y + 1
            assert 1 <= line < table.scrollable_content_region.height
            assert selected in table.render_line(line).text
            await pilot.press("k", "j")

        offset = table.max_scroll_x
        table.scroll_to(x=offset, animate=False)
        del tasks["task-00"]
        screen._render_rows()
        await pilot.pause()
        assert screen.selected_id() == selected
        assert 0 < table.max_scroll_x < offset
        assert table.scroll_x == table.scroll_target_x == table.max_scroll_x

    run_app(repo, fake_cfg(), scenario, size=(80, 12))
