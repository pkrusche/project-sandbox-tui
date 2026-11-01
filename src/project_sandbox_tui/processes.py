"""Collect detached children's exit status without delaying the TUI or its exit."""

from __future__ import annotations

import subprocess
import threading


def reap_later(proc: subprocess.Popen[bytes]) -> None:
    """Keep ownership until the child exits, even if its job record is forgotten.

    Dropping Popen only reaps on a later spawn, leaving the last child a zombie.
    A daemon waiter also lets an editor or a detached job outlive the TUI.
    """
    if proc.poll() is None:
        threading.Thread(target=proc.wait, name="sbx-child-wait", daemon=True).start()
