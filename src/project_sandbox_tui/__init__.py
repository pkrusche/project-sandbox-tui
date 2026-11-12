"""Minimal vim/ranger-style TUI for project-sandbox."""

import sys
from importlib.metadata import version

__version__ = version("project-sandbox-tui")

if sys.platform == "win32":  # fcntl / termios / pty: fail clearly instead of ImportError
    raise SystemExit("sbx-tui runs on Linux and macOS only; on Windows, use it inside WSL.")
