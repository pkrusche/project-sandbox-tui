"""Configuration: global + per-project TOML, merged, plus per-repo state paths.

Resolution order (later wins):  built-in defaults → global config → project config.
The "last used" launch values live in the state directory, not in TOML, so the
TUI never rewrites a file the user edits by hand.

The project config comes with the repository, so it can be written by someone
else; it can set commands that run on the host. Only a copy the user reviewed
is loaded: trusting the file copies it into the state directory, and a changed
file is shown as a diff against that copy before it takes effect.
"""

from __future__ import annotations

import contextlib
import dataclasses
import difflib
import hashlib
import json
import math
import os
import shlex
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from project_sandbox_tui import commands
from project_sandbox_tui.launchers import TERMINALS, fill_command

AGENTS: tuple[str, ...] = ("claude", "codex", "opencode", "pi", "bash")
FAST_AGENTS: tuple[str, ...] = ("claude", "codex")
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
RUNTIMES: tuple[str, ...] = ("auto", "apple-container", "docker", "podman")
GENERATED_BUILDS: tuple[str, ...] = ("", "python-uv", "rust-cargo", "node-npm")

PROJECT_CONFIG_NAME = ".project-sandbox-tui.toml"
TRUSTED_CONFIG_NAME = "trusted-project-config.toml"  # in the repo's state directory
APP_NAME = "project-sandbox-tui"


class ConfigError(ValueError):
    """Raised for invalid or inconsistent configuration."""


# ---------------------------------------------------------------- sections


@dataclass
class Defaults:
    agent: str = "claude"
    model: str = ""  # "" = the agent's own default
    effort: str = ""  # "" = the agent's own default
    timeout: int = 0  # seconds for unsupervised runs; 0 = no timeout
    fast: bool = False  # Codex and Claude; requires project-sandbox >= 0.3.1


@dataclass
class Network:
    internet_proxy: str = ""  # --internet-proxy URL
    agent_proxy: str = ""  # --agent-proxy URL
    agent_proxy_key_env: str = ""  # --agent-proxy-key-env NAME
    ca_certs: list[str] = field(default_factory=list)  # --ca-cert PATH (repeatable)
    extra_domains: list[str] = field(default_factory=list)  # --extra-domain (repeatable)
    allow_github: bool = False  # --allow-github
    no_firewall: bool = False  # --no-firewall


@dataclass
class Sandbox:
    command: str = "project-sandbox"  # e.g. "uvx project-sandbox"
    runtime: str = "auto"
    memory: str = ""  # "" = project-sandbox default (8g)
    cpus: int = 0  # 0 = project-sandbox default (4)
    no_forward_credentials: bool = False
    api_key_env: list[str] = field(default_factory=list)
    api_key_env_file: list[str] = field(default_factory=list)
    extra_args: list[str] = field(default_factory=list)


@dataclass
class Project:
    base_image: str = ""  # positional base image, e.g. python:3.12-slim
    dockerfile: str = ""  # --dockerfile (relative paths resolve against the repo)
    docker_context: str = ""  # --docker-context
    build: str = ""  # generated build: python-uv | rust-cargo | node-npm
    build_version: str = ""  # --python / --rust version for the generated build
    worktree_dir: str = ""  # --worktree-dir
    keep_workspace: bool = True  # --keep-workspace
    default_branch_start: str = ""  # start revision for new bookmarks ("" = main/trunk/@-)
    main_branch: str = ""  # cleanup removes work fully merged into it ("" = main/master/trunk)


@dataclass
class UI:
    notify_command: str = ""  # "" = platform default; tokens may use {title} {message}
    # where interactive sessions run: inline | tmux | zellij | screen | custom
    # ("" = custom if terminal_command is set, else inline)
    terminal: str = ""
    terminal_command: str = ""  # ui.terminal = "custom"; tokens may use {cmd} {branch} {repo}
    editor_command: str = ""  # "" = $EDITOR; tokens may use {path}
    host_agent_command: str = ""  # before first :host; tokens may use {agent} {path} {branch}
    refresh_seconds: float = 3.0
    bell: bool = True
    cheatsheet: bool = True  # auto-collapses in small terminals; toggle with H
    leader: str = "\\"  # starts [leader] key mappings ("" = none)

    def terminal_mode(self) -> str:
        """The effective ``terminal`` setting (see launchers.py)."""
        if self.terminal:
            return self.terminal
        return "custom" if self.terminal_command else "inline"


