"""Configuration: global + per-project TOML, merged, plus per-repo state paths.

Resolution order (later wins):  built-in defaults → global config → project config.
The "last used" launch values live in the state directory, not in TOML.
Named agent profiles are explicitly saved to the project TOML.

The project config comes with the repository, so it can be written by someone
else; it can set commands that run on the host. Only a copy the user reviewed
is loaded: trusting the file copies it into the state directory, and a changed
file is shown as a diff against that copy before it takes effect.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import difflib
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import time
import tomllib
import uuid
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from project_sandbox_tui import catalogue, commands
from project_sandbox_tui.catalogue import BUILTIN_PERSONAS, Persona
from project_sandbox_tui.launchers import TERMINALS, fill_command

# Fallbacks for when the launcher's --help cannot be read (see capabilities.py);
# kept in step with project-sandbox 0.4.1.
AGENTS: tuple[str, ...] = ("claude", "codex", "opencode", "pi", "copilot", "bash")
FAST_AGENTS: tuple[str, ...] = ("claude", "codex")
GATEWAY_AGENTS: tuple[str, ...] = ("pi", "opencode", "bash")  # --agent-proxy
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
RUNTIMES: tuple[str, ...] = ("auto", "apple-container", "docker", "podman", "colima", "chroot")
GENERATED_BUILDS: tuple[str, ...] = ("", "python-uv", "rust-cargo", "node-npm")
LEVELS: tuple[int, ...] = (1, 2, 3, 4, 5)  # security levels, more isolation upwards
EGRESS_BACKENDS: tuple[str, ...] = ("sidecar", "firewall")

PROJECT_CONFIG_NAME = ".project-sandbox-tui.toml"
TRUSTED_CONFIG_NAME = "trusted-project-config.toml"  # in the repo's state directory
APP_NAME = "project-sandbox-tui"
ConfigScope = Literal["global", "project"]


class ConfigError(ValueError):
    """Raised for invalid or inconsistent configuration."""


# Agent and runtime names are checked for form only: which ones the launcher
# supports is discovered from it (capabilities.py) and checked by doctor and
# before a launch, so newer launcher agents work without an sbx-tui release.
NAME_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,31}")


def valid_name(value: object) -> bool:
    return isinstance(value, str) and NAME_PATTERN.fullmatch(value) is not None


def _check_name(key: str, value: str, what: str = "an agent name") -> None:
    if not valid_name(value):
        raise ConfigError(f"{key}: {value!r} is not {what} (lowercase letters, digits and dashes)")


# ---------------------------------------------------------------- sections


@dataclass
class Defaults:
    agent: str = "claude"
    model: str = ""  # "" = the agent's own default
    effort: str = ""  # "" = the agent's own default
    timeout: int = 0  # seconds for unsupervised runs; 0 = no timeout
    fast: bool = False  # Codex and Claude; requires project-sandbox >= 0.3.1
    gateway: bool = False  # pi / opencode / bash through network.agent_proxy


@dataclass
class AgentProfile:
    agent: str = "claude"
    model: str = ""
    effort: str = ""
    fast: bool = False
    extra_args: list[str] = field(default_factory=list)
    gateway: bool = False  # launch through network.agent_proxy (pi, opencode, bash)

    def validate(self, where: str) -> None:
        _check_name(f"{where}.agent", self.agent)
        if self.effort and self.effort not in EFFORTS:
            raise ConfigError(f"{where}.effort must be empty or one of {', '.join(EFFORTS)}")
        if any(ch.isspace() for ch in self.model):
            raise ConfigError(f"{where}.model: invalid model name {self.model!r}")
        if any("\0" in arg for arg in self.extra_args):
            raise ConfigError(f"{where}.extra_args must not contain NUL characters")


def validate_profile_name(name: str) -> None:
    if not name.strip() or name != name.strip() or not name.isprintable():
        raise ConfigError("profile name must contain printable text without surrounding spaces")
    if name.casefold() == "custom":
        raise ConfigError("profile name 'custom' is reserved for manual launch settings")


@dataclass
class CommitReview:
    profile: str = ""
    provider: str = ""  # provider row for the commit-reviewer persona ("" = codex)


@dataclass
class Security:
    level: int = 0  # project default security level 1-5; 0 = not set (earlier behaviour)
    egress: str = "sidecar"  # backend for levels 3-5: sidecar | firewall (global config only)


@dataclass
class Catalogue:
    default_provider: str = ""  # provider row selections start with ("" = first available)
    gateway_agent: str = "pi"  # agent of the gateway rows: pi | opencode
    # [catalogue.models.<vendor|row>] tier = "model"; "" disables the tier
    models: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class Network:
    internet_proxy: str = ""  # --internet-proxy URL
    agent_proxy: str = ""  # --agent-proxy URL
    agent_proxy_key_env: str = ""  # --agent-proxy-key-env NAME
    ca_certs: list[str] = field(default_factory=list)  # --ca-cert PATH (repeatable)
    extra_domains: list[str] = field(default_factory=list)  # --extra-domain (repeatable)
    allow_github: bool = False  # --allow-github
    no_firewall: bool = False  # --no-firewall
    no_internet_proxy_preflight: bool = False  # --no-internet-proxy-preflight
    build_proxy: str = ""  # --build-proxy URL (image builds only)
    build_no_proxy: list[str] = field(default_factory=list)  # --build-no-proxy (repeatable)
    build_ca_certs: list[str] = field(default_factory=list)  # --build-ca-cert PATH (repeatable)


# Corporate network settings: read only from the global config while a project
# security level is in effect (security.egress always is).
GLOBAL_ONLY_NETWORK: tuple[str, ...] = (
    "internet_proxy",
    "no_internet_proxy_preflight",
    "ca_certs",
    "build_proxy",
    "build_no_proxy",
    "build_ca_certs",
)


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
class Feedback:
    enabled: bool = True
    require_reported_success: bool = False
    pass_state_keys: list[str] = field(default_factory=list)
    retry_outcomes: list[str] = field(default_factory=list)
    max_attempts: int = 1
    on_success: str = ""  # configured action name
    on_failure: str = ""
    on_blocked: str = ""
    max_steps: int = 1

    @property
    def makes_decisions(self) -> bool:
        return bool(
            self.require_reported_success
            or self.retry_outcomes
            or self.on_success
            or self.on_failure
            or self.on_blocked
        )

    @property
    def requires_reporting(self) -> bool:
        return self.makes_decisions or bool(self.pass_state_keys)


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
    deploy_config_skill: bool = True  # copy the bundled configuration skills into workspaces


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
    profile: str = ""  # [profiles.NAME]; excludes agent, model, effort and persona
    persona: str = ""  # [personas.NAME] or a built-in persona; excludes agent, model, effort
    level: int | None = None  # security level 1-5; None = the persona's, else the project's

    @property
    def has_own_settings(self) -> bool:
        """The action sets its own agent, model or effort instead of a profile."""
        return bool(self.agent or self.model or self.effort)

    def render(self, bookmark: str, main: str, extra: str = "") -> str:
        values = {"bookmark": bookmark, "main": main}
        text = re.sub(r"\{(bookmark|main)\}", lambda m: values[m[1]], self.prompt).rstrip()
        if extra.strip():
            text += f"\n\n{extra.strip()}"
        return text + "\n"

    def to_dict(self) -> dict[str, Any]:
        """The keys a config file sets, for ``sbx-tui config list``."""
        out: dict[str, Any] = {"prompt": self.prompt}
        out.update(
            {
                k: v
                for k in ("agent", "model", "effort", "profile", "persona")
                if (v := getattr(self, k))
            }
        )
        for key in ("timeout", "level"):
            if (value := getattr(self, key)) is not None:
                out[key] = value
        return out


HUMAN_REVIEW = "human-review"
HOST_STEP_PREFIX = "host:"


@dataclass
class Workflow:
    planning_action: str = ""
    steps: list[str] = field(default_factory=list)
    pass_state_keys: list[str] = field(default_factory=list)
    result_state_keys: list[str] = field(default_factory=list)
    first_step_interactive: bool = True


# Where workspace skills may be deployed: one directory directly below one of these.
SKILL_ROOTS: tuple[str, ...] = (".agents/skills", ".claude/skills")
SAFETY_SKILL = "sbx-tui-safety"
CONFIG_SKILLS = {
    name: tuple(f"{root}/{name}" for root in SKILL_ROOTS)
    for name in ("sbx-tui-config", SAFETY_SKILL)
}


@dataclass
class WorkspaceSkill:
    """``[[workspace_skills]]``: a skill directory copied into bookmark workspaces.

    ``source`` is a host path (``~`` and absolute, or relative to the repository);
    each target is ``<root>/<name>`` for a root in :data:`SKILL_ROOTS`.
    """

    source: str = ""
    targets: list[str] = field(default_factory=list)


WORKSPACE_SKILL_KEYS: dict[str, Any] = {"source": "", "targets": []}


def validate_skill_target(target: str, where: str) -> None:
    root, _, name = target.rpartition("/")
    roots = " or ".join(f"{r}/" for r in SKILL_ROOTS)
    if root not in SKILL_ROOTS:
        raise ConfigError(f"{where}: {target!r} must be one directory directly in {roots}")
    if (
        not name
        or name in (".", "..")
        or "\\" in name
        or not name.isprintable()
        or name != name.strip()
    ):
        raise ConfigError(f"{where}: {target!r} needs a plain directory name in {root}/")


ACTION_KEYS: dict[str, Any] = {
    "prompt": "",
    "agent": "",
    "model": "",
    "effort": "",
    "timeout": 0,
    "profile": "",
    "persona": "",
    "level": 0,
}

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
    feedback: Feedback = field(default_factory=Feedback)
    commit_review: CommitReview = field(default_factory=CommitReview)
    security: Security = field(default_factory=Security)
    catalogue: Catalogue = field(default_factory=Catalogue)
    # Built-in personas merged field by field with [personas.NAME] (see catalogue.py).
    personas: dict[str, Persona] = field(default_factory=lambda: dict(BUILTIN_PERSONAS))
    persona_config: dict[str, dict[str, Any]] = field(default_factory=dict)  # as configured
    models: dict[str, list[str]] = field(default_factory=dict)  # extra suggestions (models.py)
    commands: dict[str, str] = field(  # :name -> shell command (commands.py)
        default_factory=lambda: dict(DEFAULT_COMMANDS)
    )
    leader: dict[str, str] = field(default_factory=dict)  # keys after ui.leader -> command line
    actions: dict[str, Action] = field(default_factory=dict)  # :name -> sandbox run per target
    workflows: dict[str, Workflow] = field(default_factory=dict)
    profiles: dict[str, AgentProfile] = field(default_factory=dict)
    workspace_skills: list[WorkspaceSkill] = field(default_factory=list)  # [[workspace_skills]]
    sources: list[Path] = field(default_factory=list)  # files that were loaded
    # List the project scope first when both files define the profile.
    profile_sources: dict[str, tuple[ConfigScope, ...]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    workflow_problems: list[tuple[Path, str]] = field(default_factory=list)

    # ---------------------------------------------------------- helpers

    def commit_summary_profile(self) -> tuple[str, AgentProfile]:
        """The commit reviewer's agent settings: ``commit_review.profile``, else the
        internal commit-reviewer persona on ``commit_review.provider`` (Codex)."""
        name = self.commit_review.profile
        if not name:
            row = catalogue.ROW_BY_ID[self.commit_review.provider or "codex"]
            persona = self.personas[catalogue.COMMIT_REVIEWER]
            model = catalogue.model_for(self, row, persona.tier)
            if model is None:
                raise ConfigError(
                    f"commit_review.provider: {catalogue.tier_problem(self, row, persona.tier)}"
                    " for the commit reviewer; select another provider"
                )
            if row.gateway and not self.network.agent_proxy:
                raise ConfigError(
                    f"commit_review.provider {row.id!r} uses the agent gateway, "
                    "but network.agent_proxy is empty"
                )
            return f"{catalogue.COMMIT_REVIEWER} on {row.label}", AgentProfile(
                agent=catalogue.row_agent(self, row),
                model=model,
                effort=persona.effort if row.effort else "",
                fast=persona.fast and row.fast,
                gateway=row.gateway,
            )
        profile = self.profiles.get(name)
        if profile is None:
            raise ConfigError(
                f"commit_review.profile {name!r} is missing; select an existing profile"
            )
        if profile.agent == "bash":
            raise ConfigError(f"commit_review.profile {name!r} uses bash; select a model agent")
        return name, dataclasses.replace(profile, extra_args=list(profile.extra_args))

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
        _check_name("defaults.agent", d.agent)
        if d.effort and d.effort not in EFFORTS:
            raise ConfigError(f"defaults.effort must be empty or one of {', '.join(EFFORTS)}")
        if d.timeout < 0:
            raise ConfigError("defaults.timeout must be >= 0")
        _check_name("sandbox.runtime", s.runtime, "a runtime name")
        if s.cpus < 0:
            raise ConfigError("sandbox.cpus must be >= 0")
        f = self.feedback
        if not 1 <= f.max_attempts <= 100 or not 1 <= f.max_steps <= 100:
            raise ConfigError("feedback.max_attempts and max_steps must be between 1 and 100")
        if any(o not in ("failure", "blocked") for o in f.retry_outcomes):
            raise ConfigError("feedback.retry_outcomes accepts failure and blocked")
        if any(not key for key in f.pass_state_keys):
            raise ConfigError("feedback.pass_state_keys must contain nonempty keys")
        for name in (f.on_success, f.on_failure, f.on_blocked):
            if name and name not in self.actions:
                raise ConfigError(f"feedback successor action does not exist: {name}")
        if not f.enabled and f.requires_reporting:
            raise ConfigError("feedback policies require feedback.enabled")
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
            _check_name(f"models.{agent}", agent)
        for name, profile in self.profiles.items():
            validate_profile_name(name)
            profile.validate(f"profiles.{name}")
        uses_gateway = [f"profiles.{n}.gateway" for n, pr in self.profiles.items() if pr.gateway]
        if d.gateway:
            uses_gateway.insert(0, "defaults.gateway")
        if uses_gateway and not self.network.agent_proxy:
            raise ConfigError(f"{', '.join(uses_gateway)} needs network.agent_proxy")
        if not math.isfinite(self.ui.refresh_seconds) or self.ui.refresh_seconds <= 0:
            raise ConfigError("ui.refresh_seconds must be finite and > 0")
        for name, action in self.actions.items():
            if not action.prompt.strip():
                raise ConfigError(f"[actions.{name}] needs a prompt")
            if action.agent:
                _check_name(f"actions.{name}.agent", action.agent)
            if action.effort and action.effort not in EFFORTS:
                raise ConfigError(
                    f"actions.{name}.effort must be empty or one of {', '.join(EFFORTS)}"
                )
            if action.timeout is not None and action.timeout < 0:
                raise ConfigError(f"actions.{name}.timeout must be >= 0")
            if action.level is not None and action.level not in LEVELS:
                raise ConfigError(f"actions.{name}.level must be between 1 and 5")
            if action.persona:
                persona = self.personas.get(action.persona)
                if persona is None or persona.internal:
                    offered = ", ".join(catalogue.offered_personas(self.personas))
                    raise ConfigError(
                        f"actions.{name}.persona: unknown persona {action.persona!r}; "
                        f"choose one of {offered} or define [personas.{action.persona}]"
                    )
                fields = [k for k in ("profile", "agent", "model", "effort") if getattr(action, k)]
                if fields:
                    raise ConfigError(
                        f"[actions.{name}] sets persona and {', '.join(fields)}: "
                        "use either a persona, a profile or the action's own agent settings"
                    )
            if action.profile:
                # Existence is checked where the profile is used: profiles can be
                # deleted at runtime, and a missing one warns and falls back.
                try:
                    validate_profile_name(action.profile)
                except ConfigError as exc:
                    raise ConfigError(f"actions.{name}.profile: {exc}") from exc
                if action.has_own_settings:
                    fields = [k for k in ("agent", "model", "effort") if getattr(action, k)]
                    raise ConfigError(
                        f"[actions.{name}] sets profile and {', '.join(fields)}: "
                        "use either a profile or the action's own agent settings"
                    )
        self._validate_security()
        self._validate_workflows()
        self._validate_workspace_skills()
        try:
            commands.validate(self.commands, self.ui.leader, self.leader, self.actions)
        except commands.CommandError as exc:
            raise ConfigError(str(exc)) from exc

    def _validate_security(self) -> None:
        sec, cat, review = self.security, self.catalogue, self.commit_review
        if sec.level and sec.level not in LEVELS:
            raise ConfigError("security.level must be between 1 and 5")
        if sec.egress not in EGRESS_BACKENDS:
            raise ConfigError(f"security.egress must be one of {', '.join(EGRESS_BACKENDS)}")
        rows = ", ".join(catalogue.ROW_BY_ID)
        for key, value in (
            ("catalogue.default_provider", cat.default_provider),
            ("commit_review.provider", review.provider),
        ):
            if value and value not in catalogue.ROW_BY_ID:
                raise ConfigError(f"{key}: unknown provider {value!r}; choose one of {rows}")
        if review.provider and review.profile:
            raise ConfigError(
                "commit_review sets profile and provider: use either a profile or a provider"
            )
        if cat.gateway_agent not in catalogue.GATEWAY_CHOICES:
            raise ConfigError(
                f"catalogue.gateway_agent must be one of {', '.join(catalogue.GATEWAY_CHOICES)}"
            )
        for key, tiers in cat.models.items():
            where = f"catalogue.models.{key if key.isprintable() else repr(key)}"
            if key not in catalogue.VENDORS and key not in catalogue.ROW_BY_ID:
                raise ConfigError(
                    f"{where}: unknown vendor or provider; use one of "
                    f"{', '.join(catalogue.VENDORS)}, {rows}"
                )
            for tier, model in tiers.items():
                if tier not in catalogue.TIERS:
                    raise ConfigError(
                        f"{where}.{tier}: unknown tier; use {', '.join(catalogue.TIERS)}"
                    )
                if any(ch.isspace() for ch in model):
                    raise ConfigError(f"{where}.{tier}: invalid model name {model!r}")

    def _validate_workflows(self) -> None:
        if self.workflows and not self.feedback.enabled:
            raise ConfigError("workflows require feedback.enabled")
        for name, workflow in self.workflows.items():
            where = f"workflows.{name}"
            if not commands.NAME.fullmatch(name):
                raise ConfigError(
                    f"[{where}] name must start with a letter and use letters, digits, _ or -"
                )
            if not workflow.planning_action or not 1 <= len(workflow.steps) <= 99:
                raise ConfigError(f"[{where}] needs a planning_action and 1 to 99 steps")
            if workflow.planning_action not in self.actions:
                raise ConfigError(
                    f"[{where}] refers to unknown action {workflow.planning_action!r}"
                )
            steps: list[str] = []
            for step in workflow.steps:
                if step.startswith(HOST_STEP_PREFIX):
                    command = step.removeprefix(HOST_STEP_PREFIX)
                    if command not in self.commands:
                        raise ConfigError(f"[{where}] refers to unknown host command {command!r}")
                    if not steps or steps[-1] != HUMAN_REVIEW:
                        steps.append(HUMAN_REVIEW)
                elif step != HUMAN_REVIEW and step not in self.actions:
                    raise ConfigError(f"[{where}] refers to unknown action {step!r}")
                steps.append(step)
            if len(steps) > 99:
                raise ConfigError(f"[{where}] allows at most 99 steps including human reviews")
            workflow.steps = steps
            for field_name in ("pass_state_keys", "result_state_keys"):
                if any(not key.strip() for key in getattr(workflow, field_name)):
                    raise ConfigError(f"[{where}] {field_name} must contain nonempty keys")

    def _validate_workspace_skills(self) -> None:
        seen: dict[str, str] = {}
        if self.project.deploy_config_skill:
            seen = {t: "project.deploy_config_skill" for ts in CONFIG_SKILLS.values() for t in ts}
        for index, skill in enumerate(self.workspace_skills):
            where = f"workspace_skills[{index}]"
            if not skill.source.strip() or "\0" in skill.source:
                raise ConfigError(f"{where}.source must name a skill directory")
            if not skill.targets:
                raise ConfigError(f"{where}.targets must list at least one target directory")
            for target in skill.targets:
                validate_skill_target(target, f"{where}.targets")
                if target in seen:
                    raise ConfigError(f"{where}.targets: {target!r} is also used by {seen[target]}")
                seen[target] = where

    def has_image_source(self) -> bool:
        p = self.project
        return bool(p.base_image or p.dockerfile or p.build)

    def resolve_path(self, value: str, repo: Path) -> Path:
        path = Path(os.path.expanduser(value))
        return path if path.is_absolute() else (repo / path)


# Free-form tables: what their entries are, for error messages.
TABLES: dict[str, str] = {
    "profiles": "must be a table of [profiles.NAME] tables",
    "models": "must be a table of agent = [model, ...]",
    "commands": 'must be a table of name = "shell command"',
    "leader": 'must be a table of keys = "command line"',
    "actions": "must be a table of [actions.NAME] tables with a prompt",
    "workflows": "must be a table of [workflows.NAME] tables",
    "workspace_skills": "must be an array of [[workspace_skills]] tables with source and targets",
    "personas": "must be a table of [personas.NAME] tables",
    "catalogue": "must be a table with default_provider, gateway_agent and [catalogue.models.*]",
}
CATALOGUE_KEYS: dict[str, Any] = {"default_provider": "", "gateway_agent": "pi", "models": {}}

_SECTIONS: dict[str, type[Any]] = {
    "defaults": Defaults,
    "network": Network,
    "sandbox": Sandbox,
    "project": Project,
    "ui": UI,
    "prompts": Prompts,
    "feedback": Feedback,
    "commit_review": CommitReview,
    "security": Security,
}
_NAMED_SECTION_KEYS = {
    "actions": set(ACTION_KEYS),
    "personas": set(catalogue.PERSONA_KEYS),
    "profiles": {f.name for f in dataclasses.fields(AgentProfile)},
    "workflows": {f.name for f in dataclasses.fields(Workflow)},
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
    for key, value in data.items():
        setattr(instance, key, _coerce(name, key, value, getattr(instance, key)))
    return instance


def _check_config_keys(
    data: dict[str, Any], warn: Callable[[str], None] | None = None
) -> dict[str, Any]:
    """Check schema keys while leaving value validation to the merged config."""

    def check(table: Any, known: set[str], message: str) -> Any:
        if not isinstance(table, dict):
            return table
        unknown = sorted(set(table) - known)
        if not unknown:
            return table
        names = ", ".join(key if key.isprintable() else repr(key) for key in unknown)
        detail = f"{message}: {names}"
        if warn is None:
            raise ConfigError(detail)
        warn(detail)
        return {key: value for key, value in table.items() if key in known}

    result = dict(check(data, set(_SECTIONS) | set(TABLES), "unknown section(s)"))
    for name, cls in _SECTIONS.items():
        if name in result:
            keys = {f.name for f in dataclasses.fields(cls)}
            result[name] = check(result[name], keys, f"[{name}] has unknown key(s)")
    for name, keys in _NAMED_SECTION_KEYS.items():
        tables = result.get(name)
        if isinstance(tables, dict):
            result[name] = {
                entry: check(
                    table,
                    keys,
                    f"[{name}.{entry if entry.isprintable() else repr(entry)}] has unknown key(s)",
                )
                for entry, table in tables.items()
            }
    if "catalogue" in result:
        result["catalogue"] = check(
            result["catalogue"], set(CATALOGUE_KEYS), "[catalogue] has unknown key(s)"
        )
    skills = result.get("workspace_skills")
    if isinstance(skills, list):
        keys = set(WORKSPACE_SKILL_KEYS)
        result["workspace_skills"] = [
            check(table, keys, f"workspace_skills[{index}] has unknown key(s)")
            for index, table in enumerate(skills)
        ]
    return result


def merge_config(
    global_data: dict[str, Any], project_data: dict[str, Any], project_source: Path | None
) -> Config:
    if project_source is None:
        return from_dict(global_data)
    warnings: list[str] = []
    project_data = _check_config_keys(project_data, warnings.append)
    cfg = from_dict(deep_merge(global_data, project_data))
    _check_global_only(cfg, project_data)
    cfg.warnings = [f"{project_source}: {warning} (ignored)" for warning in warnings]
    return cfg


def _check_global_only(cfg: Config, project_data: dict[str, Any]) -> None:
    """Corporate network settings describe this machine, not the repository."""
    security = project_data.get("security")
    if isinstance(security, dict) and "egress" in security:
        raise ConfigError(
            "security.egress describes this machine's container runtime: "
            "set it in the global config, not the project config"
        )
    network = project_data.get("network")
    if not cfg.security.level or not isinstance(network, dict):
        return
    for key in GLOBAL_ONLY_NETWORK:
        if key in network:
            raise ConfigError(
                f"network.{key} is a corporate network setting: with security.level set, "
                "set it in the global config, not the project config"
            )


def from_dict(data: dict[str, Any]) -> Config:
    data = _check_config_keys(data)
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
        if name == "commands" and any("/" in k for k in table):
            raise ConfigError("[commands] names cannot contain /")
        getattr(cfg, name).update({k: _coerce(name, k, v, "") for k, v in table.items()})
    actions = data.get("actions", {})
    if not isinstance(actions, dict):
        raise ConfigError(f"[actions] {TABLES['actions']}")
    for name, table in actions.items():
        if "/" in name:
            raise ConfigError(f"[actions] invalid name {name!r}")
        cfg.actions[name] = _build_action(name, table)
    security = data.get("security")
    if isinstance(security, dict) and security.get("level") == 0:
        raise ConfigError("security.level must be between 1 and 5")
    catalogue_table = data.get("catalogue", {})
    if not isinstance(catalogue_table, dict):
        raise ConfigError(f"[catalogue] {TABLES['catalogue']}")
    cfg.catalogue = _build_catalogue(catalogue_table)
    personas = data.get("personas", {})
    if not isinstance(personas, dict):
        raise ConfigError(f"[personas] {TABLES['personas']}")
    for name, table in personas.items():
        cfg.personas[name] = _build_persona(name, table)
        cfg.persona_config[name] = dict(table)
    workflows = data.get("workflows", {})
    if not isinstance(workflows, dict):
        raise ConfigError(f"[workflows] {TABLES['workflows']}")
    for name, table in workflows.items():
        cfg.workflows[name] = _build_section(f"workflows.{name}", Workflow, table)
    profiles = data.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ConfigError(f"[profiles] {TABLES['profiles']}")
    for name, table in profiles.items():
        cfg.profiles[name] = _build_section(f"profiles.{name}", AgentProfile, table)
    skills = data.get("workspace_skills", [])
    if not isinstance(skills, list):
        raise ConfigError(f"workspace_skills {TABLES['workspace_skills']}")
    for index, table in enumerate(skills):
        where = f"workspace_skills[{index}]"
        if not isinstance(table, dict):
            raise ConfigError(f"{where} must be a table with source and targets")
        values = {k: _coerce(where, k, v, WORKSPACE_SKILL_KEYS[k]) for k, v in table.items()}
        cfg.workspace_skills.append(WorkspaceSkill(**values))
    return cfg


def _build_catalogue(data: dict[str, Any]) -> Catalogue:
    out = Catalogue()
    for key in ("default_provider", "gateway_agent"):
        if key in data:
            setattr(out, key, _coerce("catalogue", key, data[key], ""))
    models = data.get("models", {})
    if not isinstance(models, dict):
        raise ConfigError("[catalogue.models] must be a table of [catalogue.models.NAME] tables")
    for key, tiers in models.items():
        where = f"catalogue.models.{key if key.isprintable() else repr(key)}"
        if not isinstance(tiers, dict):
            raise ConfigError(f'[{where}] must be a table of tier = "model"')
        out.models[key] = {tier: _coerce(where, tier, value, "") for tier, value in tiers.items()}
    return out


def _build_persona(name: str, data: Any) -> Persona:
    """``[personas.NAME]`` over the built-in persona of that name, field by field."""
    where = f"personas.{name if isinstance(name, str) and name.isprintable() else repr(name)}"
    if not valid_name(name):
        raise ConfigError(f"{where}: persona names use lowercase letters, digits and dashes")
    if not isinstance(data, dict):
        raise ConfigError(f'[{where}] must be a table, e.g. [{where}] tier = "high"')
    values = {k: _coerce(where, k, v, catalogue.PERSONA_KEYS[k]) for k, v in data.items()}
    base = BUILTIN_PERSONAS.get(name)
    if base is None and "tier" not in values:
        raise ConfigError(
            f"{where}.tier is required for a new persona: {', '.join(catalogue.TIERS)}"
        )
    if "tier" in values and values["tier"] not in catalogue.TIERS:
        raise ConfigError(f"{where}.tier must be one of {', '.join(catalogue.TIERS)}")
    if values.get("effort", "") and values["effort"] not in EFFORTS:
        raise ConfigError(f"{where}.effort must be empty or one of {', '.join(EFFORTS)}")
    if "level" in values and values["level"] not in LEVELS:
        raise ConfigError(f"{where}.level must be between 1 and 5")
    if base is None:
        return Persona(**values)
    return dataclasses.replace(base, **values)


def _build_action(name: str, data: Any) -> Action:
    where = f"actions.{name}"
    if not isinstance(data, dict):
        raise ConfigError(f'[{where}] must be a table, e.g. [{where}] prompt = "…"')
    values = {k: _coerce(where, k, v, ACTION_KEYS[k]) for k, v in data.items()}
    return Action(**values)


def parse_workflow_file(
    text: str, path: Path
) -> tuple[Workflow, dict[str, Action], dict[str, str], list[str]]:
    """Parse a standalone workflow and ignore unknown keys with warnings."""
    try:
        data = tomllib.loads(text)
    except ValueError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    warnings: list[str] = []
    for key in data.keys() - {"workflow", "actions", "commands", "source"}:
        warnings.append(f"{path}: unknown section or key {key} (ignored)")
    source_data = data.get("source", {})
    if isinstance(source_data, dict):
        for key in source_data.keys() - {"url", "path", "imported_at", "sha256"}:
            warnings.append(f"{path}: [source] has unknown key {key} (ignored)")
    elif "source" in data:
        raise ConfigError(f"{path}: [source] must be a table")
    workflow_data = data.get("workflow")
    if not isinstance(workflow_data, dict):
        raise ConfigError(f"{path}: missing [workflow] table")
    workflow_keys = {f.name for f in dataclasses.fields(Workflow)}
    for key in workflow_data.keys() - workflow_keys:
        warnings.append(f"{path}: [workflow] has unknown key {key} (ignored)")
    workflow = _build_section(
        "workflow", Workflow, {k: v for k, v in workflow_data.items() if k in workflow_keys}
    )
    action_data = data.get("actions", {})
    if not isinstance(action_data, dict):
        raise ConfigError(f"{path}: [actions] must be a table")
    actions: dict[str, Action] = {}
    for name, table in action_data.items():
        if not commands.NAME.fullmatch(name):
            raise ConfigError(f"{path}: invalid action name {name!r}")
        if not isinstance(table, dict):
            raise ConfigError(f"{path}: [actions.{name}] must be a table")
        if table.keys() & {"profile", "agent", "model", "effort"}:
            raise ConfigError(f"{path}: workflow file actions select agents with persona only")
        for key in table.keys() - ACTION_KEYS.keys():
            warnings.append(f"{path}: [actions.{name}] has unknown key {key} (ignored)")
        actions[name] = _build_action(name, {k: v for k, v in table.items() if k in ACTION_KEYS})
    command_data = data.get("commands", {})
    if not isinstance(command_data, dict):
        raise ConfigError(f"{path}: [commands] must be a table")
    bundled_commands: dict[str, str] = {}
    for name, value in command_data.items():
        if not commands.NAME.fullmatch(name):
            raise ConfigError(f"{path}: invalid command name {name!r}")
        bundled_commands[name] = _coerce("commands", name, value, "")
    return workflow, actions, bundled_commands, warnings


def load(repo: Path, global_path: Path | None = None) -> Config:
    """Load global + the trusted project config for ``repo`` and validate the result.

    A project config that changed since it was trusted is ignored in favour of
    the trusted copy (see :class:`ProjectTrust`); one never trusted is ignored.
    """
    trust = ProjectTrust.check(repo)
    if trust.current is None or trust.trusted is None:
        cfg = _load(global_path, None, None, defer_leader=True)
    else:
        cfg = _load(
            global_path,
            trust.trusted,
            trusted_config_path(repo),
            project_path=trust.path,
            defer_leader=True,
        )
    return _merge_workflow_files(cfg, repo)


def _merge_workflow_files(cfg: Config, repo: Path) -> Config:
    leader = cfg.leader
    cfg.leader = {}
    files, warnings = WorkflowTrust.scan(repo)
    cfg.warnings.extend(warnings)
    for file in files:
        name = file.path.stem
        text = file.trusted
        if file.needs_review:
            cfg.warnings.append(file.describe())
        if text is None:
            continue
        if name in cfg.workflows:
            raise ConfigError(f"[workflows.{name}] conflicts with {file.path}")
        try:
            workflow, actions, bundled, file_warnings = parse_workflow_file(text, file.path)
            for action_name in [workflow.planning_action, *workflow.steps]:
                if action_name == HUMAN_REVIEW:
                    continue
                if action_name.startswith(HOST_STEP_PREFIX):
                    short = action_name.removeprefix(HOST_STEP_PREFIX)
                    if short not in bundled:
                        raise ConfigError(f"unknown host command {short!r}")
                elif action_name not in actions:
                    raise ConfigError(f"unknown action {action_name!r}")
            workflow.planning_action = f"{name}/{workflow.planning_action}"
            workflow.steps = [
                step
                if step == HUMAN_REVIEW
                else f"host:{name}/{step[5:]}"
                if step.startswith(HOST_STEP_PREFIX)
                else f"{name}/{step}"
                for step in workflow.steps
            ]
            candidate = copy.deepcopy(cfg)
            candidate.actions.update({f"{name}/{k}": v for k, v in actions.items()})
            candidate.commands.update({f"{name}/{k}": v for k, v in bundled.items()})
            candidate.workflows[name] = workflow
            candidate.validate()
        except ConfigError as exc:
            cfg.workflow_problems.append((file.path, str(exc)))
            continue
        cfg = candidate
        cfg.warnings.extend(file_warnings)
        cfg.sources.append(file.trusted_path)
    cfg.leader = leader
    cfg.validate()
    return cfg


def load_candidate(trust: ProjectTrust, global_path: Path | None = None) -> Config:
    """Load the project config under review, to validate it before trusting it."""
    cfg = _load(global_path, trust.current, trust.path, defer_leader=True)
    return _merge_workflow_files(cfg, trust.path.parent)


def _load(
    global_path: Path | None,
    project_text: str | None,
    project_source: Path | None,
    *,
    project_path: Path | None = None,
    defer_leader: bool = False,
) -> Config:
    gpath = global_path or global_config_path()
    global_data: dict[str, Any] = {}
    project_data: dict[str, Any] = {}
    sources: list[Path] = []
    if global_path is not None and not gpath.is_file():
        raise ConfigError(f"{gpath}: config file does not exist or is not a file")
    if gpath.is_file():
        global_data = _read_toml(gpath)
        sources.append(gpath)
    if project_text is not None and project_source is not None:
        try:
            project_data = tomllib.loads(project_text)
        except ValueError as exc:
            raise ConfigError(f"{project_source}: {exc}") from exc
        sources.append(project_source)
    cfg = merge_config(global_data, project_data, project_path or project_source)
    cfg.sources = sources
    profile_tables: dict[ConfigScope, Any] = {
        "project": project_data.get("profiles"),
        "global": global_data.get("profiles"),
    }
    cfg.profile_sources = {
        name: tuple(
            scope
            for scope, table in profile_tables.items()
            if isinstance(table, dict) and isinstance(table.get(name), dict)
        )
        for name in cfg.profiles
    }
    leader = cfg.leader
    if defer_leader:
        cfg.leader = {}
    cfg.validate()
    cfg.leader = leader
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
class FileTrust:
    """A file and the copy the user last trusted."""

    path: Path
    trusted_path: Path
    current: str | None  # its contents; None = the repository has no project config
    trusted: str | None  # the reviewed copy; None = never trusted

    @classmethod
    def check(cls, path: Path, trusted_path: Path) -> FileTrust:
        return cls(path, trusted_path, _read_text(path), _read_text(trusted_path))

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

    def trust(self, repo: Path | None = None) -> None:
        """Trust exactly the reviewed text (not whatever the file holds by now)."""
        if self.current is None:
            raise ConfigError(f"{self.path}: nothing to trust")
        try:
            with config_lock():
                path = self.trusted_path
                private_dir(path.parent)
                baseline = _read_text(path)
                if baseline == self.current:
                    return
                if baseline != self.trusted:
                    raise ConfigError("trusted config changed during review; review the diff again")
                _write_trusted_file(path, self.current)
        except OSError as exc:
            raise ConfigError(f"cannot write {self.trusted_path}: {exc}") from exc

    def describe(self) -> str:
        """One line for the user: why the project config is not in effect."""
        if self.trusted is None:
            return f"{self.path} is not trusted yet and is ignored"
        return f"{self.path} changed since it was trusted; using the trusted version"


@dataclass(frozen=True)
class ProjectTrust(FileTrust):
    """The repository's project config and its trusted copy."""

    @classmethod
    def check(cls, path: Path, trusted_path: Path | None = None) -> ProjectTrust:
        repo = path
        path = project_config_path(repo)
        target = trusted_path or trusted_config_path(repo)
        return cls(path, target, _read_text(path), _read_text(target))


