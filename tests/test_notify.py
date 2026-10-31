from __future__ import annotations

import subprocess
from unittest.mock import Mock

import pytest

from project_sandbox_tui import notify


@pytest.mark.parametrize("title", ["t; rm -rf /", "literal {message}"])
def test_template_substitutes_once_without_splitting(title: str) -> None:
    message = "msg $(x) 'quoted'\nsecond line"
    assert notify.notification_argv(title, message, "notifier --title {title} -m {message}") == [
        "notifier",
        "--title",
        title,
        "-m",
        message,
    ]


def test_template_passes_text_as_script_argument() -> None:
    message = "done; $(rm -rf ~)"
    argv = notify.notification_argv("t", message, "sh -c 'notify-send sbx \"$1\"' sh {message}")
    assert argv == ["sh", "-c", 'notify-send sbx "$1"', "sh", message]


@pytest.mark.parametrize(
    ("system", "available", "expected"),
    [
        ("Linux", {"notify-send"}, ["notify-send", "--app-name=sbx-tui", "title", "message"]),
        ("Linux", set(), None),
        (
            "Darwin",
            {"osascript", "notify-send"},
            [
                "osascript",
                "-e",
                "on run argv",
                "-e",
                "display notification (item 2 of argv) with title (item 1 of argv)",
                "-e",
                "end run",
                "title",
                "message",
            ],
        ),
        ("Darwin", {"notify-send"}, ["notify-send", "--app-name=sbx-tui", "title", "message"]),
    ],
)
def test_platform_notifier_selection(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    available: set[str],
    expected: list[str] | None,
) -> None:
    monkeypatch.setattr(notify.platform, "system", lambda: system)
    monkeypatch.setattr(notify.shutil, "which", lambda name: name if name in available else None)
    assert notify.notification_argv("title", "message") == expected


@pytest.mark.parametrize("command", [" ", "'unclosed", "sh -c 'notify-send sbx \"{message}\"'"])
def test_empty_or_invalid_command_does_not_spawn(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    spawn = Mock()
    monkeypatch.setattr(notify.subprocess, "Popen", spawn)
    assert not notify.send("title", "message", command)
    spawn.assert_not_called()


def test_no_notifier_does_not_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "notification_argv", lambda *args: None)
    spawn = Mock()
    monkeypatch.setattr(notify.subprocess, "Popen", spawn)
    assert not notify.send("title", "message")
    spawn.assert_not_called()


@pytest.mark.parametrize("error", [None, OSError("cannot start")])
def test_send_detaches_and_handles_spawn_failure(
    monkeypatch: pytest.MonkeyPatch,
    error: OSError | None,
) -> None:
    spawn = Mock(side_effect=error)
    monkeypatch.setattr(notify.subprocess, "Popen", spawn)
    assert notify.send("title", "message", "notifier {title} {message}") is (error is None)
    spawn.assert_called_once_with(
        ["notifier", "title", "message"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
