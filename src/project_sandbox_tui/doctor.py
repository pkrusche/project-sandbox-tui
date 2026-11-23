"""Dependency checks and diagnostics for unavailable comparisons to main."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Literal

import tomlkit

from project_sandbox_tui import (
    capabilities,
    catalogue,
    initialization,
    launchers,
    notify,
    security,
)
from project_sandbox_tui import models as models_mod
from project_sandbox_tui.capabilities import Capabilities
from project_sandbox_tui.config import (
    FAST_AGENTS,
    GATEWAY_AGENTS,
    AgentProfile,
    Config,
    ConfigError,
    LastUsed,
    ProjectTrust,
    Selection,
    WorkflowTrust,
    _load,
    initial_choice,
    load,
    resolve_selection,
)
from project_sandbox_tui.sandbox import runtime_cli
from project_sandbox_tui.vcs import Branch, VcsError, detect, main_branch

# The jj release CI tests against; older ones may lack template keywords or flags.
TESTED_JJ = (0, 46, 0)
MIN_PROJECT_SANDBOX = (0, 4, 1)

Status = Literal["ok", "warn", "fail"]
MARKS: dict[Status, str] = {"ok": "✓", "warn": "!", "fail": "✗"}


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    hint: str = ""

    def render(self) -> str:
        line = f"{MARKS[self.status]} {self.name}: {self.detail}"
        return f"{line}\n    → {self.hint}" if self.hint else line


# Injected by tests: run argv, return (exit code, combined output); OSError if missing.
Runner = capabilities.Runner
run = capabilities.run


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def _v(version: tuple[int, int, int]) -> str:
    return ".".join(map(str, version))


def _last_line(output: str) -> str:
    lines = output.splitlines()
    return lines[-1] if lines else "no output"


def check_jj(runner: Runner) -> Check:
    try:
        code, out = runner(["jj", "--version"], 30)
    except OSError:
        return Check("jj", "fail", "not found on PATH", "install jj: https://jj-vcs.github.io/jj/")
    version = parse_version(out) if code == 0 else None
    if version is None:
        return Check("jj", "fail", f"`jj --version` failed: {_last_line(out)}")
    if version < TESTED_JJ:
        return Check(
            "jj",
            "warn",
            f"{_v(version)} is older than {_v(TESTED_JJ)}, the version sbx-tui is tested with",
            "upgrade jj if listing, diffs, merges or cleanup fail",
        )
    return Check("jj", "ok", _v(version))


def check_repo(path: Path) -> Check:
    try:
        backend = detect(path)
    except VcsError as exc:
        return Check("repository", "warn", str(exc), "run sbx-tui inside a jj repository")
    return Check("repository", "ok", str(backend.root))


def check_comparisons(branches: list[Branch], configured: str = "") -> list[Check]:
    """Explain the supplied listing without retrying away transient jj failures."""
    bookmarks = {b.name: b for b in branches if not b.working_copy}
    main = main_branch(bookmarks, configured)
    baseline = bookmarks.get(main) if main else None
    baseline_detail = (
        f"{configured!r} (configured)" if configured else "auto: main / master / trunk"
    )
    if baseline:
        baseline_detail = f"{main} ({baseline.commit or 'conflicted; multiple targets'})"
    checks = [
        Check(
            "comparison baseline",
            "ok" if baseline and baseline.commit else "warn",
            baseline_detail,
        )
    ]
    unavailable = [b for b in branches if b.comparison is None]
    checks.append(
        Check(
            "comparisons to main",
            "warn" if unavailable or not branches else "ok",
            f"{len(branches) - len(unavailable)}/{len(branches)} revisions available; "
            f"{len(unavailable)} show ?",
            "R or :refresh retries comparisons; :reload reloads project.main_branch"
            if unavailable
            else "",
        )
    )
    for branch in unavailable:
        if main is None:
            hint = "set project.main_branch to an existing local bookmark"
        elif not baseline or not baseline.commit or not branch.commit:
            hint = "resolve the conflicted bookmark with jj bookmark set --to REV NAME"
        elif branch.comparison_error:
            hint = "check the jj error above; sbx-tui retries on the next refresh"
        else:
            hint = "R or :refresh calculates comparisons"
        checks.append(
            Check(
                f"? {branch.label}" + (f" ({branch.commit})" if branch.commit else ""),
                "warn",
                branch.comparison_error or "comparison was not requested for this listing",
                hint,
            )
        )
    return checks


def check_project_sandbox(cfg: Config, runner: Runner) -> Check:
    try:
        cmd = cfg.sandbox_command()
    except ConfigError as exc:
        return Check("project-sandbox", "fail", str(exc))
    shown = " ".join(cmd)
    try:
        code, out = runner([*cmd, "--version"], 180)  # uvx may download it first
    except OSError:
        return Check(
            "project-sandbox",
            "fail",
            f"{cmd[0]} not found on PATH",
            "uv tool install project-sandbox, or set sandbox.command",
        )
    version = parse_version(out) if code == 0 else None
    if version is None:
        return Check("project-sandbox", "fail", f"`{shown} --version` failed: {_last_line(out)}")
    if version < MIN_PROJECT_SANDBOX:
        return Check(
            "project-sandbox",
            "fail",
            f"{_v(version)} is too old; sbx-tui needs {_v(MIN_PROJECT_SANDBOX)} or newer",
            "uv tool upgrade project-sandbox",
        )
    return Check("project-sandbox", "ok", f"{_v(version)} ({shown})")


def check_runtime(cfg: Config, runner: Runner, which: Callable[[str], str | None]) -> Check:
    """The runtime project-sandbox would pick, and whether it answers."""
    requested = cfg.sandbox.runtime
    cli = runtime_cli(requested, which)
    if cli is None and requested != "auto":
        return Check(
            "container runtime",
            "warn",
            f"could not verify {requested!r} without launcher diagnostics",
        )
    if cli is None or which(cli) is None:
        wanted = cli or "docker, podman or Apple container"
        return Check(
            "container runtime",
            "fail",
            f"{wanted} not found on PATH (sandbox.runtime = {requested!r})",
            "install Docker, Podman or (macOS) Apple container",
        )
    probe = [cli, "system", "status"] if cli == "container" else [cli, "info"]
    try:
        code, out = runner(probe, 60)
    except OSError:
        return Check("container runtime", "fail", f"{cli} could not be run")
    if code != 0:
        hint = "container system start" if cli == "container" else f"start the {cli} daemon"
        return Check(
            "container runtime", "fail", f"`{' '.join(probe)}` failed: {_last_line(out)}", hint
        )
    return Check("container runtime", "ok", f"{cli} is running")


def _check_workflow_files(cfg: Config, repo: Path) -> list[Check]:
    """Trust, validity, provenance and persona dependencies of each workflow file."""
    try:
        files, scan_warnings = WorkflowTrust.scan(repo)
    except (ConfigError, OSError) as exc:
        return [Check("workflow file", "fail", f"cannot scan workflow files: {exc}")]
    checks = [Check("workflow file", "warn", warning) for warning in scan_warnings]
    problems: dict[Path, list[str]] = {}
    for path, problem in cfg.workflow_problems:
        problems.setdefault(path, []).append(problem)
    for file in files:
        if file.needs_review:
            checks.append(Check("workflow file", "warn", file.describe(), "sbx-tui --trust-config"))
        elif file.path in problems:
            pass  # reported below instead of "trusted"
        elif file.current is not None and file.trusted is not None:
            checks.append(Check("workflow file", "ok", f"{file.path} is trusted"))
        checks.extend(_check_workflow_source(file.path, file.current))
    for path, messages in problems.items():
        checks.extend(
            Check("workflow file", "warn", f"{path}: {message}")
            for message in dict.fromkeys(messages)
        )
    dependencies = dict.fromkeys(
        (name.split("/", 1)[0], action.persona)
        for name, action in cfg.actions.items()
        if "/" in name and action.persona and action.persona not in catalogue.BUILTIN_PERSONAS
    )
    checks.extend(
        Check(
            "workflow file",
            "warn",
            f"workflow {workflow} depends on locally configured persona {persona}",
        )
        for workflow, persona in dependencies
    )
    return checks


def _check_workflow_source(path: Path, text: str | None) -> list[Check]:
    """Whether an imported workflow file still matches the recorded ``[source]`` hash."""
    if text is None:
        return []
    try:
        document = tomlkit.parse(text)
    except ValueError:
        return []
    source = document.pop("source", None)
    if not isinstance(source, dict) or not isinstance(source.get("sha256"), str):
        return []
    body = tomlkit.dumps(document)
    if not body.endswith("\n"):
        body += "\n"
    matches = source["sha256"] == hashlib.sha256(body.encode()).hexdigest()
    detail = "matches source" if matches else "edited since import"
    return [Check("workflow source", "ok" if matches else "warn", f"{path}: {detail}")]


def check_config(cfg: Config, repo: Path | None) -> list[Check]:
    checks = [Check("project config", "warn", warning) for warning in cfg.warnings]
    if repo is not None:
        try:
            trust = ProjectTrust.check(repo)
        except ConfigError as exc:
            checks.append(Check("project config", "fail", str(exc)))
        else:
            if trust.needs_review:
                checks.append(
                    Check("project config", "warn", trust.describe(), "sbx-tui --trust-config")
                )
        checks.extend(_check_workflow_files(cfg, repo))
        if not cfg.has_image_source():
            checks.append(
                Check(
                    "image",
                    "warn",
                    "no project.base_image, project.dockerfile or project.build",
                    "sbx-tui --init, or sbx-tui config set --project project.build python-uv",
                )
            )
    mode = cfg.ui.terminal_mode()
    if mode in launchers.MULTIPLEXERS and shutil.which(mode) is None:
        checks.append(
            Check("terminal", "warn", f"ui.terminal = {mode!r} but {mode} is not on PATH")
        )
    if notify.notification_argv("", "", cfg.ui.notify_command) is None:
        checks.append(
            Check("notifications", "warn", "no notifier found", "install notify-send (libnotify)")
        )
    unknown = [
        (name, action.profile)
        for name, action in cfg.actions.items()
        if action.profile and action.profile not in cfg.profiles
    ]
    if unknown:
        checks.append(
            Check(
                "action profiles",
                "warn",
                "unknown profile: "
                + ", ".join(f"actions.{name} -> {profile!r}" for name, profile in unknown),
                "define [profiles.NAME] or change actions.NAME.profile; "
                "these actions use the remembered launch profile",
            )
        )
    return checks


def check_feedback(
    cfg: Config, repo: Path | None, runner: Runner, caps: Capabilities | None = None
) -> list[Check]:
    if not cfg.feedback.enabled:
        return [Check("run feedback", "ok", "disabled")]
    required = cfg.feedback.requires_reporting or bool(cfg.workflows)
    if caps is not None and caps.help_ok:
        supported = caps.mount  # the discovery probe already read --help
    else:
        try:
            code, help_text = runner([*cfg.sandbox_command(), "--help"], 10)
            supported = code == 0 and re.search(r"(?<!\S)--mount(?:\s|=)", help_text) is not None
        except OSError:
            supported = False
    checks = [
        Check(
            "run feedback",
            "ok" if supported else ("fail" if required else "warn"),
            "bind-mount transport available" if supported else "--mount capability unavailable",
            "" if supported else "install project-sandbox with --mount support",
        )
    ]
    if repo is not None:
        try:
            guidance = (repo / "AGENTS.md").read_text()
            complete = initialization.GUIDANCE in guidance
            template = (
                files("project_sandbox_tui")
                .joinpath("skills", initialization.FEEDBACK_SKILL, "SKILL.md")
                .read_text()
            )
            skills = all(
                (repo / path / "SKILL.md").read_text() == template
                for path in initialization.FEEDBACK_DIRS
            )
        except (OSError, UnicodeError):
            complete = skills = False
        checks.append(
            Check(
                "feedback guidance",
                "ok" if complete and skills else "warn",
                "skill and AGENTS.md guidance installed"
                if complete and skills
                else "missing or customized skill / AGENTS.md guidance",
                "sbx-tui --init",
            )
        )
    return checks


# ---------------------------------------------------------------- capabilities


def check_capabilities(cfg: Config, caps: Capabilities, home: Path | None = None) -> list[Check]:
    """What the launcher supports and which agents this host can run."""
    who = capabilities.launcher_name(caps)
    checks: list[Check] = []
    if not caps.discovered:
        checks.append(
            Check(
                "launcher capabilities",
                "warn",
                "could not be discovered; using built-in lists"
                + (f" ({'; '.join(caps.problems)})" if caps.problems else ""),
                "check sandbox.command; agent checks below use the built-in lists",
            )
        )
    else:
        checks.append(Check("launcher capabilities", "ok", f"from {who}"))
        for problem in caps.problems:
            checks.append(Check("launcher capabilities", "warn", problem))
    gateway = bool(cfg.network.agent_proxy)
    for agent in caps.agents:
        result = capabilities.agent_sources(
            agent, caps, gateway_configured=gateway, home=home, cfg=cfg
        )
        checks.append(
            Check(
                f"agent {agent}",
                "warn" if result.available is False else "ok",
                result.describe()
                if result.available is not False
                else f"unavailable: {result.reason}",
            )
        )
    checks.append(
        Check(
            "vcs access",
            "ok",
            ", ".join(caps.vcs_modes) if caps.vcs_modes else f"{who} has no --vcs-access option",
        )
    )
    shown = []
    for name, runtime in caps.runtimes.items():
        if name == "auto":
            continue
        mark = {"usable": "usable", "unavailable": "unavailable", "unknown": "status unknown"}
        detail = f" ({runtime.detail})" if runtime.detail else ""
        shown.append(f"{name} {mark[runtime.status]}{detail}")
    if caps.auto_selects:
        shown.append(f"auto selects {caps.auto_selects}")
    usable = any(rt.status == "usable" for rt in caps.runtimes.values())
    checks.append(
        Check(
            "runtimes",
            "ok" if usable or caps.runtime_source != "doctor" else "warn",
            "; ".join(shown) or "none reported",
            ""
            if caps.runtime_source == "doctor"
            else "status unknown: `doctor --json` unavailable",
        )
    )
    return checks


# ---------------------------------------------------------------- security levels


def egress_policy_path() -> Path:
    """The egress sidecar's policy: global per container runtime, managed outside sbx-tui."""
    configured = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(configured) if configured.startswith("/") else Path.home() / ".config"
    return base / "project-sandbox" / "egress.toml"


