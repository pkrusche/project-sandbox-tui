"""Configuration: global + per-project TOML, merged, plus per-repo state paths.

Resolution order (later wins):  built-in defaults → global config → project config.
The "last used" launch values live in the state directory, not in TOML, so the
TUI never rewrites a file the user edits by hand.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import shlex
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from project_sandbox_tui.launchers import TERMINALS

AGENTS: tuple[str, ...] = ("claude", "codex", "opencode", "pi", "bash")
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
RUNTIMES: tuple[str, ...] = ("auto", "apple-container", "docker", "podman")
GENERATED_BUILDS: tuple[str, ...] = ("", "python-uv", "rust-cargo", "node-npm")

PROJECT_CONFIG_NAME = ".project-sandbox-tui.toml"
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
    default_branch_start: str = ""  # start revision for new branches ("" = main/trunk)
    main_branch: str = ""  # cleanup removes work fully merged into it ("" = main/master/trunk)


@dataclass
class UI:
    notify_command: str = ""  # "" = platform default; tokens may use {title} {message}
    # where interactive sessions run: inline | tmux | zellij | screen | custom
    # ("" = custom if terminal_command is set, else inline)
    terminal: str = ""
    terminal_command: str = ""  # ui.terminal = "custom"; tokens may use {cmd} {branch} {repo}
    editor_command: str = ""  # "" = $EDITOR; tokens may use {path}
    refresh_seconds: float = 3.0
    bell: bool = True
    cheatsheet: bool = True  # key cheat-sheet under the branch list (toggle with H)

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
class Config:
    defaults: Defaults = field(default_factory=Defaults)
    network: Network = field(default_factory=Network)
    sandbox: Sandbox = field(default_factory=Sandbox)
    project: Project = field(default_factory=Project)
    ui: UI = field(default_factory=UI)
    prompts: Prompts = field(default_factory=Prompts)
    models: dict[str, list[str]] = field(default_factory=dict)  # per-agent suggestions
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
        for key in ("notify_command", "terminal_command", "editor_command"):
            command = getattr(self.ui, key)
            if command:
                try:
                    tokens = shlex.split(command)
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

    def has_image_source(self) -> bool:
        p = self.project
        return bool(p.base_image or p.dockerfile or p.build)

    def resolve_path(self, value: str, repo: Path) -> Path:
        path = Path(os.path.expanduser(value))
        return path if path.is_absolute() else (repo / path)


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
        return float(value)
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
    unknown = sorted(set(data) - set(_SECTIONS) - {"models"})
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(unknown)}")
    cfg = Config()
    for name, cls in _SECTIONS.items():
        if name in data:
            setattr(cfg, name, _build_section(name, cls, data[name]))
    models = data.get("models", {})
    if not isinstance(models, dict):
        raise ConfigError("[models] must be a table of agent = [model, ...]")
    for agent, values in models.items():
        cfg.models[agent] = _coerce("models", agent, values, [])
    return cfg


def load(repo: Path, global_path: Path | None = None) -> Config:
    """Load global + project config for ``repo`` and validate the result."""
    gpath = global_path or global_config_path()
    ppath = project_config_path(repo)
    merged: dict[str, Any] = {}
    sources: list[Path] = []
    for path in (gpath, ppath):
        if path == gpath and global_path is not None and not path.is_file():
            raise ConfigError(f"{path}: config file does not exist or is not a file")
        if path.is_file():
            merged = deep_merge(merged, _read_toml(path))
            sources.append(path)
    cfg = from_dict(merged)
    cfg.sources = sources
    cfg.validate()
    return cfg


# ---------------------------------------------------------------- state


def repo_state_dir(repo: Path) -> Path:
    resolved = repo.resolve()
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:8]
    return state_home() / f"{resolved.name}-{digest}"


@dataclass
class LastUsed:
    """Launch-form values remembered per repository ("defaults that update")."""

    agent: str = ""
    effort: str | None = None  # None = never chosen; "" = explicitly agent default
    timeout: int | None = None
    model_by_agent: dict[str, str] = field(default_factory=dict)

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
        if isinstance(raw.get("timeout"), int) and raw["timeout"] >= 0:
            out.timeout = raw["timeout"]
        if isinstance(raw.get("model_by_agent"), dict):
            out.model_by_agent = {
                k: v for k, v in raw["model_by_agent"].items() if isinstance(v, str)
            }
        return out

    def save(self, repo: Path) -> None:
        path = repo_state_dir(repo) / "last_used.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            tmp.write_text(json.dumps(dataclasses.asdict(self), indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass  # remembering defaults is a convenience, never fatal


@dataclass
class LaunchDefaults:
    agent: str
    model: str
    effort: str
    timeout: int


def resolve_model(cfg: Config, last: LastUsed, agent: str) -> str:
    if agent in last.model_by_agent:
        return last.model_by_agent[agent]
    if agent == cfg.defaults.agent and cfg.defaults.model:
        return cfg.defaults.model
    return ""  # agent default; [models] entries are only autocomplete suggestions


def resolve_launch_defaults(cfg: Config, last: LastUsed) -> LaunchDefaults:
    agent = last.agent or cfg.defaults.agent
    effort = last.effort if last.effort is not None else cfg.defaults.effort
    timeout = last.timeout if last.timeout is not None else cfg.defaults.timeout
    return LaunchDefaults(
        agent=agent,
        model=resolve_model(cfg, last, agent),
        effort=effort,
        timeout=timeout,
    )


def remember(repo: Path, agent: str, model: str, effort: str, timeout: int | None) -> None:
    last = LastUsed.load(repo)
    last.agent = agent
    last.effort = effort
    if timeout is not None:
        last.timeout = timeout
    last.model_by_agent[agent] = model
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
# worktree_dir = ""                    # -> --worktree-dir (default: sibling <repo>-worktrees)
keep_workspace = true                  # -> --keep-workspace
# default_branch_start = "main"        # start revision for new branches
# main_branch = "main"                 # cleanup offers workspaces/branches fully merged into this

# [defaults]
# agent = "claude"                     # claude | codex | opencode | pi | bash
# model = ""
# effort = "medium"                    # low | medium | high | xhigh | max
"""

GLOBAL_TEMPLATE = """\
# project-sandbox-tui — global settings

[defaults]
agent = "claude"          # claude | codex | opencode | pi | bash
model = ""                # "" = the agent's own default
effort = ""               # "" or low | medium | high | xhigh | max
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
refresh_seconds = 3
bell = true
cheatsheet = true             # key cheat-sheet under the branch list (H toggles)

[models]
claude = ["opus", "sonnet", "haiku"]
codex = ["gpt-5.4", "gpt-5.4-mini"]

# [prompts]                   # templates for merging / rebasing selected branches (v / space,
#                             # then r); placeholders {inputs} {result} {base} {first} {rest}
# octopus_merge = '''...'''
# octopus_rebase = '''...'''
"""


def write_template(path: Path, template: str) -> bool:
    """Write ``template`` to ``path`` unless it exists. Returns True if written."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template, encoding="utf-8")
    return True
