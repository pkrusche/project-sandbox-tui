"""Minimal vim/ranger-style TUI for project-sandbox."""

import sys

__version__ = "0.1.0"

if sys.platform == "win32":  # fcntl / termios / pty: fail clearly instead of ImportError
    raise SystemExit("sbx-tui runs on Linux and macOS only; on Windows, use it inside WSL.")
