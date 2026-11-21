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
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit
import tomlkit.items

from project_sandbox_tui.config import (
    _SECTIONS,
    ACTION_KEYS,
    AGENTS,
    AgentProfile,
    Config,
    ConfigError,
    ConfigScope,
    ProjectTrust,
    Workflow,
    _load,
    global_config_path,
    load,
    merge_config,
    project_config_path,
    state_home,
    state_lock,
    validate_profile_name,
)


@dataclass(frozen=True)
class Key:
    section: str
    name: str
    default: Any  # decides how a value from the command line is parsed

    def __str__(self) -> str:
        return f"{self.section}.{self.name}"

    @property
    def path(self) -> list[str]:
        """Table names from the document root to the value."""
        if self.section in ("actions", "profiles", "workflows"):
            entry, _, name = self.name.rpartition(".")
            return [self.section, entry, name]
        return [self.section, self.name]


def parse_key(text: str) -> Key:
    """``section.key``, ``models.<agent>``, ``commands.<name>``, ``leader.<keys>``
    or a field of a named action, profile or workflow."""
    section, _, name = text.partition(".")
    if section == "models" and name in AGENTS:
        return Key(section, name, [])
    if section in ("commands", "leader") and name:
        return Key(section, name, "")
    if section == "actions":
        action, _, field = name.rpartition(".")
        if action and field in ACTION_KEYS:
            return Key(section, name, ACTION_KEYS[field])
        raise ConfigError(f"unknown key {text!r}: use actions.<name>.<{'|'.join(ACTION_KEYS)}>")
    if section == "profiles":
        profile, _, field = name.rpartition(".")
        defaults = dataclasses.asdict(AgentProfile())
        if profile and field in defaults:
            return Key(section, name, defaults[field])
        raise ConfigError(f"unknown key {text!r}: use profiles.<name>.<{'|'.join(defaults)}>")
    if section == "workflows":
        workflow, _, field = name.rpartition(".")
        defaults = dataclasses.asdict(Workflow())
        if workflow and field in defaults:
            return Key(section, name, defaults[field])
        raise ConfigError(f"unknown key {text!r}: use workflows.<name>.<{'|'.join(defaults)}>")
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
    out = []
    for section, table in data.items():
        if isinstance(table, list):  # [[workspace_skills]]
            for index, entry in enumerate(table):
                if isinstance(entry, dict):
                    out += [(f"{section}[{index}].{k}", v) for k, v in entry.items()]
            continue
        if not isinstance(table, dict):
            continue
        for name, value in table.items():
            if section in ("actions", "profiles", "workflows") and isinstance(value, dict):
                out += [(f"{section}.{name}.{k}", v) for k, v in value.items()]
            else:
                out.append((f"{section}.{name}", value))
    return out


def effective(cfg: Config) -> dict[str, Any]:
    """The merged configuration as ``{section: {key: value}}``."""
    data: dict[str, Any] = {name: dataclasses.asdict(getattr(cfg, name)) for name in _SECTIONS}
    data["models"] = dict(cfg.models)
    data["commands"] = dict(cfg.commands)
    data["leader"] = dict(cfg.leader)
    data["actions"] = {name: action.to_dict() for name, action in cfg.actions.items()}
    data["profiles"] = {name: dataclasses.asdict(profile) for name, profile in cfg.profiles.items()}
    data["workflows"] = {
        name: dataclasses.asdict(workflow) for name, workflow in cfg.workflows.items()
    }
    data["workspace_skills"] = [dataclasses.asdict(skill) for skill in cfg.workspace_skills]
    return data


# ---------------------------------------------------------------- editing