def workflow_dir(repo: Path) -> Path:
    return repo / ".project-sandbox-tui" / "workflows"


@dataclass(frozen=True)
class WorkflowTrust(FileTrust):
    """Trust state for one discovered workflow file."""

    @classmethod
    def scan(cls, repo: Path) -> tuple[list[WorkflowTrust], list[str]]:
        directory = workflow_dir(repo)
        files: list[WorkflowTrust] = []
        warnings: list[str] = []
        if not directory.exists():
            return files, warnings
        for path in sorted(directory.glob("*.toml")):
            if not commands.NAME.fullmatch(path.stem):
                warnings.append(f"{path}: invalid workflow name (ignored)")
            elif path.is_symlink() or not path.is_file():
                warnings.append(f"{path}: not a regular file (ignored)")
            else:
                target = repo_state_dir(repo) / "trusted-workflows" / path.name
                files.append(cls(path, target, _read_text(path), _read_text(target)))
        return files, warnings


# ---------------------------------------------------------------- state


def write_trusted_config(repo: Path, text: str) -> None:
    """Store the approved text atomically. The caller holds config_lock()."""
    _write_trusted_file(trusted_config_path(repo), text)


def _write_trusted_file(path: Path, text: str) -> None:
    private_dir(path.parent)
    tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def repo_state_dir(repo: Path) -> Path:
    resolved = repo.resolve()
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:8]
    return state_home() / f"{resolved.name}-{digest}"