@dataclass
class Prompts:
    """Prompt templates for integrating selected branches ("" = built-in default).

    Placeholders: {inputs} {inputs_plain} {result} {base} {first} {rest}.
    sbx-tui appends a "Current state" section describing what it prepared.
    """

    octopus_merge: str = ""
    octopus_rebase: str = ""


@dataclass
class Action:
    """``[actions.NAME]``: a prompt that ``:NAME`` starts as a background sandbox
    run on each target bookmark. Unset launch options use the launch defaults.

    Placeholders ``{bookmark}`` and ``{main}`` are replaced; other braces stay.
    """

    prompt: str = ""
    agent: str = ""  # "" = launch default
    model: str = ""  # "" = launch default for the agent
    effort: str = ""  # "" = launch default
    timeout: int | None = None  # None = launch default

    def render(self, bookmark: str, main: str, extra: str = "") -> str:
        text = self.prompt.replace("{bookmark}", bookmark).replace("{main}", main).rstrip()
        if extra.strip():
            text += f"\n\n{extra.strip()}"
        return text + "\n"

    def to_dict(self) -> dict[str, Any]:
        """The keys a config file sets, for ``sbx-tui config list``."""
        out: dict[str, Any] = {"prompt": self.prompt}
        out.update({k: v for k in ("agent", "model", "effort") if (v := getattr(self, k))})
        if self.timeout is not None:
            out["timeout"] = self.timeout
        return out


ACTION_KEYS: dict[str, Any] = {"prompt": "", "agent": "", "model": "", "effort": "", "timeout": 0}

# [commands] every configuration starts with; config files can redefine them.
DEFAULT_COMMANDS: dict[str, str] = {
    # Empty commits with only empty descendants, e.g. leftover working-copy heads.
    "abandon-empty": "jj abandon -r '(~empty())..'",
}
# Asked before a DEFAULT_COMMANDS entry runs, unless a config file redefined it.
DEFAULT_COMMAND_WARNINGS: dict[str, str] = {
    "abandon-empty": (
        "Run this only when all workspaces are inactive. Other workspaces' empty "
        "working-copy commits are empty heads too: abandoning them leaves those "
        "workspaces stale, and bookmarks on empty heads are deleted."
    ),
}


def command_warning(commands: dict[str, str], name: str) -> str:
    """The warning to confirm before ``:name``, or "" if it has none or was redefined."""
    if not name or commands.get(name) != DEFAULT_COMMANDS.get(name):
        return ""
    return DEFAULT_COMMAND_WARNINGS.get(name, "")


