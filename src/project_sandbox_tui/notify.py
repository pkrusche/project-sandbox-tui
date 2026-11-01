"""Desktop notifications (fire-and-forget, never raise)."""

from __future__ import annotations

import platform
import shutil
import subprocess

from project_sandbox_tui.launchers import fill_command
from project_sandbox_tui.processes import reap_later


def notification_argv(title: str, message: str, command: str = "") -> list[str] | None:
    """Build the notifier command line, or None if no notifier is available.

    A configured ``command`` is split with shlex *before* substituting
    ``{title}``/``{message}`` per token, so message text can never inject
    extra arguments. Embedded placeholders are rejected; shell scripts should
    take values as positional arguments (see :func:`launchers.fill_command`).
    """
    if command:
        return fill_command(command, {"title": title, "message": message})
    if platform.system() == "Darwin" and shutil.which("osascript"):
        # Pass text as argv so quotes in branch names cannot break the script.
        return [
            "osascript",
            "-e",
            "on run argv",
            "-e",
            "display notification (item 2 of argv) with title (item 1 of argv)",
            "-e",
            "end run",
            title,
            message,
        ]
    if shutil.which("notify-send"):
        return ["notify-send", "--app-name=sbx-tui", title, message]
    return None


def send(title: str, message: str, command: str = "") -> bool:
    try:
        argv = notification_argv(title, message, command)
        if not argv:
            return False
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        reap_later(proc)
    except (OSError, ValueError):
        return False
    return True