def private_dir(path: Path) -> Path:
    """Create ``path`` for the owner only: state holds transcripts, prompts and job
    argv. Also tightens a directory an earlier version created with the umask.

    Missing ancestors (such as ``$XDG_STATE_HOME`` itself) are created 0700 as the
    XDG spec asks; ``mkdir(parents=True)`` would apply the umask, and project-sandbox
    refuses private storage under a group- or world-writable ancestor."""
    for missing in reversed([p for p in path.parents if not p.exists()]):
        with contextlib.suppress(FileExistsError):  # created concurrently
            missing.mkdir(mode=0o700)
    path.mkdir(exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):  # e.g. a directory someone else owns
        path.chmod(0o700)
    return path


@contextlib.contextmanager
def state_lock(path: Path) -> Generator[None]:
    private_dir(path.parent)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def config_lock() -> contextlib.AbstractContextManager[None]:
    return state_lock(state_home() / "config.lock")


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
        if durable:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
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
    gateway_by_agent: dict[str, bool] = field(default_factory=dict)
    host_command: list[str] = field(default_factory=list)
    profile: str | None = None  # None = never chosen; "" = custom
    extra_args: list[str] = field(default_factory=list)  # custom launch switches
    selection: str | None = None  # "persona" = provider + persona; None = profile / custom
    provider: str = ""  # provider row of the last persona launch
    persona: str = ""

    @classmethod
    def load(cls, repo: Path) -> LastUsed:
        path = repo_state_dir(repo) / "last_used.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        return cls._from_dict(raw)

    @classmethod
    def _from_dict(cls, raw: Any) -> LastUsed:
        if not isinstance(raw, dict):
            return cls()
        out = cls()
        if isinstance(raw.get("profile"), str):
            out.profile = raw["profile"]
        if raw.get("selection") == "persona":
            out.selection = "persona"
        provider = raw.get("provider")
        if isinstance(provider, str) and provider in catalogue.ROW_BY_ID:
            out.provider = provider
        if valid_name(raw.get("persona")):
            out.persona = raw["persona"]
        extra_args = raw.get("extra_args")
        if isinstance(extra_args, list) and all(
            isinstance(arg, str) and "\0" not in arg for arg in extra_args
        ):
            out.extra_args = extra_args
        if valid_name(raw.get("agent")):
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
        if isinstance(raw.get("gateway_by_agent"), dict):
            out.gateway_by_agent = {
                k: v
                for k, v in raw["gateway_by_agent"].items()
                if k in GATEWAY_AGENTS and type(v) is bool
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

    @classmethod
    @contextlib.contextmanager
    def edit(cls, repo: Path) -> Generator[LastUsed]:
        directory = repo_state_dir(repo)
        with state_lock(directory / "last_used.lock"):
            last = cls._from_dict(read_state_for_update(directory / "last_used.json"))
            yield last
            last.save(repo)

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
    gateway: bool = False


def resolve_model(cfg: Config, last: LastUsed, agent: str) -> str:
    if agent in last.model_by_agent:
        return last.model_by_agent[agent]
    if agent == cfg.defaults.agent and cfg.defaults.model:
        return cfg.defaults.model
    return ""  # agent default; [models] entries are only autocomplete suggestions


def resolve_fast(cfg: Config, last: LastUsed, agent: str) -> bool:
    return agent in FAST_AGENTS and last.fast_by_agent.get(agent, cfg.defaults.fast)


def resolve_gateway(cfg: Config, last: LastUsed, agent: str) -> bool:
    """Gateway use for a launch form's ``agent``: remembered, else ``defaults.gateway``.

    Only agents the gateway serves, and only while a gateway is configured.
    """
    if agent not in GATEWAY_AGENTS or not cfg.network.agent_proxy:
        return False
    return last.gateway_by_agent.get(agent, cfg.defaults.gateway)


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
        gateway=resolve_gateway(cfg, last, agent),
    )