@dataclass
class Config:
    defaults: Defaults = field(default_factory=Defaults)
    network: Network = field(default_factory=Network)
    sandbox: Sandbox = field(default_factory=Sandbox)
    project: Project = field(default_factory=Project)
    ui: UI = field(default_factory=UI)
    prompts: Prompts = field(default_factory=Prompts)
    models: dict[str, list[str]] = field(default_factory=dict)  # extra suggestions (models.py)
    commands: dict[str, str] = field(  # :name -> shell command (commands.py)
        default_factory=lambda: dict(DEFAULT_COMMANDS)
    )
    leader: dict[str, str] = field(default_factory=dict)  # keys after ui.leader -> command line
    actions: dict[str, Action] = field(default_factory=dict)  # :name -> sandbox run per target
    sources: list[Path] = field(default_factory=list)  # files that were loaded

    # ---------------------------------------------------------- helpers

    def sandbox_command(self) -> list[str]:
        try:
            cmd = shlex.split(self.sandbox.command)
        except ValueError as exc:
            raise ConfigError(f"sandbox.command: {exc}") from exc
        if not cmd:
            raise ConfigError("sandbox.command must not be empty")
        return cmd

    def validate(self) -> None:
        d, p, s = self.defaults, self.project, self.sandbox
        if d.agent not in AGENTS:
            raise ConfigError(f"defaults.agent must be one of {', '.join(AGENTS)}")
        if d.effort and d.effort not in EFFORTS:
            raise ConfigError(f"defaults.effort must be empty or one of {', '.join(EFFORTS)}")
        if d.timeout < 0:
            raise ConfigError("defaults.timeout must be >= 0")
        if s.runtime not in RUNTIMES:
            raise ConfigError(f"sandbox.runtime must be one of {', '.join(RUNTIMES)}")
        if s.cpus < 0:
            raise ConfigError("sandbox.cpus must be >= 0")
        self.sandbox_command()
        placeholders = {
            "notify_command": ("title", "message"),
            "terminal_command": ("cmd", "branch", "repo"),
            "editor_command": ("path",),
            "host_agent_command": ("agent", "path", "branch"),
        }
        for key, names in placeholders.items():
            command = getattr(self.ui, key)
            if command:
                try:
                    tokens = fill_command(command, dict.fromkeys(names, ""))
                except ValueError as exc:
                    raise ConfigError(f"ui.{key}: {exc}") from exc
                if not tokens:
                    raise ConfigError(f"ui.{key} must contain a command")
        if self.ui.terminal and self.ui.terminal not in TERMINALS:
            raise ConfigError(f"ui.terminal must be one of {', '.join(TERMINALS)}")
        mode = self.ui.terminal_mode()
        if mode == "custom" and not self.ui.terminal_command:
            raise ConfigError('ui.terminal = "custom" needs ui.terminal_command')
        if mode != "custom" and self.ui.terminal_command:
            raise ConfigError(
                f'ui.terminal_command is only used with ui.terminal = "custom" (not "{mode}")'
            )
        if p.build not in GENERATED_BUILDS:
            raise ConfigError(
                "project.build must be empty or one of python-uv, rust-cargo, node-npm"
            )
        image_sources = [bool(p.base_image), bool(p.dockerfile), bool(p.build)]
        if sum(image_sources) > 1:
            raise ConfigError(
                "set only one of project.base_image, project.dockerfile, project.build"
            )
        if p.build_version and p.build not in ("python-uv", "rust-cargo"):
            raise ConfigError("project.build_version needs build = python-uv or rust-cargo")
        if p.docker_context and not p.dockerfile:
            raise ConfigError("project.docker_context needs project.dockerfile")
        for agent in self.models:
            if agent not in AGENTS:
                raise ConfigError(f"[models] has unknown agent {agent!r}")
        if not math.isfinite(self.ui.refresh_seconds) or self.ui.refresh_seconds <= 0:
            raise ConfigError("ui.refresh_seconds must be finite and > 0")
        for name, action in self.actions.items():
            if not action.prompt.strip():
                raise ConfigError(f"[actions.{name}] needs a prompt")
            if action.agent and action.agent not in AGENTS:
                raise ConfigError(f"actions.{name}.agent must be one of {', '.join(AGENTS)}")
            if action.effort and action.effort not in EFFORTS:
                raise ConfigError(
                    f"actions.{name}.effort must be empty or one of {', '.join(EFFORTS)}"
                )
            if action.timeout is not None and action.timeout < 0:
                raise ConfigError(f"actions.{name}.timeout must be >= 0")
        try:
            commands.validate(self.commands, self.ui.leader, self.leader, self.actions)
        except commands.CommandError as exc:
            raise ConfigError(str(exc)) from exc

    def has_image_source(self) -> bool:
        p = self.project
        return bool(p.base_image or p.dockerfile or p.build)

    def resolve_path(self, value: str, repo: Path) -> Path:
        path = Path(os.path.expanduser(value))
        return path if path.is_absolute() else (repo / path)


# Free-form tables: what their entries are, for error messages.
TABLES: dict[str, str] = {
    "models": "must be a table of agent = [model, ...]",
    "commands": 'must be a table of name = "shell command"',
    "leader": 'must be a table of keys = "command line"',
    "actions": "must be a table of [actions.NAME] tables with a prompt",
}

