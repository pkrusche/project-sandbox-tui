from __future__ import annotations

import os
from pathlib import Path

import pytest

from project_sandbox_tui import commands as cmd

CONFIGURED = {"test": "uv run pytest", "lint": "uv run ruff check ."}


@pytest.mark.parametrize(
    ("line", "builtin", "script"),
    [
        ("!make lint", "", "make lint"),
        (":! ls -l ", "", "ls -l"),
        ("test", "", "uv run pytest"),
        ("test -k 'a b'", "", "uv run pytest -k 'a b'"),
        ("test\t-k 'a b'", "", "uv run pytest -k 'a b'"),
        ("  lint  ", "", "uv run ruff check ."),
        ("q", "quit", ""),
        ("quit", "quit", ""),
        ("help", "help", ""),
        ("reload", "reload", ""),
    ],
)
def test_parse(line: str, builtin: str, script: str) -> None:
    command = cmd.parse(line, CONFIGURED)
    assert (command.builtin, command.script) == (builtin, script)


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("", "empty command"),
        (":", "empty command"),
        ("!", "needs a shell command"),
        ("pytest", "unknown command :pytest"),
        ("quit now", "takes no arguments"),
    ],
)
def test_parse_errors(line: str, message: str) -> None:
    with pytest.raises(cmd.CommandError, match=message):
        cmd.parse(line, CONFIGURED)


def test_validate_accepts_mappings_to_any_command_line() -> None:
    mappings = {"t": "test", "tf": "test -x", "s": "!jj st", "r": ":reload"}
    with pytest.raises(cmd.CommandError, match="prefix"):
        cmd.validate(CONFIGURED, "\\", mappings)
    del mappings["t"]
    cmd.validate(CONFIGURED, "\\", mappings)
    cmd.validate(CONFIGURED, " ", {"x": "q"})
    cmd.validate({}, "", {})


@pytest.mark.parametrize(
    ("commands", "leader", "mappings", "message"),
    [
        ({"1x": "true"}, "\\", {}, "must start with a letter"),
        ({"a b": "true"}, "\\", {}, "must start with a letter"),
        ({"quit": "true"}, "\\", {}, "built-in"),
        ({"q": "true"}, "\\", {}, "built-in"),
        ({"x": "  "}, "\\", {}, "must not be empty"),
        ({}, "ab", {}, "ui.leader"),
        ({}, ":", {}, "ui.leader"),
        ({}, "\t", {}, "ui.leader"),
        ({}, "", {"t": "q"}, "need ui.leader"),
        ({}, "\\", {"": "q"}, "printable"),
        ({}, "\\", {"t": "nope"}, "unknown command :nope"),
        ({}, "\\", {"t": ""}, "empty command"),
    ],
)
def test_validate_errors(
    commands: dict[str, str], leader: str, mappings: dict[str, str], message: str
) -> None:
    with pytest.raises(cmd.CommandError, match=message):
        cmd.validate(commands, leader, mappings)


def test_leader_keys_wait_for_a_complete_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    mappings = {"t": "test", "gs": "!jj st", "gl": "!jj log"}
    keys = cmd.LeaderKeys()
    assert keys.pending is None
    keys.start()
    assert keys.pending == ""
    assert keys.feed("t", mappings) == "test"
    assert keys.pending is None

    keys.start()
    assert keys.feed("g", mappings) is None
    assert keys.pending == "g"
    assert keys.feed("l", mappings) == "!jj log"

    keys.start()
    assert keys.feed("x", mappings) is None
    assert keys.pending is None  # unmapped: the sequence is over

    now = 100.0
    monkeypatch.setattr("time.monotonic", lambda: now)
    keys.start()
    keys.feed("g", mappings)
    now += cmd.LeaderKeys.TIMEOUT
    assert keys.pending is None  # expired


def test_complete_command_names() -> None:
    names = [*cmd.BUILTINS, "test", "tidy"]
    assert cmd.complete("t", names, Path()) == (0, ["test", "tidy"])
    assert cmd.complete("", names, Path())[1] == sorted(names)
    assert cmd.complete("x", names, Path()) == (0, [])