def remembered_profile(cfg: Config, last: LastUsed) -> str:
    """The profile the launch forms select first: the remembered one, or the first
    configured profile before any choice. "" means Custom, including when the
    remembered profile is no longer configured."""
    name = next(iter(cfg.profiles), "") if last.profile is None else last.profile
    return name if name in cfg.profiles else ""


def remembered_selection(cfg: Config, last: LastUsed) -> StepChoice:
    """The setup the launch forms select first: a remembered persona selection, a
    remembered profile or custom; before any choice the first configured profile,
    else persona implementer."""
    if last.selection == "persona":
        persona = cfg.personas.get(last.persona)
        if persona is not None and not persona.internal:
            return StepChoice("persona", persona=last.persona)
        return StepChoice("persona", persona=catalogue.IMPLEMENTER)
    if last.profile is None and not cfg.profiles:
        return StepChoice("persona", persona=catalogue.IMPLEMENTER)
    profile = remembered_profile(cfg, last)
    return StepChoice("profile", profile) if profile else CUSTOM


def remembered_provider(
    cfg: Config, last: LastUsed, available: Callable[[str], bool] | None = None
) -> str:
    """The provider persona selections start with: the remembered one, else
    ``catalogue.default_provider``, else the first available row."""
    ok = available or (lambda _row: True)
    if last.provider in catalogue.ROW_BY_ID and ok(last.provider):
        return last.provider
    if cfg.catalogue.default_provider:
        return cfg.catalogue.default_provider
    return next((row.id for row in catalogue.ROWS if ok(row.id)), catalogue.ROWS[0].id)