_SECTIONS: dict[str, type[Any]] = {
    "defaults": Defaults,
    "network": Network,
    "sandbox": Sandbox,
    "project": Project,
    "ui": UI,
    "prompts": Prompts,
}


# ---------------------------------------------------------------- loading


def config_home() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(configured) if configured.startswith("/") else Path.home() / ".config"
    return base / APP_NAME


def state_home() -> Path:
    configured = os.environ.get("XDG_STATE_HOME", "")
    base = Path(configured) if configured.startswith("/") else Path.home() / ".local" / "state"
    return base / APP_NAME


def global_config_path() -> Path:
    return config_home() / "config.toml"


def project_config_path(repo: Path) -> Path:
    return repo / PROJECT_CONFIG_NAME


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def _coerce(section: str, key: str, value: Any, default: Any) -> Any:
    where = f"{section}.{key}"
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{where} must be true or false")
        return value
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"{where} must be a number")
        try:
            return float(value)
        except OverflowError as exc:
            raise ConfigError(f"{where} is too large") from exc
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where} must be an integer")
        return value
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ConfigError(f"{where} must be a string")
        return value
    if isinstance(default, list):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{where} must be a list of strings")
        return list(value)
    raise ConfigError(f"{where}: unsupported type")  # pragma: no cover


def _build_section(name: str, cls: type[Any], data: Any) -> Any:
    if not isinstance(data, dict):
        raise ConfigError(f"[{name}] must be a table")
    instance = cls()
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"[{name}] has unknown key(s): {', '.join(unknown)}")
    for key, value in data.items():
        setattr(instance, key, _coerce(name, key, value, getattr(instance, key)))
    return instance


def from_dict(data: dict[str, Any]) -> Config:
    unknown = sorted(set(data) - set(_SECTIONS) - set(TABLES))
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(unknown)}")
    cfg = Config()
    for name, cls in _SECTIONS.items():
        if name in data:
            setattr(cfg, name, _build_section(name, cls, data[name]))
    models = data.get("models", {})
    if not isinstance(models, dict):
        raise ConfigError(f"[models] {TABLES['models']}")
    for agent, values in models.items():
        cfg.models[agent] = _coerce("models", agent, values, [])
    for name in ("commands", "leader"):
        table = data.get(name, {})
        if not isinstance(table, dict):
            raise ConfigError(f"[{name}] {TABLES[name]}")
        getattr(cfg, name).update({k: _coerce(name, k, v, "") for k, v in table.items()})
    actions = data.get("actions", {})
    if not isinstance(actions, dict):
        raise ConfigError(f"[actions] {TABLES['actions']}")
    for name, table in actions.items():
        cfg.actions[name] = _build_action(name, table)
    return cfg


def _build_action(name: str, data: Any) -> Action:
    where = f"actions.{name}"
    if not isinstance(data, dict):
        raise ConfigError(f'[{where}] must be a table, e.g. [{where}] prompt = "…"')
    unknown = sorted(set(data) - set(ACTION_KEYS))
    if unknown:
        raise ConfigError(f"[{where}] has unknown key(s): {', '.join(unknown)}")
    values = {k: _coerce(where, k, v, ACTION_KEYS[k]) for k, v in data.items()}
    return Action(**values)


def load(repo: Path, global_path: Path | None = None) -> Config:
    """Load global + the trusted project config for ``repo`` and validate the result.

    A project config that changed since it was trusted is ignored in favour of
    the trusted copy (see :class:`ProjectTrust`); one never trusted is ignored.
    """
    trust = ProjectTrust.check(repo)
    if trust.current is None or trust.trusted is None:
        return _load(global_path, None, None)
    return _load(global_path, trust.trusted, trusted_config_path(repo))


def load_candidate(trust: ProjectTrust, global_path: Path | None = None) -> Config:
    """Load the project config under review, to validate it before trusting it."""
    return _load(global_path, trust.current, trust.path)


