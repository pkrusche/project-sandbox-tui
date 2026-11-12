"""Command lines typed after ``:``, and leader-key mappings that run them.

- ``:!CMD`` runs ``CMD`` with ``sh -c`` in the repository, like vim's ``:!``.
- ``:NAME [ARGS]`` runs the ``[commands]`` entry ``NAME`` with ``ARGS``
  appended, e.g. ``test = "uv run pytest"`` makes ``:test -k merge`` run
  ``uv run pytest -k merge``.
- ``:NAME [ARGS]`` for an ``[actions]`` entry starts its prompt as a background
  sandbox run on each target bookmark, with ``ARGS`` appended to the prompt.
- Built-in commands control the TUI itself (:data:`BUILTINS`).
- ``:host AGENT [ARGS]`` runs an agent CLI directly in each target workspace.
- A leading ``*`` runs a shell command once in each target workspace instead
  of once in the repository: ``:*!pytest -x``, ``:*test``. Actions run per
  target anyway, so ``*`` changes nothing for them.
- A leading ``&`` runs a shell command once in the background: ``:&!pytest``,
  ``:&test``.

``[leader]`` maps keys typed after ``ui.leader`` to command lines, e.g.
``t = "test"`` runs ``:test`` on ``\\t``. No mapping may be a prefix of another,
so a sequence runs as soon as it is complete.
"""

from __future__ import annotations

import os
import re
import shlex
import time
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

BUILTINS: dict[str, str] = {
    "help": "full key reference",
    "host": "run a host agent: :host AGENT [ARGS...]",
    "quit": "quit (asks while runs continue)",
    "refresh": "refresh the bookmark list",
    "reload": "reload configuration",
    "config-profiles": "configure saved agent profiles",
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
    each: bool = False  # once in each target workspace (``:*``), not in the repository
    background: bool = False  # a single captured task (``:&``)
    action: str = ""  # an [actions] name: a sandbox run on each target
    args: str = ""  # an action's extra words, appended to its prompt
    name: str = ""  # the [commands] entry that supplied ``script``, if any
    argv: tuple[str, ...] = ()  # :host agent executable and CLI arguments


def parse(line: str, commands: Mapping[str, str], actions: Collection[str] = ()) -> Command:
    """Interpret a command line typed after ``:`` (a leading ``:`` is ignored)."""
    line = line.strip().removeprefix(":").strip()
    each = line.startswith("*")
    background = line.startswith("&")
    prefix = line[:1] if each or background else ""
    body = line[1:].strip() if prefix else line
    if not body:
        raise CommandError(
            f":{prefix} needs a command, e.g. :{prefix}!make" if prefix else "empty command"
        )
    if body.startswith("!"):
        script = body[1:].strip()
        if not script:
            raise CommandError(f":{prefix}! needs a shell command, e.g. :{prefix}!make")
        return Command(line, script=script, each=each, background=background)
    name, *rest = body.split(maxsplit=1)
    args = rest[0] if rest else ""
    builtin = ALIASES.get(name, name)
    if builtin in BUILTINS:
        if each:
            raise CommandError(f":{name} cannot run per workspace")
        if background:
            raise CommandError(f":{name} cannot run in the background")
        if builtin == "host":
            try:
                argv = tuple(shlex.split(args))
            except ValueError as exc:
                raise CommandError(f":host: {exc}") from exc
            if not argv or not argv[0]:
                raise CommandError(":host needs an agent, e.g. :host codex --model my-model")
            return Command(line, builtin=builtin, argv=argv)
        if args:
            raise CommandError(f":{name} takes no arguments")
        return Command(line, builtin=builtin)
    if name in commands:
        return Command(
            line,
            script=f"{commands[name]} {args}".strip(),
            each=each,
            background=background,
            name=name,
        )
    if name in actions:
        if background:
            raise CommandError(
                f":{name} already runs in the background on each target; use :{name}"
            )
        return Command(line, action=name, args=args, each=True)
    raise CommandError(f"unknown command :{name} (tab lists commands)")


def check_name(table: str, name: str) -> None:
    """Raise :class:`CommandError` unless ``name`` can be typed after ``:``."""
    if not NAME.fullmatch(name):
        raise CommandError(
            f"[{table}] name {name!r} must start with a letter and contain only "
            "letters, digits, _ and -"
        )
    if name in BUILTINS or name in ALIASES:
        raise CommandError(f"[{table}] {name} is a built-in command")


def validate(
    commands: Mapping[str, str],
    leader: str,
    mappings: Mapping[str, str],
    actions: Collection[str] = (),
) -> None:
    """Raise :class:`CommandError` for names, leader keys or mappings that cannot work."""
    for name, script in commands.items():
        check_name("commands", name)
        if not script.strip():
            raise CommandError(f"[commands] {name} must not be empty")
    for name in actions:
        check_name("actions", name)
        if name in commands:
            raise CommandError(f"[actions] {name} is also in [commands]; rename one")
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
            parse(line, commands, actions)
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
    first word (also after ``*`` or ``&``), programs on ``path`` (default
    ``$PATH``) after ``!``, and file names relative to ``cwd`` for arguments.
    """
    prefix = len(line) - len(line[1:].lstrip()) if line.startswith(("*", "&")) else 0
    line = line[prefix:]
    shell = line.startswith("!")
    body = line[1:] if shell else line
    word_start, word = _last_word(body)
    start = prefix + word_start + int(shell)
    if body[:word_start].strip() or (shell and "/" in word):
        return start, files(word, cwd)
    if shell:
        return start, [shlex.quote(name) for name in programs(word, path, cwd)]
    return start, sorted(n for n in set(names) if n.startswith(word))


def _last_word(line: str) -> tuple[int, str]:
    """Find a shell word without splitting spaces inside quotes or escapes.

    Close unfinished quotes only for decoding the completion prefix; candidates
    replace the entire word with a complete, quoted argument.
    """
    start, quote, escaped = 0, "", False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char.isspace():
            start = index + 1
    raw = line[start:]
    words = shlex.split(raw + ("\\" if escaped else "") + quote)
    return start, words[0] if words else ""


def programs(prefix: str, path: str | None = None, cwd: Path | None = None) -> list[str]:
    """Executables on ``path`` (default ``$PATH``) whose names start with ``prefix``."""
    if not prefix:
        return []
    found: set[str] = set()
    for directory in os.get_exec_path({"PATH": path} if path is not None else None):
        root = Path(directory or ".")
        if cwd is not None and not root.is_absolute():
            root = cwd / root
        try:
            entries = list(os.scandir(root))
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