@dataclass(frozen=True)
class StepChoice:
    """How an action run or workflow step picks its agent settings."""

    kind: Literal["profile", "action", "custom", "persona"]
    profile: str = ""  # for kind "profile"
    persona: str = ""  # for kind "persona"

    def label(self) -> str:
        if self.kind == "profile":
            return self.profile
        if self.kind == "persona":
            return f"persona {self.persona}"
        return ACTION_CHOICE_LABELS[self.kind]


ACTION_CHOICE_LABELS = {"action": "action settings", "custom": "custom"}
ACTION_SETTINGS = StepChoice("action")
CUSTOM = StepChoice("custom")


def initial_choice(
    cfg: Config, last: LastUsed, name: str, action: Action
) -> tuple[StepChoice, str]:
    """The choice an action starts with, and a warning when its profile is missing.

    The action's persona comes first, then its profile, then its own agent settings,
    then a remembered persona selection, then the profile the launch forms would
    select (Custom when that is none)."""
    warning = ""
    if action.persona:
        return StepChoice("persona", persona=action.persona), ""
    if action.profile:
        if action.profile in cfg.profiles:
            return StepChoice("profile", action.profile), ""
        warning = f"actions.{name} refers to unknown profile {action.profile!r}"
    elif action.has_own_settings:
        return ACTION_SETTINGS, ""
    if last.selection == "persona":
        choice = remembered_selection(cfg, last)
        if choice.kind == "persona":
            return choice, warning
    profile = remembered_profile(cfg, last)
    return (StepChoice("profile", profile) if profile else CUSTOM), warning


