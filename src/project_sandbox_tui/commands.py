"""Command lines typed after ``:``, and leader-key mappings that run them.

- ``:!CMD`` runs ``CMD`` with ``sh -c`` in the repository, like vim's ``:!``.
- ``:NAME [ARGS]`` runs the ``[commands]`` entry ``NAME`` with ``ARGS``
  appended, e.g. ``test = "uv run pytest"`` makes ``:test -k merge`` run
  ``uv run pytest -k merge``.
- Built-in commands control the TUI itself (:data:`BUILTINS`).

``[leader]`` maps keys typed after ``ui.leader`` to command lines, e.g.
``t = "test"`` runs ``:test`` on ``\\t``. No mapping may be a prefix of another,
so a sequence runs as soon as it is complete.
"""

from __future__ import annotations

import os
import re
import shlex
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

BUILTINS: dict[str, str] = {
    "help": "full key reference",
    "quit": "quit (asks while runs continue)",
    "refresh": "refresh the bookmark list",
    "reload": "reload configuration",
}
ALIASES = {"q": "quit"}  # not offered by completion
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


class CommandError(ValueError):
    """A command line that cannot run."""


@dataclass(frozen=True)
class Command:
    line: str  # as typed, without the colon
    builtin: str = ""  # a BUILTINS name, or "" for a shell command
    script: str = ""  # run with sh -c


def parse(line: str, commands: Mapping[str, str]) -> Command:
    """Interpret a command line typed after ``:`` (a leading ``:`` is ignored)."""
    line = line.strip().removeprefix(":").strip()
    if not line:
        raise CommandError("empty command")
    if line.startswith("!"):
        script = line[1:].strip()
        if not script:
            raise CommandError(":! needs a shell command, e.g. :!make")
        return Command(line, script=script)
    name, _, args = line.partition(" ")
    args = args.strip()
    builtin = ALIASES.get(name, name)
    if builtin in BUILTINS:
        if args:
            raise CommandError(f":{name} takes no arguments")
        return Command(line, builtin=builtin)
    if name in commands:
        return Command(line, script=f"{commands[name]} {args}".strip())
    raise CommandError(f"unknown command :{name} (tab lists commands)")


def validate(commands: Mapping[str, str], leader: str, mappings: Mapping[str, str]) -> None:
    """Raise :class:`CommandError` for names, leader keys or mappings that cannot work."""
    for name, script in commands.items():
        if not NAME.fullmatch(name):
            raise CommandError(
                f"[commands] name {name!r} must start with a letter and contain only "
                "letters, digits, _ and -"
            )
        if name in BUILTINS or name in ALIASES:
            raise CommandError(f"[commands] {name} is a built-in command")
        if not script.strip():
            raise CommandError(f"[commands] {name} must not be empty")
    if leader and (len(leader) != 1 or not leader.isprintable() or leader == ":"):
        raise CommandError('ui.leader must be one printable character other than ":", or ""')
    if mappings and not leader:
        raise CommandError('[leader] mappings need ui.leader (e.g. "\\\\")')
    for keys, line in mappings.items():
        if not keys or not keys.isprintable():
            raise CommandError(f"[leader] {keys!r}: keys must be printable characters")
        longer = next((k for k in mappings if k != keys and k.startswith(keys)), None)
        if longer is not None:
            raise CommandError(
                f"[leader] {keys!r} is a prefix of {longer!r}; it could never wait for the rest"
            )
        try:
            parse(line, commands)
        except CommandError as exc:
            raise CommandError(f"[leader] {keys!r}: {exc}") from exc


class LeaderKeys:
    """Keys typed after the leader, until they spell a mapping.

    Like :class:`~project_sandbox_tui.widgets.PrefixKeys`, a timestamp rather
    than a timer decides when a half-typed sequence expires.
    """

    TIMEOUT = 1.5

    def __init__(self) -> None:
        self._typed: str | None = None
        self._at = 0.0

    @property
    def pending(self) -> str | None:
        """What was typed after the leader; None when not waiting for a mapping."""
        if self._typed is not None and time.monotonic() - self._at < self.TIMEOUT:
            return self._typed
        return None

    def start(self) -> None:
        self._typed, self._at = "", time.monotonic()

    def cancel(self) -> None:
        self._typed = None

    def feed(self, key: str, mappings: Mapping[str, str]) -> str | None:
        """Add a key. Returns the mapped command line once a mapping is complete;
        otherwise None, still pending while the keys can grow into a mapping."""
        typed = (self.pending or "") + key
        if typed in mappings:
            self._typed = None
            return mappings[typed]
        if any(k.startswith(typed) for k in mappings):
            self._typed, self._at = typed, time.monotonic()
        else:
            self._typed = None
        return None


# ---------------------------------------------------------------- completion


def complete(
    line: str, names: Iterable[str], cwd: Path, path: str | None = None
) -> tuple[int, list[str]]:
    """Completions for the word at the end of ``line``.

    Returns where that word starts and its replacements: command names for the
    first word, programs on ``path`` (default ``$PATH``) after ``!``, and file
    names relative to ``cwd`` for arguments.
    """
    shell = line.startswith("!")
    body = line[1:] if shell else line
    start = len(line) - len(re.split(r"\s", body)[-1])
    word = line[start:]
    if re.search(r"\s", body) or (shell and "/" in word):
        return start, files(word, cwd)
    if shell:
        return start, programs(word, path)
    return start, sorted(n for n in set(names) if n.startswith(word))


def programs(prefix: str, path: str | None = None) -> list[str]:
    """Executables on ``path`` (default ``$PATH``) whose names start with ``prefix``."""
    if not prefix:
        return []
    found: set[str] = set()
    for directory in os.get_exec_path({"PATH": path} if path is not None else None):
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(prefix) and os.access(entry.path, os.X_OK):
                try:
                    if entry.is_file():
                        found.add(entry.name)
                except OSError:
                    continue
    return sorted(found)


def files(word: str, cwd: Path) -> list[str]:
    """Paths starting with ``word``; directories end in ``/``. Hidden files need a ``.``."""
    head, _, prefix = word.rpartition("/")
    if word.startswith("/") and not head:
        head = "/"
    directory = Path(os.path.expanduser(head)) if head else Path()
    if not directory.is_absolute():
        directory = cwd / directory
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return []
    shown = word[: len(word) - len(prefix)]
    out = []
    for entry in entries:
        if not entry.name.startswith(prefix) or (
            entry.name[0] == "." and not prefix.startswith(".")
        ):
            continue
        try:
            suffix = "/" if entry.is_dir() else ""
        except OSError:
            suffix = ""
        out.append(_quote(shown + entry.name) + suffix)
    return sorted(out)


def _quote(word: str) -> str:
    """Shell-quote a file name, keeping a leading ``~/`` expandable."""
    if word.startswith("~/"):
        return "~/" + _quote(word[2:]) if word[2:] else word
    return shlex.quote(word)