def test_complete_programs_after_bang(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, mode in (("mytool", 0o755), ("mytext", 0o644), ("other", 0o755)):
        (bin_dir / name).write_text("")
        (bin_dir / name).chmod(mode)
    (bin_dir / "mydir").mkdir()
    path = f"{bin_dir}{os.pathsep}{tmp_path / 'missing'}"
    assert cmd.complete("!my", [], tmp_path, path) == (1, ["mytool"])
    assert cmd.complete("!", [], tmp_path, path) == (1, [])


def test_complete_file_arguments(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("")
    (tmp_path / "tests" / "test b.py").write_text("")
    (tmp_path / "tox.ini").write_text("")
    (tmp_path / ".hidden").write_text("")
    assert cmd.complete("test t", [], tmp_path) == (5, ["tests/", "tox.ini"])
    assert cmd.complete("test tests/test_", [], tmp_path) == (5, ["tests/test_a.py"])
    assert cmd.complete("test tests/", [], tmp_path)[1] == ["'tests/test b.py'", "tests/test_a.py"]
    assert cmd.complete("!cat ", [], tmp_path) == (5, ["tests/", "tox.ini"])
    assert cmd.complete("!cat .", [], tmp_path) == (5, [".hidden"])
    assert cmd.complete("!./t", [], tmp_path) == (1, ["./tests/", "./tox.ini"])
    assert cmd.complete(f"!ls {tmp_path}/to", [], Path("/")) == (4, [f"{tmp_path}/tox.ini"])
    assert cmd.complete("test missing/x", [], tmp_path) == (5, [])


@pytest.mark.parametrize("typed", ["'some dir'/fi", '"some dir/fi', "some\\ dir/fi"])
def test_complete_paths_with_shell_quoting(tmp_path: Path, typed: str) -> None:
    directory = tmp_path / "some dir"
    directory.mkdir()
    (directory / "file.txt").write_text("")
    assert cmd.complete(f"!cat {typed}", [], tmp_path) == (5, ["'some dir/file.txt'"])


def test_complete_programs_uses_repository_for_relative_path_entries(tmp_path: Path) -> None:
    program = tmp_path / "local tool"
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    for path in (".", ""):
        assert cmd.complete("!loc", [], tmp_path, path=path) == (1, ["'local tool'"])


@pytest.mark.parametrize(
    ("line", "each", "script", "action", "args"),
    [
        ("*!make lint", True, "make lint", "", ""),
        ("* !make", True, "make", "", ""),
        (":*test -k x", True, "uv run pytest -k x", "", ""),
        ("review", True, "", "review", ""),
        ("review focus on  errors", True, "", "review", "focus on  errors"),
        ("*review", True, "", "review", ""),
        ("!make", False, "make", "", ""),
    ],
)
def test_parse_each_and_actions(line: str, each: bool, script: str, action: str, args: str) -> None:
    command = cmd.parse(line, CONFIGURED, {"review"})
    assert (command.each, command.script, command.action, command.args) == (
        each,
        script,
        action,
        args,
    )


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("*", ":\\* needs a command"),
        ("*!", "needs a shell command"),
        ("*quit", "cannot run per workspace"),
        ("review", "unknown command :review"),  # not configured here
    ],
)
def test_parse_each_errors(line: str, message: str) -> None:
    with pytest.raises(cmd.CommandError, match=message):
        cmd.parse(line, CONFIGURED)


def test_validate_actions() -> None:
    cmd.validate(CONFIGURED, "\\", {"T": "*test", "rv": "review x"}, {"review"})
    with pytest.raises(cmd.CommandError, match=r"\[actions\] test is also in \[commands\]"):
        cmd.validate(CONFIGURED, "\\", {}, {"test"})
    with pytest.raises(cmd.CommandError, match="built-in"):
        cmd.validate({}, "\\", {}, {"help"})
    with pytest.raises(cmd.CommandError, match="must start with a letter"):
        cmd.validate({}, "\\", {}, {"re view"})


def test_complete_after_each(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("")
    assert cmd.complete("*te", ["test", "help"], tmp_path) == (1, ["test"])
    assert cmd.complete("* te", ["test"], tmp_path) == (2, ["test"])
    assert cmd.complete("*test no", ["test"], tmp_path) == (6, ["notes.txt"])
    start, words = cmd.complete("*!ech", [], tmp_path, path=str(tmp_path))
    assert start == 2 and words == []