@dataclass
class ActionSettings:
    """Launch settings resolved for one action run or workflow step."""

    agent: str
    model: str
    effort: str
    fast: bool
    timeout: int
    extra_args: list[str] = field(default_factory=list)
    profile: str = ""  # the named profile used, "" otherwise
    gateway: bool = False

    def label(self) -> str:
        parts = [
            self.agent,
            self.model,
            self.effort,
            "fast" if self.fast else "",
            "gateway" if self.gateway else "",
        ]
        return "/".join(part for part in parts if part)


@dataclass(frozen=True)
class Selection:
    """A launch setup choice with its per-job provider (persona kind only) and level."""

    choice: StepChoice
    provider: str = ""
    level: int | None = None  # None = the starting level


@dataclass
class ResolvedSetup(ActionSettings):
    """Fully resolved settings for one launch, action run or workflow step."""

    persona: str = ""
    provider: str = ""  # provider row id for persona selections
    level: int | None = None  # None = no security level: earlier network behaviour
    start_level: int | None = None  # what the level field starts at
    problem: str = ""  # why this cannot launch, e.g. a tier the provider lacks
    effort_note: str = ""  # e.g. that the provider default effort applies

    def label(self) -> str:
        text = super().label()
        if self.effort_note:
            text += f" ({self.effort_note})"
        if self.level is not None:
            text += f" · level {self.level}"
        return text