def _load(
    global_path: Path | None, project_text: str | None, project_source: Path | None
) -> Config:
    gpath = global_path or global_config_path()
    merged: dict[str, Any] = {}
    sources: list[Path] = []
    if global_path is not None and not gpath.is_file():
        raise ConfigError(f"{gpath}: config file does not exist or is not a file")
    if gpath.is_file():
        merged = _read_toml(gpath)
        sources.append(gpath)
    if project_text is not None and project_source is not None:
        try:
            merged = deep_merge(merged, tomllib.loads(project_text))
        except ValueError as exc:
            raise ConfigError(f"{project_source}: {exc}") from exc
        sources.append(project_source)
    cfg = from_dict(merged)
    cfg.sources = sources
    cfg.validate()
    return cfg


def trusted_config_path(repo: Path) -> Path:
    return repo_state_dir(repo) / TRUSTED_CONFIG_NAME


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc


@dataclass(frozen=True)
class ProjectTrust:
    """The repository's project config next to the copy the user last trusted."""

    path: Path  # <repo>/.project-sandbox-tui.toml
    current: str | None  # its contents; None = the repository has no project config
    trusted: str | None  # the reviewed copy; None = never trusted

    @classmethod
    def check(cls, repo: Path) -> ProjectTrust:
        path = project_config_path(repo)
        return cls(path, _read_text(path), _read_text(trusted_config_path(repo)))

    @property
    def needs_review(self) -> bool:
        return self.current is not None and self.current != self.trusted

    def diff(self) -> str:
        """What trusting would change: a unified diff against the trusted copy."""
        before = (self.trusted or "").splitlines(keepends=True)
        after = (self.current or "").splitlines(keepends=True)
        lines = difflib.unified_diff(
            before,
            after,
            fromfile="trusted" if self.trusted is not None else "(never trusted)",
            tofile=str(self.path),
        )
        return "".join(line if line.endswith("\n") else line + "\n" for line in lines)

    def trust(self, repo: Path) -> None:
        """Trust exactly the reviewed text (not whatever the file holds by now)."""
        if self.current is None:
            raise ConfigError(f"{self.path}: nothing to trust")
        path = trusted_config_path(repo)
        tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            private_dir(path.parent)
            tmp.write_text(self.current, encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            raise ConfigError(f"cannot write {path}: {exc}") from exc
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    def describe(self) -> str:
        """One line for the user: why the project config is not in effect."""
        if self.trusted is None:
            return f"{self.path} is not trusted yet and is ignored"
        return f"{self.path} changed since it was trusted; using the trusted version"


# ---------------------------------------------------------------- state


def repo_state_dir(repo: Path) -> Path:
    resolved = repo.resolve()
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:8]
    return state_home() / f"{resolved.name}-{digest}"


def private_dir(path: Path) -> Path:
    """Create ``path`` for the owner only: state holds transcripts, prompts and job
    argv. Also tightens a directory an earlier version created with the umask."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):  # e.g. a directory someone else owns
        path.chmod(0o700)
    return path


STATE_VERSION = 1  # of the JSON state files read with read_state_for_update


class StateFileError(OSError):
    """A state file cannot be updated without losing what it holds."""


def read_state_for_update(path: Path) -> dict[str, Any] | None:
    """The JSON object in ``path`` for a locked read-modify-write; None if missing.

    Never mistakes an unreadable file for an empty one, since the write that
    follows would discard everything it holds: read errors propagate. A file
    that does not hold a JSON object is moved aside (``<name>.corrupt-…``) so it
    can still be recovered, then treated as missing. One written by a newer
    version is left alone: rewriting it would drop what this version does not know.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        raw = json.loads(data.decode("utf-8"))
    except ValueError:
        raw = None
    if not isinstance(raw, dict):
        stamp = time.strftime("%Y%m%dT%H%M%S")
        path.replace(path.with_name(f"{path.name}.corrupt-{stamp}-{uuid.uuid4().hex[:6]}"))
        return None
    version = raw.get("version")
    if type(version) is int and version > STATE_VERSION:
        raise StateFileError(f"{path} was written by a newer sbx-tui (version {version})")
    return raw