def egress_hosts(path: Path) -> list[str] | None:
    """Best-effort list of hosts in the sidecar policy; None when it cannot be read.

    The file's structure is project-sandbox's: every string in a list under a key
    that names hosts or domains (``allow``, ``hosts``, ``domains``…) counts. The
    file is only read, never changed.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    found: list[str] = []

    def walk(value: object, hosty: bool) -> None:
        if isinstance(value, dict):
            for key, inner in value.items():
                walk(inner, hosty or bool(re.search(r"allow|host|domain", str(key), re.I)))
        elif isinstance(value, list):
            for item in value:
                if hosty and isinstance(item, str):
                    found.append(item)
                else:
                    walk(item, hosty)

    walk(data, False)
    return list(dict.fromkeys(found))


def check_security(cfg: Config, caps: Capabilities, home: Path | None = None) -> list[Check]:
    """Level conflicts, the egress backend and provider availability."""
    checks: list[Check] = []
    if cfg.security.level:
        sources = [("sandbox.extra_args", cfg.sandbox.extra_args)]
        sources += [(f"profiles.{n}.extra_args", p.extra_args) for n, p in cfg.profiles.items()]
        for key, args in sources:
            if flags := security.managed_conflicts(args):
                message = security.conflict_message(key, flags)
                detail, _, hint = message.partition("; ")
                checks.append(Check(key, "fail", detail, hint))
        if cfg.network.no_firewall:
            checks.append(
                Check(
                    "network.no_firewall",
                    "warn",
                    "ignored while security levels are in effect",
                    "use level 1 for the jobs that need no firewall",
                )
            )
    checks += _check_egress(cfg, caps)
    checks += _check_providers(cfg, caps, home)
    return checks


def _check_egress(cfg: Config, caps: Capabilities) -> list[Check]:
    backend = cfg.security.egress
    name = "egress backend"
    who = capabilities.launcher_name(caps)
    hint = (
        "set up the sidecar with project-sandbox-egress, or set "
        'security.egress = "firewall" in the global config'
    )
    if caps.discovered and backend not in caps.egress_modes:
        supported = ", ".join(caps.egress_modes) or "no --egress option"
        return [
            Check(
                name,
                "fail",
                f"{backend} (security.egress) is not supported by {who}: {supported}; "
                "levels 3 to 5 and commit review need it",
                hint if caps.egress_modes else "upgrade project-sandbox",
            )
        ]
    if backend != "sidecar":
        return [Check(name, "ok", f"{backend}: agent API hosts only, pinned in the firewall")]
    path = egress_policy_path()
    if not path.is_file():
        return [
            Check(
                name,
                "ok",
                f"sidecar uses default settings because {path} does not exist",
                "GitHub Copilot needs the sidecar to allow access to its hosts; "
                "configure the policy with project-sandbox-egress if needed",
            )
        ]
    hosts = egress_hosts(path)
    listed = (
        f"allows {len(hosts)} host{'s' * (len(hosts) != 1)}: {', '.join(hosts)}"
        if hosts
        else f"policy at {path}"
    )
    return [
        Check(
            name,
            "ok",
            f"sidecar · {listed}",
            f"{path} is shared by all projects on the runtime; sbx-tui never changes it",
        )
    ]


def _check_providers(cfg: Config, caps: Capabilities, home: Path | None) -> list[Check]:
    checks: list[Check] = []
    reasons: dict[str, str] = {}
    shown = []
    for row in catalogue.ROWS:
        result = capabilities.provider_availability(cfg, caps, row, home=home)
        if result.available is False:
            reasons[row.id] = result.reason
            shown.append(f"{row.label} unavailable: {result.reason}")
        elif result.available is None:
            shown.append(f"{row.label} unknown")
        else:
            shown.append(row.label)
    checks.append(Check("providers", "ok", "; ".join(shown)))
    for key, value in (
        ("catalogue.default_provider", cfg.catalogue.default_provider),
        ("commit_review.provider", cfg.commit_review.provider),
    ):
        if value in reasons:
            label = catalogue.ROW_BY_ID[value].label
            checks.append(Check(key, "fail", f"{label} is unavailable: {reasons[value]}"))
    return checks


def _selections(
    cfg: Config, caps: Capabilities, home: Path | None, last: LastUsed
) -> tuple[list[tuple[str, AgentProfile]], list[Check]]:
    """Every configured agent selection, keyed by its config table."""
    d = cfg.defaults
    out: list[tuple[str, AgentProfile]] = [
        (
            "defaults",
            AgentProfile(
                agent=d.agent,
                model=d.model,
                effort=d.effort,
                fast=d.fast,
                gateway=d.gateway and d.agent in GATEWAY_AGENTS,
            ),
        )
    ]
    out += [(f"profiles.{name}", profile) for name, profile in cfg.profiles.items()]
    problems: list[Check] = []
    for name, action in cfg.actions.items():
        if not (action.persona or action.has_own_settings):
            continue
        choice, _ = initial_choice(cfg, last, name, action)
        provider = ""
        if action.persona:
            provider = capabilities.default_provider(cfg, last, caps, home=home)
        setup = resolve_selection(cfg, last, Selection(choice, provider), action)
        key = f"actions.{name}"
        if setup.problem:
            problems.append(Check(f"{key}.persona", "fail", setup.problem))
        else:
            out.append(
                (
                    key,
                    AgentProfile(
                        setup.agent, setup.model, setup.effort, setup.fast, gateway=setup.gateway
                    ),
                )
            )
    if not cfg.commit_review.profile:
        try:
            name, profile = cfg.commit_summary_profile()
        except ConfigError:
            return out, problems  # check_security reports the unavailable provider
        out.append((f"commit review ({name})", profile))
    return out, problems


def _model_listed(
    cfg: Config, agent: str, model: str, store: models_mod.Store | None
) -> bool | None:
    """Whether a Claude/Codex model appears in the suggestions; None = no list to check."""
    if agent not in ("claude", "codex") or not model:
        return None
    if model in catalogue.models_for_agent(cfg, agent):
        return True
    if store is None and (agent == "claude" or not models_mod.codex_cache_models()):
        return None  # only aliases or nothing: an unknown full ID proves nothing
    names = models_mod.suggestions(
        agent, cfg.models.get(agent), store, catalogue.models_for_agent(cfg, agent)
    )
    return model in names if names else None


def check_agent_selections(
    cfg: Config,
    caps: Capabilities,
    store: models_mod.Store | None = None,
    home: Path | None = None,
    last: LastUsed | None = None,
) -> list[Check]:
    """Configured selections the launcher would reject (fail) or that look wrong (warn)."""
    selections, checks = _selections(cfg, caps, home, last or LastUsed())
    who = capabilities.launcher_name(caps)
    for key, profile in selections:
        agent = profile.agent
        extra = [*cfg.sandbox.extra_args, *profile.extra_args]
        if cfg.sandbox.no_forward_credentials:
            extra.append("--no-forward-credentials")
        if caps.discovered and agent not in caps.agents:
            checks.append(
                Check(
                    f"{key}.agent",
                    "fail",
                    f"{agent!r} is not supported by {who}",
                    f"choose one of {', '.join(caps.agents)}",
                )
            )
        else:
            result = capabilities.launch_availability(
                agent, caps, gateway=profile.gateway, extra_args=extra, home=home
            )
            if result.available is False:
                hint = (
                    "set gateway = true to use the agent gateway"
                    if agent in GATEWAY_AGENTS and cfg.network.agent_proxy
                    else "log in on the host, or add --no-forward-credentials to log in "
                    "inside the sandbox"
                )
                checks.append(
                    Check(f"{key}.agent", "fail", f"{agent} unavailable: {result.reason}", hint)
                )
        for problem in capabilities.gateway_problems(agent, profile.model, profile.gateway):
            field = "gateway" if agent not in GATEWAY_AGENTS else "model"
            checks.append(Check(f"{key}.{field}", "fail", problem))
        problems = capabilities.launch_problems(
            caps, agent=agent, extra_args=extra, runtime=cfg.sandbox.runtime
        )
        for problem in problems:
            if problem.startswith("agent "):
                continue  # reported above
            if (
                problem.startswith("runtime ")
                and capabilities.flag_value(extra, "--runtime") is None
            ):
                continue  # sandbox.runtime is checked once, below
            checks.append(Check(f"{key}.extra_args", "fail", problem))
        runtime = capabilities.flag_value(profile.extra_args, "--runtime")
        if runtime and (status := caps.runtimes.get(runtime)) and status.status == "unavailable":
            checks.append(
                Check(
                    f"{key}.extra_args", "fail", f"runtime {runtime}: {status.detail}", status.hint
                )
            )
        if profile.fast and agent not in FAST_AGENTS:
            checks.append(
                Check(f"{key}.fast", "warn", f"fast mode is only for {', '.join(FAST_AGENTS)}")
            )
        if _model_listed(cfg, agent, profile.model, store) is False:
            checks.append(
                Check(
                    f"{key}.model",
                    "warn",
                    f"{profile.model!r} is not in the {agent} model list",
                    "run :update-models, or add it under [models] if it is correct",
                )
            )
    runtime = cfg.sandbox.runtime
    status = caps.runtimes.get(runtime)
    if caps.runtime_source != "fallback" and caps.discovered and status is None:
        checks.append(
            Check(
                "sandbox.runtime",
                "fail",
                f"{runtime!r} is not supported by {who}",
                f"choose one of {', '.join(caps.runtimes)}",
            )
        )
    elif status is not None and status.status == "unavailable":
        checks.append(Check("sandbox.runtime", "fail", f"{runtime}: {status.detail}", status.hint))
    providers = (cfg.catalogue.default_provider, cfg.commit_review.provider)
    if cfg.network.agent_proxy and not (
        any(p.gateway for _key, p in selections)
        or any(row.startswith("gateway-") for row in providers)
    ):
        checks.append(
            Check(
                "agent gateway",
                "ok",
                "network.agent_proxy is configured but no configured selection uses it",
                "set gateway = true on a pi, opencode or bash profile, or defaults.gateway",
            )
        )
    if not checks:
        checks.append(Check("agent selections", "ok", f"{len(selections)} checked"))
    return checks


def run_checks(
    path: Path,
    global_path: Path | None = None,
    runner: Runner | None = None,
    which: Callable[[str], str | None] | None = None,
) -> list[Check]:
    runner = runner or run
    which = which or shutil.which
    checks = [check_jj(runner)]
    repo_check = check_repo(path)
    checks.append(repo_check)
    repo = Path(repo_check.detail) if repo_check.status == "ok" else None
    try:
        cfg = load(repo, global_path) if repo is not None else _load(global_path, None, None)
    except ConfigError as exc:
        checks.append(Check("config", "fail", str(exc), "sbx-tui config list"))
        cfg = Config()
    checks.append(check_project_sandbox(cfg, runner))
    try:
        command = cfg.sandbox_command()
    except ConfigError:
        caps = capabilities.fallback()
    else:  # doctor always probes fresh, and saves the result for the TUI
        caps = capabilities.refresh(command, repo, runner, force=True)
    if caps.runtime_source != "doctor":
        checks.append(check_runtime(cfg, runner, which))
    checks += check_capabilities(cfg, caps)
    checks += check_security(cfg, caps)
    checks += check_agent_selections(
        cfg, caps, models_mod.load_store(), last=LastUsed.load(repo) if repo else None
    )
    checks += check_config(cfg, repo)
    checks += check_feedback(cfg, repo, runner, caps)
    if repo is not None:
        try:
            branches = detect(repo).list_branches(compare_to_main=cfg.project.main_branch)
        except VcsError as exc:
            checks.append(Check("comparisons to main", "fail", exc.detail))
        else:
            checks += check_comparisons(branches, cfg.project.main_branch)
    return checks


def report(path: Path, global_path: Path | None = None) -> tuple[str, int]:
    """Return the diagnostic text and CLI exit status."""
    checks = run_checks(path, global_path)
    failed = sum(c.status == "fail" for c in checks)
    summary = f"{failed} problem{'s' if failed != 1 else ''} found" if failed else "all good"
    body = "\n".join(check.render() for check in checks)
    return f"{body}\n\n{summary}", int(bool(failed))


def main(path: Path, global_path: Path | None = None) -> int:
    output, status = report(path, global_path)
    print(output)
    sys.stdout.flush()
    return status