def project_level(cfg: Config) -> int | None:
    return cfg.security.level or None


def resolve_selection(
    cfg: Config, last: LastUsed, selection: Selection, action: Action | None = None
) -> ResolvedSetup:
    """Agent settings and level for ``selection``; one resolver for launch forms,
    action runs, workflow steps, dry runs and commit review.

    A provider that lacks the persona's tier gives a result with ``problem`` set:
    callers show it and refuse to launch, there is no fallback model.
    """
    action = action or Action()
    choice = selection.choice
    default = project_level(cfg)
    if choice.kind == "persona":
        return _resolve_persona(cfg, last, selection, action)
    settings = resolve_action_settings(cfg, last, action, choice)
    start = action.level or default
    return ResolvedSetup(
        **dataclasses.asdict(settings),
        level=selection.level if selection.level is not None else start,
        start_level=start,
    )


def _resolve_persona(
    cfg: Config, last: LastUsed, selection: Selection, action: Action
) -> ResolvedSetup:
    name = selection.choice.persona
    persona = cfg.personas.get(name)
    provider = selection.provider or remembered_provider(cfg, last)
    row = catalogue.ROW_BY_ID.get(provider)
    defaults = resolve_launch_defaults(cfg, last)
    timeout = action.timeout if action.timeout is not None else defaults.timeout
    start = action.level or (persona.level if persona else None) or project_level(cfg)
    level = selection.level if selection.level is not None else start
    if persona is None or row is None:
        what = f"persona {name!r}" if persona is None else f"provider {provider!r}"
        return ResolvedSetup(
            agent="",
            model="",
            effort="",
            fast=False,
            timeout=timeout,
            persona=name,
            provider=provider,
            level=level,
            start_level=start,
            problem=f"unknown {what}",
        )
    model = catalogue.model_for(cfg, row, persona.tier)
    problem = "" if model is not None else catalogue.tier_problem(cfg, row, persona.tier)
    if row.gateway and not cfg.network.agent_proxy and not problem:
        problem = f"{row.label}: no agent gateway is configured (network.agent_proxy)"
    return ResolvedSetup(
        agent=catalogue.row_agent(cfg, row),
        model=model or "",
        effort=persona.effort if row.effort else "",
        fast=persona.fast and row.fast,
        timeout=timeout,
        gateway=row.gateway,
        persona=name,
        provider=row.id,
        level=level,
        start_level=start,
        problem=problem,
        effort_note="" if row.effort else "provider default effort",
    )


