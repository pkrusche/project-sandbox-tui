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

from project_sandbox_tui.app import SandboxTUI
from project_sandbox_tui.config import Config
from project_sandbox_tui.screens.command import CommandScreen
from project_sandbox_tui.screens.dialogs import ConfirmScreen, HelpScreen
from tests.test_app import run_app


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
        assert LastUsed.load(repo) == last
        assert app.tasks.tasks == {}
        if mode == "inline":
            assert job.exit_code == 0 and not job.running
            assert json.loads((path / "host-args.json").read_text()) == [str(path), argv[2:]]
        else:
            launch, cwd = opened.pop()
            assert cwd == path
            assert launch.argv == [
                "terminal",
                shlex.join(launchers.host_command(argv, job.summary_path)),
            ]
            # The command uses the same busy reservation as the shortcut.
            await app.run_command("host other-agent")
            assert not opened
            assert app.jobs.jobs["feature/x"] is job
            job.status = "completed"
            app.jobs.save(job)

    run_app(repo, cfg, scenario)


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
    cfg.commands = {"wait": "sleep 30"}
    cfg.leader = {"W": "*wait"}

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        await pilot.press("j")  # feature/x
        await pilot.press("backslash", "W")
        await wait_until(pilot, lambda: app.tasks.running_count() == 1)
        assert "1 task" in str(app.main.query_one("#header", Static).content)
        await pilot.pause(0.2)
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


def test_actions_start_a_run_on_each_target(repo: Path, fake_cfg: Callable[..., Config]) -> None:
    from project_sandbox_tui.config import Action, LastUsed
    from tests.test_app import wait_until

    cfg = fake_cfg()
    cfg.defaults.agent = "bash"
    cfg.actions = {"chores": Action(prompt="chores on {bookmark} vs {main}", effort="low")}
    cfg.leader = {"rc": "chores sleep:1"}

    async def scenario(app: SandboxTUI, pilot: Any) -> None:
        mark(app, "@", "main", "feature/x")
        await type_line(pilot, "chores sleep:0.2")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await wait_until(pilot, lambda: "feature/x" in app.jobs.jobs)
        assert set(app.jobs.jobs) == {"@", "feature/x"}
        assert any("skipped: main is protected; create a bookmark" in m for m in messages(app))
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

        # One target that is not main: no question. Busy targets are skipped.
        names = [r.branch.name for r in app.visible_rows()]
        app.table.move_cursor(row=names.index("feature/x"))
        await pilot.press("backslash", "r", "c")
        await wait_until(
            pilot, lambda: app.jobs.jobs["feature/x"].spec.prompt_text.endswith("sleep:1\n")
        )
        assert app.jobs.jobs["feature/x"].running
        await pilot.press("backslash", "r", "c")
        await pilot.pause()
        assert any("skipped: feature/x already has" in m for m in messages(app))

        # The main bookmark is blocked even when it is the only target.
        await wait_until(pilot, lambda: not app.jobs.running_count(), timeout=15)
        app.table.move_cursor(row=names.index("main"))
        await pilot.press("backslash", "r", "c")
        await pilot.pause()
        assert app.screen is app.main
        assert any("main is protected; create a bookmark" in m for m in messages(app))
        assert "main" not in app.jobs.jobs

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