def write_state(path: Path, data: Any, *, durable: bool = False) -> None:
    """Replace ``path`` atomically with ``data`` as JSON.

    ``durable`` flushes it to disk first, so even a power loss leaves the old
    or the new file, never an empty one. It costs a disk sync, so it is for
    rarely written state that cannot be rebuilt (the bookmark history), not
    for marks set on each key press.
    """
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=1)
            if durable:
                stream.flush()
                os.fsync(stream.fileno())
        tmp.replace(path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


@dataclass
class LastUsed:
    """Launch choices remembered per repository ("defaults that update")."""

    agent: str = ""
    effort: str | None = None  # None = never chosen; "" = explicitly agent default
    timeout: int | None = None
    model_by_agent: dict[str, str] = field(default_factory=dict)
    fast_by_agent: dict[str, bool] = field(default_factory=dict)
    host_command: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, repo: Path) -> LastUsed:
        path = repo_state_dir(repo) / "last_used.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        out = cls()
        if raw.get("agent") in AGENTS:
            out.agent = raw["agent"]
        if raw.get("effort") in (*EFFORTS, ""):
            out.effort = raw["effort"]
        if type(raw.get("timeout")) is int and raw["timeout"] >= 0:
            out.timeout = raw["timeout"]
        if isinstance(raw.get("model_by_agent"), dict):
            out.model_by_agent = {
                k: v for k, v in raw["model_by_agent"].items() if isinstance(v, str)
            }
        if isinstance(raw.get("fast_by_agent"), dict):
            out.fast_by_agent = {
                k: v
                for k, v in raw["fast_by_agent"].items()
                if k in FAST_AGENTS and type(v) is bool
            }
        host_command = raw.get("host_command")
        if (
            isinstance(host_command, list)
            and host_command
            and all(isinstance(arg, str) and "\0" not in arg for arg in host_command)
            and host_command[0]
        ):
            out.host_command = host_command
        return out

    def save(self, repo: Path) -> None:
        path = repo_state_dir(repo) / "last_used.json"
        tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            private_dir(path.parent)
            tmp.write_text(json.dumps(dataclasses.asdict(self), indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass  # remembering defaults is a convenience, never fatal
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)


@dataclass
class LaunchDefaults:
    agent: str
    model: str
    effort: str
    timeout: int
    fast: bool = False


def resolve_model(cfg: Config, last: LastUsed, agent: str) -> str:
    if agent in last.model_by_agent:
        return last.model_by_agent[agent]
    if agent == cfg.defaults.agent and cfg.defaults.model:
        return cfg.defaults.model
    return ""  # agent default; [models] entries are only autocomplete suggestions


def resolve_fast(cfg: Config, last: LastUsed, agent: str) -> bool:
    return agent in FAST_AGENTS and last.fast_by_agent.get(agent, cfg.defaults.fast)


def resolve_launch_defaults(cfg: Config, last: LastUsed) -> LaunchDefaults:
    agent = last.agent or cfg.defaults.agent
    effort = last.effort if last.effort is not None else cfg.defaults.effort
    timeout = last.timeout if last.timeout is not None else cfg.defaults.timeout
    return LaunchDefaults(
        agent=agent,
        model=resolve_model(cfg, last, agent),
        effort=effort,
        timeout=timeout,
        fast=resolve_fast(cfg, last, agent),
    )


def remember(
    repo: Path, agent: str, model: str, effort: str, timeout: int | None, *, fast: bool = False
) -> None:
    last = LastUsed.load(repo)
    last.agent = agent
    last.effort = effort
    if timeout is not None:
        last.timeout = timeout
    last.model_by_agent[agent] = model
    if agent in FAST_AGENTS:
        last.fast_by_agent[agent] = fast
    last.save(repo)


# ---------------------------------------------------------------- templates

PROJECT_TEMPLATE = """\
# project-sandbox-tui — per-project settings (merged over ~/.config/project-sandbox-tui/config.toml)

[project]
# Exactly one image source: base_image, dockerfile, or a generated build.
base_image = "python:3.12-slim"
# dockerfile = "Dockerfile"            # -> --dockerfile (relative to the repo)
# docker_context = "."                 # -> --docker-context
# build = "python-uv"                  # python-uv | rust-cargo | node-npm
# build_version = "3.12"               # -> --python / --rust for the generated build
# worktree_dir = ""                    # -> --worktree-dir (default: sibling <repo>-workspaces)
keep_workspace = true                  # -> --keep-workspace
# default_branch_start = "main"        # start revision for new bookmarks
# main_branch = "main"                 # cleanup offers workspaces/bookmarks fully merged into this

# [defaults]
# agent = "claude"                     # claude | codex | opencode | pi | bash
# model = ""
# effort = "medium"                    # low | medium | high | xhigh | max
# fast = false                         # Codex / Claude; project-sandbox >= 0.3.1

# [commands]                           # :name runs the shell command in the repository
# test = "uv run pytest"               # :test -k merge appends the arguments

# [actions.review]                     # :review starts this prompt as a background run on
# prompt = "Review {bookmark} against {main}; fix what you find."  # each selected bookmark
# effort = "high"                      # agent / model / effort / timeout: launch defaults

# [leader]                             # ui.leader (\\) then these keys run a command line
# t = "test"                           # "*test" runs it in each selected workspace
# rv = "review"
"""

GLOBAL_TEMPLATE = """\
# project-sandbox-tui — global settings

[defaults]
agent = "claude"          # claude | codex | opencode | pi | bash
model = ""                # "" = the agent's own default
effort = ""               # "" or low | medium | high | xhigh | max
fast = false              # Codex / Claude; project-sandbox >= 0.3.1
timeout = 0               # seconds for unsupervised runs; 0 = none

[network]
# internet_proxy = "http://proxy.example:3128"   # -> --internet-proxy
# agent_proxy = "http://127.0.0.1:4000"          # -> --agent-proxy
# ca_certs = ["/path/to/corp-root.pem"]          # -> --ca-cert (repeatable)
# extra_domains = []                              # -> --extra-domain (repeatable)
# allow_github = false

[sandbox]
command = "project-sandbox"   # or "uvx project-sandbox"
runtime = "auto"              # auto | apple-container | docker | podman
# memory = "16g"
# cpus = 6
# no_forward_credentials = false
# api_key_env = []
# api_key_env_file = []
# extra_args = []

[ui]
# notify_command = ""         # e.g. 'terminal-notifier -title {title} -message {message}'
# terminal = "inline"         # interactive sessions: inline (take over this terminal, return
#                             # after) | tmux | zellij | screen | custom (terminal_command)
# terminal_command = ""       # with terminal = "custom", e.g. 'wezterm start -- sh -c {cmd}'
# editor_command = ""         # e.g. 'code {path}'
# host_agent_command = ""     # I before first :host; e.g. '{agent}' or 'my-agent {path}'
refresh_seconds = 3
bell = true
cheatsheet = true             # auto-collapses in small terminals (H toggles)
# leader = '\\'                # starts [leader] key mappings, like vim's <leader>

# [models]                   # extra launch-form suggestions, listed first. Built in:
#                             # Claude aliases, Codex's own list (~/.codex/models_cache.json)
#                             # and the list from `sbx-tui --update-models`
# claude = ["opus", "sonnet"]
# codex = ["gpt-5.5"]

# [prompts]                   # templates for merging / rebasing selected branches (v / space,
#                             # then r); placeholders {inputs} {inputs_plain} {result} {base}
#                             # {first} {rest}
# octopus_merge = '''...'''
# octopus_rebase = '''...'''

# [commands]                  # :name runs the shell command (sh -c) in the repository;
#                             # arguments typed after the name are appended
# lint = "uv run ruff check ."

# [actions.chores]            # :chores starts this prompt as a background sandbox run on
#                             # each selected bookmark; {bookmark} and {main} are filled in
# prompt = "Tidy up {bookmark}: update docs and tests for its changes against {main}."
# agent = "claude"            # agent / model / effort / timeout default to the launch defaults

# [leader]                    # ui.leader, then these keys run a command line (as after :)
# l = "lint"
# L = "*lint"                 # * runs it in each selected workspace
# s = "!jj status"
# rc = "chores"
"""


def write_template(path: Path, template: str) -> bool:
    """Write ``template`` to ``path`` unless it exists. Returns True if written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(template)
    except FileExistsError:
        return False
    return True
