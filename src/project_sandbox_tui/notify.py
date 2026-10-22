"""Desktop notifications (fire-and-forget, never raise)."""

from __future__ import annotations

import platform
import shlex
import shutil
import subprocess


def notification_argv(title: str, message: str, command: str = "") -> list[str] | None:
    """Build the notifier command line, or None if no notifier is available.

    A configured ``command`` is split with shlex *before* substituting
    ``{title}``/``{message}`` per token, so message text can never inject
    extra arguments or shell syntax.
    """
    if command:
        tokens = shlex.split(command)
        return [t.replace("{title}", title).replace("{message}", message) for t in tokens]
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
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return False
    return True