def resolve_action_settings(
    cfg: Config, last: LastUsed, action: Action, choice: StepChoice
) -> ActionSettings:
    """Agent settings for ``action`` under ``choice``; the timeout always comes from
    the action, else the launch defaults. A choice naming a profile that is not
    configured resolves as Custom."""
    defaults = resolve_launch_defaults(cfg, last)
    timeout = action.timeout if action.timeout is not None else defaults.timeout
    if choice.kind == "profile" and (profile := cfg.profiles.get(choice.profile)):
        return ActionSettings(
            agent=profile.agent,
            model=profile.model if profile.agent != "bash" or profile.gateway else "",
            effort=profile.effort,
            fast=profile.fast and profile.agent in FAST_AGENTS,
            timeout=timeout,
            extra_args=list(profile.extra_args),
            profile=choice.profile,
            gateway=profile.gateway,  # kept as configured: launch checks reject misuse
        )
    if choice.kind == "action":
        agent = action.agent or defaults.agent
        return ActionSettings(
            agent=agent,
            model=action.model or (defaults.model if agent == defaults.agent else ""),
            effort=action.effort or defaults.effort,
            fast=resolve_fast(cfg, last, agent),
            timeout=timeout,
            gateway=resolve_gateway(cfg, last, agent),
        )
    return ActionSettings(
        agent=defaults.agent,
        model=defaults.model if defaults.agent != "bash" or defaults.gateway else "",
        effort=defaults.effort,
        fast=defaults.fast,
        timeout=timeout,
        extra_args=list(last.extra_args),
        gateway=defaults.gateway,
    )


def remember(
    repo: Path,
    agent: str,
    model: str,
    effort: str,
    timeout: int | None,
    *,
    fast: bool = False,
    gateway: bool = False,
    profile: str = "",
    extra_args: list[str] | None = None,
    provider: str = "",
    persona: str = "",
) -> None:
    """Remember an accepted launch. A persona launch (``persona`` set) remembers only
    the selection and timeout: custom agent settings and arguments stay as they are."""
    with contextlib.suppress(OSError), LastUsed.edit(repo) as last:
        if timeout is not None:
            last.timeout = timeout
        if persona:
            last.selection, last.provider, last.persona = "persona", provider, persona
            return
        last.selection = None
        last.profile = profile
        if not profile:
            last.extra_args = list(extra_args or [])
        last.agent = agent
        last.effort = effort
        last.model_by_agent[agent] = model
        if agent in FAST_AGENTS:
            last.fast_by_agent[agent] = fast
        if agent in GATEWAY_AGENTS:
            last.gateway_by_agent[agent] = gateway


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
# main_branch = "main"                 # baseline for diff, fast-forward, @ saves and cleanup
# deploy_config_skill = true           # copy the bundled configuration skills into workspaces

# [feedback]
# enabled = true                    # collect structured agent reports when supported
# require_reported_success = false  # pause the queue without a successful final report
# pass_state_keys = []              # selected predecessor state keys
# retry_outcomes = []               # failure / blocked; max_attempts includes first run
# max_attempts = 1
# on_success = ""                   # configured action name; also on_failure / on_blocked
# max_steps = 1                     # includes initial task; limit pauses the queue

# [defaults]
# agent = "claude"                     # claude | codex | opencode | pi | copilot | bash
# model = ""
# effort = "medium"                    # low | medium | high | xhigh | max
# fast = false                         # Codex / Claude; project-sandbox >= 0.3.1
# gateway = false                      # pi / opencode / bash via network.agent_proxy

# [profiles.review]                    # :config-profiles edits these in the TUI
# agent = "codex"                      # launch forms offer profiles and custom
# model = ""
# effort = "high"
# fast = false
# extra_args = ["--memory=16g"]          # additional project-sandbox switches

# [commands]                           # :name runs the shell command in the repository
# test = "uv run pytest"               # :test -k merge appends the arguments

# [actions.review]                     # :review starts this prompt as a background run on
# prompt = "Review {bookmark} against {main}; fix what you find."  # each selected bookmark
# effort = "high"                      # agent/model/effort/timeout: launch defaults; or a profile

# [leader]                             # ui.leader (\\) then these keys run a command line
# t = "test"                           # "*test" runs it in each selected workspace
# rv = "review"

# [[workspace_skills]]                 # top level: copied into bookmark workspaces on first use;
# source = "~/skills/release-review"   # the bookmark's .gitignore must ignore each target
# targets = [".agents/skills/release-review", ".claude/skills/release-review"]
"""

GLOBAL_TEMPLATE = """\
# project-sandbox-tui — global settings

[defaults]
agent = "claude"          # claude | codex | opencode | pi | copilot | bash
model = ""                # "" = the agent's own default
effort = ""               # "" or low | medium | high | xhigh | max
fast = false              # Codex / Claude; project-sandbox >= 0.3.1
gateway = false           # pi / opencode / bash via network.agent_proxy
timeout = 0               # seconds for unsupervised runs; 0 = none

[network]
# internet_proxy = "http://proxy.example:3128"   # -> --internet-proxy
# agent_proxy = "http://127.0.0.1:4000/v1"       # -> --agent-proxy for gateway = true launches
# ca_certs = ["/path/to/corp-root.pem"]          # -> --ca-cert (repeatable)
# extra_domains = []                              # -> --extra-domain (repeatable)
# allow_github = false

[sandbox]
command = "project-sandbox"   # or "uvx project-sandbox"
runtime = "auto"              # auto | apple-container | docker | podman | colima | chroot
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
# agent = "claude"            # agent / model / effort / timeout default to the launch defaults;
#                             # profile = "NAME" uses [profiles.NAME] instead of agent/model/effort

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
