"""``sbx-tui config``: read and change settings from the command line, like
``git config`` / ``jj config``.

Edits keep the file's comments and layout (tomlkit). A change is validated
together with the other file before it is written. Editing a trusted project
config keeps it trusted; an untrusted one stays untrusted (see config.py).
"""

from __future__ import annotations

import contextlib
import dataclasses
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import tomlkit
import tomlkit.items

from project_sandbox_tui.config import (
    _SECTIONS,
    AGENTS,
    Config,
    ConfigError,
    ProjectTrust,
    _load,
    deep_merge,
    from_dict,
    global_config_path,
    load,
    project_config_path,
)

Scope = Literal["global", "project"]


@dataclass(frozen=True)
class Key:
    section: str
    name: str
    default: Any  # decides how a value from the command line is parsed

    def __str__(self) -> str:
        return f"{self.section}.{self.name}"


def parse_key(text: str) -> Key:
    """``section.key``, ``models.<agent>``, ``commands.<name>`` or ``leader.<keys>``."""
    section, _, name = text.partition(".")
    if section == "models" and name in AGENTS:
        return Key(section, name, [])
    if section in ("commands", "leader") and name:
        return Key(section, name, "")
    cls = _SECTIONS.get(section)
    if cls is None or name not in {f.name for f in dataclasses.fields(cls)}:
        raise ConfigError(f"unknown key {text!r} (see `sbx-tui config list`)")
    return Key(section, name, getattr(cls(), name))


def parse_value(key: Key, text: str) -> Any:
    """Interpret a command-line value with the type of ``key``."""
    default = key.default
    if isinstance(default, bool):
        if text not in ("true", "false"):
            raise ConfigError(f"{key} must be true or false")
        return text == "true"
    if isinstance(default, int | float):
        try:
            return type(default)(text)
        except ValueError:
            kind = "an integer" if isinstance(default, int) else "a number"
            raise ConfigError(f"{key} must be {kind}") from None
    if isinstance(default, list):
        try:
            value = tomllib.loads(f"v = {text}")["v"]
        except tomllib.TOMLDecodeError:
            value = None
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f'{key} takes a TOML list of strings, e.g. \'["a", "b"]\'')
        return value
    return text


def format_value(value: Any) -> str:
    """A value as TOML (``get`` prints plain strings unquoted)."""
    return tomlkit.item(value).as_string()


def flatten(data: dict[str, Any]) -> list[tuple[str, Any]]:
    return [
        (f"{section}.{name}", value)
        for section, table in data.items()
        if isinstance(table, dict)
        for name, value in table.items()
    ]


def effective(cfg: Config) -> dict[str, Any]:
    """The merged configuration as ``{section: {key: value}}``."""
    data = {name: dataclasses.asdict(getattr(cfg, name)) for name in _SECTIONS}
    data["models"] = dict(cfg.models)
    data["commands"] = dict(cfg.commands)
    data["leader"] = dict(cfg.leader)
    return data


# ---------------------------------------------------------------- editing


class Editor:
    """Change one config file of ``repo`` (``global`` or ``project``)."""

    def __init__(self, scope: Scope, repo: Path | None, global_path: Path | None = None) -> None:
        if scope == "project" and repo is None:
            raise ConfigError("--project needs a jj repository (-R/--repository)")
        self.scope = scope
        self.repo = repo
        self.global_path = global_path or global_config_path()
        self.path = project_config_path(repo) if repo and scope == "project" else self.global_path

    def read(self) -> tomlkit.TOMLDocument:
        try:
            return tomlkit.parse(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return tomlkit.document()
        except (OSError, ValueError) as exc:  # tomlkit's ParseError is a ValueError
            raise ConfigError(f"{self.path}: {exc}") from exc

    def set(self, key: Key, value: Any) -> None:
        doc = self.read()
        table = doc.get(key.section)
        if not isinstance(table, dict):
            table = tomlkit.table()
            doc[key.section] = table
        table[key.name] = value
        self.write(doc)

    def unset(self, key: Key) -> bool:
        """Remove ``key``; returns False if the file did not set it."""
        doc = self.read()
        table = doc.get(key.section)
        if not isinstance(table, dict) or key.name not in table:
            return False
        del table[key.name]
        if isinstance(table, tomlkit.items.Table) and not table.as_string().strip():
            del doc[key.section]  # nothing left, not even a comment
        self.write(doc)
        return True

    def _other(self) -> dict[str, Any]:
        """What the other file contributes, for validating the merged result."""
        try:
            if self.scope == "project":
                path = self.global_path
                text = path.read_text(encoding="utf-8") if path.is_file() else ""
            else:
                trust = ProjectTrust.check(self.repo) if self.repo else None
                text = (trust.trusted or "") if trust and trust.current is not None else ""
            return tomllib.loads(text)
        except (OSError, ValueError) as exc:
            raise ConfigError(f"cannot load the other config file: {exc}") from exc

    def write(self, doc: tomlkit.TOMLDocument) -> None:
        text = tomlkit.dumps(doc)
        mine = tomllib.loads(text)
        other = self._other()
        merged = deep_merge(other, mine) if self.scope == "project" else deep_merge(mine, other)
        from_dict(merged).validate()  # nothing is written when this raises

        trusted = self.repo is not None and not ProjectTrust.check(self.repo).needs_review
        tmp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            raise ConfigError(f"cannot write {self.path}: {exc}") from exc
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
        if self.scope == "project" and trusted:
            # The user made this change, to a file they had trusted.
            assert self.repo is not None
            ProjectTrust(self.path, text, None).trust(self.repo)


def file_values(scope: Scope, repo: Path | None, global_path: Path | None = None) -> dict[str, Any]:
    """The settings one file makes (the project file as in the repository)."""
    return tomllib.loads(tomlkit.dumps(Editor(scope, repo, global_path).read()))


def effective_values(repo: Path | None, global_path: Path | None = None) -> dict[str, Any]:
    """The merged settings in effect (global + trusted project config)."""
    cfg = load(repo, global_path) if repo is not None else _load(global_path, None, None)
    return effective(cfg)