class Editor:
    """Change one config file of ``repo`` (``global`` or ``project``)."""

    def __init__(
        self,
        scope: ConfigScope,
        repo: Path | None,
        global_path: Path | None = None,
        *,
        warn: Callable[[str], None] | None = None,
    ) -> None:
        if scope == "project" and repo is None:
            raise ConfigError("--project needs a jj repository (-R/--repository)")
        self.scope = scope
        self.repo = repo
        self.global_path = global_path or global_config_path()
        self.path = project_config_path(repo) if repo and scope == "project" else self.global_path
        self.warn = warn

    def read(self) -> tomlkit.TOMLDocument:
        try:
            return tomlkit.parse(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return tomlkit.document()
        except (OSError, ValueError) as exc:  # tomlkit's ParseError is a ValueError
            raise ConfigError(f"{self.path}: {exc}") from exc

    @contextlib.contextmanager
    def _edit(self) -> Generator[tomlkit.TOMLDocument]:
        # Project and global files are validated together. Serialize both scopes,
        # including the trusted-copy update, across CLI and TUI editors.
        try:
            with state_lock(state_home() / "config.lock"):
                if self.scope == "project" and self.repo is not None:
                    trust = ProjectTrust.check(self.repo)
                    # Parse exactly the bytes whose trust status was checked.
                    try:
                        doc = tomlkit.parse(trust.current or "")
                    except ValueError as exc:
                        raise ConfigError(f"{self.path}: {exc}") from exc
                    trusted = not trust.needs_review
                else:
                    doc = self.read()
                    trusted = False
                before = tomlkit.dumps(doc)
                yield doc
                text = tomlkit.dumps(doc)
                if text != before:
                    self._write(text, trusted)
        except OSError as exc:
            raise ConfigError(f"cannot write {self.path}: {exc}") from exc

    def set(self, key: Key, value: Any) -> None:
        with self._edit() as doc:
            table: Any = doc
            for part in key.path[:-1]:
                inner = table.get(part)
                if not isinstance(inner, dict):
                    inner = tomlkit.table(
                        is_super_table=part in ("actions", "profiles", "workflows")
                    )
                    table[part] = inner
                table = inner
            table[key.path[-1]] = value

    def save_profile(self, name: str, profile: AgentProfile) -> None:
        """Save all profile fields together, preserving unrelated project settings."""
        validate_profile_name(name)
        profile.validate(f"profiles.{name}")
        with self._edit() as doc:
            if "profiles" not in doc:
                doc["profiles"] = tomlkit.table(is_super_table=True)
            profiles: Any = doc["profiles"]
            if not isinstance(profiles, dict):
                raise ConfigError("[profiles] must be a table")
            if name not in profiles:
                profiles[name] = tomlkit.table()
            if not isinstance(profiles[name], dict):
                raise ConfigError(f"[profiles.{name}] must be a table")
            for key, value in dataclasses.asdict(profile).items():
                profiles[name][key] = value

    def unset(self, key: Key) -> bool:
        """Remove ``key``; returns False if the file did not set it."""
        return self._unset(key.path)

    def delete_profile(self, name: str) -> bool:
        """Remove a whole profile from this scope, if present."""
        validate_profile_name(name)
        return self._unset(["profiles", name])

    def _unset(self, path: list[str]) -> bool:
        with self._edit() as doc:
            tables: list[Any] = [doc]
            for part in path[:-1]:
                inner = tables[-1].get(part)
                if not isinstance(inner, dict):
                    return False
                tables.append(inner)
            if path[-1] not in tables[-1]:
                return False
            del tables[-1][path[-1]]
            # Drop tables left with nothing, not even a comment, innermost first.
            for parent, part, table in reversed(list(zip(tables, path, tables[1:], strict=False))):
                if isinstance(table, tomlkit.items.Table) and not table.as_string().strip():
                    del parent[part]
                else:
                    break
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

    def _write(self, text: str, trusted: bool) -> None:
        mine = tomllib.loads(text)
        other = self._other()
        if self.scope == "project":
            cfg = merge_config(other, mine, self.path)
        else:
            project_path = project_config_path(self.repo) if self.repo is not None else None
            cfg = merge_config(mine, other, project_path)
        cfg.validate()  # nothing is written when this raises

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
        if self.warn is not None:
            for warning in cfg.warnings:
                self.warn(warning)


def file_values(
    scope: ConfigScope, repo: Path | None, global_path: Path | None = None
) -> dict[str, Any]:
    """The settings one file makes (the project file as in the repository)."""
    return tomllib.loads(tomlkit.dumps(Editor(scope, repo, global_path).read()))


def effective_values(
    repo: Path | None, global_path: Path | None = None, *, warn: Callable[[str], None] | None = None
) -> dict[str, Any]:
    """The merged settings in effect (global + trusted project config)."""
    cfg = load(repo, global_path) if repo is not None else _load(global_path, None, None)
    if warn is not None:
        for warning in cfg.warnings:
            warn(warning)
    return effective(cfg)
