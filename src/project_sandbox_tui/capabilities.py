"""What the configured project-sandbox launcher supports, and what this host can run.

Sources, all from the launcher that ``sandbox.command`` runs (never the imported
package, which may be a different installation):

- ``--help``: the ``{…}`` choices of ``--agent``, ``--vcs-access``, ``--egress`` and
  ``--runtime``,
  and whether ``--mount`` exists (run feedback);
- ``doctor --json``: the ``runtimes`` section, with each runtime's status;
- ``--version``: the cache key, and the gate for the host-availability rules
  imported from the ``project_sandbox`` package.

Probes never block the UI: snapshots are cached per repository and refreshed in
the background or by ``doctor``. When a probe fails the built-in lists apply, and
such a fallback snapshot never restricts a choice or blocks a launch.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

from project_sandbox_tui import catalogue
from project_sandbox_tui.config import (
    AGENTS,
    GATEWAY_AGENTS,
    RUNTIMES,
    Config,
    repo_state_dir,
    state_home,
)

CACHE_NAME = "capabilities.json"
CACHE_VERSION = 2  # 2: egress modes
MAX_CACHE_BYTES = 256 * 1024
MAX_AGE = timedelta(hours=24)

VERSION_TIMEOUT = 180.0  # uvx may download the launcher first
HELP_TIMEOUT = 30.0
DOCTOR_TIMEOUT = 60.0

NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_MOUNT = re.compile(r"(?<!\S)--mount(?:\s|=)")
_AUTO = re.compile(r"\bselects\s+([a-z][a-z0-9-]{0,31})\b")

Source = Literal["probe", "fallback"]
RuntimeState = Literal["usable", "unavailable", "unknown"]

# Availability sources, as shown to users.
HOST_CREDENTIALS = "host credentials"
AGENT_GATEWAY = "agent gateway"
TOKEN_LOGIN = "token login"
SANDBOX_LOGIN = "login in sandbox"
NO_CREDENTIALS = "no credentials needed"

# Run argv, return (exit code, combined output); raise OSError if missing.
Runner = Callable[[list[str], float], tuple[int, str]]


def run(argv: list[str], timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


@dataclass(frozen=True)
class RuntimeStatus:
    status: RuntimeState
    detail: str = ""
    hint: str = ""


@dataclass(frozen=True)
class Capabilities:
    command: tuple[str, ...] = ()
    version: str = ""  # launcher version, "" = unknown
    discovered_at: datetime | None = None
    source: Source = "fallback"  # "probe" only when --help listed the agents
    agents: tuple[str, ...] = AGENTS
    vcs_modes: tuple[str, ...] = ()
    egress_modes: tuple[str, ...] = ()
    runtimes: dict[str, RuntimeStatus] = field(
        default_factory=lambda: {name: RuntimeStatus("unknown") for name in RUNTIMES}
    )
    runtime_source: Literal["doctor", "help", "fallback"] = "fallback"
    auto_selects: str = ""
    help_ok: bool = False  # --help ran; `mount` is then meaningful
    mount: bool = False
    problems: tuple[str, ...] = ()  # why parts of the snapshot are fallbacks

    @property
    def discovered(self) -> bool:
        return self.source == "probe"


def fallback(command: Sequence[str] = (), problem: str = "") -> Capabilities:
    return Capabilities(command=tuple(command), problems=(problem,) if problem else ())


# ---------------------------------------------------------------- parsing


def parse_version(text: str) -> str:
    match = _VERSION.search(text)
    return ".".join(match.groups()) if match else ""


def _version_tuple(version: str) -> tuple[int, ...] | None:
    match = _VERSION.fullmatch(version.strip())
    return tuple(int(part) for part in match.groups()) if match else None


def parse_choices(help_text: str, option: str) -> tuple[str, ...] | None:
    """Choices of ``option`` from argparse help (``--opt {a,b}``); None if absent or malformed."""
    match = re.search(rf"(?<![\w-]){re.escape(option)}[ =]\{{([^{{}}]*)\}}", help_text)
    if match is None:
        return None
    choices = tuple(part.strip() for part in match[1].split(","))
    if not choices or not all(NAME.fullmatch(choice) for choice in choices):
        return None
    return tuple(dict.fromkeys(choices))


def has_option(help_text: str, option: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(option)}(?![\w-])", help_text) is not None


def parse_doctor(text: str) -> tuple[dict[str, RuntimeStatus], str] | None:
    """Runtimes and the ``auto`` selection from ``doctor --json``; None if unusable."""
    start = text.find("{")
    if start < 0:
        return None
    try:
        data, _end = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    checks = data.get("checks") if isinstance(data, dict) else None
    if not isinstance(checks, list):
        return None
    runtimes: dict[str, RuntimeStatus] = {}
    auto = ""
    for check in checks:
        if not isinstance(check, dict) or check.get("section") != "runtimes":
            continue
        name, status = check.get("name"), check.get("status")
        if not isinstance(name, str) or not NAME.fullmatch(name):
            continue
        detail = _text(check.get("detail"))
        state: RuntimeState = (
            "usable"
            if status in ("ok", "warn")
            else "unavailable"
            if status in ("fail", "info")
            else "unknown"
        )
        runtimes[name] = RuntimeStatus(state, detail, _text(check.get("hint")))
        if name == "auto" and (match := _AUTO.search(detail)):
            auto = match[1]
    return (runtimes, auto) if runtimes else None


def _text(value: Any, limit: int = 300) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()[:limit]


def discover(command: Sequence[str], runner: Runner | None = None) -> Capabilities:
    """Probe the launcher. Never raises: failed probes become fallback parts."""
    runner = runner or run
    argv = list(command)
    problems: list[str] = []

    def probe(args: list[str], timeout: float) -> tuple[int, str] | None:
        try:
            return runner([*argv, *args], timeout)
        except OSError as exc:
            problems.append(f"`{' '.join([*argv, *args])}` could not run: {exc.strerror or exc}")
            return None

    result = probe(["--version"], VERSION_TIMEOUT)
    version = parse_version(result[1]) if result and result[0] == 0 else ""
    if result is None:  # the launcher is missing: the other probes would fail the same way
        return fallback(command, problems[0])
    result = probe(["--help"], HELP_TIMEOUT)
    help_ok = bool(result and result[0] == 0)
    help_text = result[1] if result and help_ok else ""
    if result is not None and not help_ok:
        problems.append(f"`{' '.join([*argv, '--help'])}` failed: {_last_line(result[1])}")
    agents = parse_choices(help_text, "--agent")
    if help_ok and agents is None:
        problems.append("`--help` lists no --agent choices")
    vcs_modes = parse_choices(help_text, "--vcs-access") or ()
    egress_modes = parse_choices(help_text, "--egress") or ()
    runtimes: dict[str, RuntimeStatus] = {}
    auto = ""
    runtime_source: Literal["doctor", "help", "fallback"] = "fallback"
    # Only a launcher whose --help looks like project-sandbox's gets `doctor`:
    # anything else may treat it as a project path (the test fake does).
    result = probe(["doctor", "--json"], DOCTOR_TIMEOUT) if agents else None
    parsed = parse_doctor(result[1]) if result else None
    if parsed is not None:
        runtimes, auto = parsed
        runtime_source = "doctor"
    else:
        if agents:
            reason = _last_line(result[1]) if result else "not run"
            problems.append(f"`doctor --json` gave no runtime status: {reason}")
        choices = parse_choices(help_text, "--runtime")
        if choices:
            runtimes = {name: RuntimeStatus("unknown") for name in choices}
            runtime_source = "help"
        else:
            runtimes = {name: RuntimeStatus("unknown") for name in RUNTIMES}
    return Capabilities(
        command=tuple(command),
        version=version,
        discovered_at=datetime.now(UTC),
        source="probe" if agents else "fallback",
        agents=agents or AGENTS,
        vcs_modes=vcs_modes if agents else (),
        egress_modes=egress_modes if agents else (),
        runtimes=runtimes,
        runtime_source=runtime_source,
        auto_selects=auto,
        help_ok=help_ok,
        mount=help_ok and _MOUNT.search(help_text) is not None,
        problems=tuple(problems),
    )


def _last_line(output: str) -> str:
    lines = output.strip().splitlines()
    return lines[-1][:200] if lines else "no output"


# ---------------------------------------------------------------- host availability


@dataclass(frozen=True)
class Availability:
    available: bool | None  # None = cannot tell; never restricts a choice
    sources: tuple[str, ...] = ()
    reason: str = ""

    def describe(self) -> str:
        if self.available is None:
            return self.reason or "availability unknown"
        return ", ".join(self.sources) if self.available else self.reason


def installed_version() -> str:
    try:
        return metadata.version("project-sandbox")
    except metadata.PackageNotFoundError:
        return ""


def _host_rules(caps: Capabilities, home: Path | None) -> tuple[set[str], dict[str, Path]] | None:
    """Agents with forwarded host configuration, using project-sandbox's own rules.

    Only when the importable package matches the launcher's version; otherwise None.
    """
    launcher, installed = _version_tuple(caps.version), _version_tuple(installed_version())
    if launcher is None or launcher != installed:
        return None
    try:
        from project_sandbox import config_agents

        present = set(config_agents.available_agents(home))
        paths = dict(config_agents._agent_host_paths(home or Path.home()))
    except Exception:  # private API of a dependency: any failure means "unknown"
        return None
    return present, paths


def agent_sources(
    agent: str,
    caps: Capabilities,
    *,
    gateway_configured: bool = False,
    home: Path | None = None,
    cfg: Config | None = None,
) -> Availability:
    """How ``agent`` can run on this host, in general (not for one launch)."""
    login = cfg is not None and (
        cfg.sandbox.no_forward_credentials or "--no-forward-credentials" in cfg.sandbox.extra_args
    )
    return _availability(agent, caps, gateway=gateway_configured, sandbox_login=login, home=home)


def launch_availability(
    agent: str,
    caps: Capabilities,
    *,
    gateway: bool = False,
    extra_args: Iterable[str] = (),
    home: Path | None = None,
) -> Availability:
    """Whether one launch of ``agent`` can start, given its gateway use and switches."""
    return _availability(
        agent,
        caps,
        gateway=gateway,
        sandbox_login="--no-forward-credentials" in extra_args,
        home=home,
    )


def _availability(
    agent: str,
    caps: Capabilities,
    *,
    gateway: bool,
    sandbox_login: bool,
    home: Path | None,
) -> Availability:
    if agent == "bash":
        return Availability(True, (NO_CREDENTIALS,))
    sources: list[str] = []
    rules = _host_rules(caps, home)
    if not sandbox_login and rules is not None and agent in rules[0]:
        sources.append(HOST_CREDENTIALS)
    if agent == "copilot":
        sources.append(TOKEN_LOGIN)
    if gateway and agent in GATEWAY_AGENTS:
        sources.append(AGENT_GATEWAY)
    if sandbox_login:
        sources.append(SANDBOX_LOGIN)
    if sources:
        return Availability(True, tuple(sources))
    if rules is None:
        why = (
            f"launcher {caps.version} differs from installed project-sandbox"
            f" {installed_version() or '(none)'}"
            if caps.version
            else "launcher version unknown"
        )
        return Availability(None, reason=f"host availability unknown: {why}")
    path = rules[1].get(agent)
    shown = _home_relative(path) if path else "its host configuration"
    reason = f"no {shown} on this host"
    if agent in GATEWAY_AGENTS:
        reason += "; use the agent gateway or --no-forward-credentials"
    elif path is not None:
        reason += f"; log in to {agent} on the host or use --no-forward-credentials"
    return Availability(False, reason=reason)


def _home_relative(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------- cache


def cache_path(repo: Path | None = None) -> Path:
    return (repo_state_dir(repo) if repo is not None else state_home()) / CACHE_NAME


def save(caps: Capabilities, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    when = caps.discovered_at or datetime.now(UTC)
    doc = {
        "version": CACHE_VERSION,
        "command": list(caps.command),
        "launcher_version": caps.version,
        "discovered_at": when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": caps.source,
        "agents": list(caps.agents),
        "vcs_modes": list(caps.vcs_modes),
        "egress_modes": list(caps.egress_modes),
        "runtimes": {
            name: {"status": rt.status, "detail": rt.detail, "hint": rt.hint}
            for name, rt in caps.runtimes.items()
        },
        "runtime_source": caps.runtime_source,
        "auto_selects": caps.auto_selects,
        "help_ok": caps.help_ok,
        "mount": caps.mount,
        "problems": list(caps.problems),
    }
    fd, tmp = tempfile.mkstemp(prefix=".capabilities-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def _names(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not all(
        isinstance(v, str) and NAME.fullmatch(v) for v in value
    ):
        return None
    return tuple(value)


def load(path: Path) -> Capabilities | None:
    """The cached snapshot, or None if missing, oversized or invalid (never raises)."""
    try:
        if path.stat().st_size > MAX_CACHE_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return None
    command = data.get("command")
    agents, vcs = _names(data.get("agents")), _names(data.get("vcs_modes"))
    egress = _names(data.get("egress_modes"))
    raw_runtimes = data.get("runtimes")
    if (
        not isinstance(command, list)
        or not all(isinstance(c, str) for c in command)
        or not agents
        or vcs is None
        or egress is None
        or not isinstance(raw_runtimes, dict)
        or data.get("source") not in ("probe", "fallback")
        or data.get("runtime_source") not in ("doctor", "help", "fallback")
    ):
        return None
    runtimes: dict[str, RuntimeStatus] = {}
    for name, raw in raw_runtimes.items():
        if not NAME.fullmatch(str(name)) or not isinstance(raw, dict):
            return None
        status = raw.get("status")
        if status not in ("usable", "unavailable", "unknown"):
            return None
        runtimes[name] = RuntimeStatus(status, _text(raw.get("detail")), _text(raw.get("hint")))
    try:
        when = datetime.fromisoformat(str(data.get("discovered_at", "")).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    problems = data.get("problems")
    return Capabilities(
        command=tuple(command),
        version=parse_version(str(data.get("launcher_version", ""))),
        discovered_at=when,
        source=data["source"],
        agents=agents,
        vcs_modes=vcs,
        egress_modes=egress,
        runtimes=runtimes,
        runtime_source=data["runtime_source"],
        auto_selects=_text(data.get("auto_selects"), 32),
        help_ok=data.get("help_ok") is True,
        mount=data.get("mount") is True,
        problems=tuple(_text(p) for p in problems if isinstance(p, str))
        if isinstance(problems, list)
        else (),
    )


def is_fresh(
    caps: Capabilities | None,
    command: Sequence[str],
    *,
    version: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Same command, younger than a day, and (when given) the same launcher version."""
    if caps is None or caps.discovered_at is None or caps.command != tuple(command):
        return False
    if version is not None and caps.version != version:
        return False
    return timedelta(0) <= (now or datetime.now(UTC)) - caps.discovered_at < MAX_AGE


def cached(command: Sequence[str], repo: Path | None = None) -> Capabilities:
    """The cached snapshot for ``command``, or fallbacks. Never probes."""
    caps = load(cache_path(repo))
    if caps is None or caps.command != tuple(command):
        return fallback(command)
    return caps


def refresh(
    command: Sequence[str],
    repo: Path | None = None,
    runner: Runner | None = None,
    *,
    force: bool = False,
) -> Capabilities:
    """Probe unless the cache is fresh for the launcher's current version; save the result."""
    path = cache_path(repo)
    current = load(path)
    if not force and is_fresh(current, command):
        runner = runner or run
        try:
            code, out = runner([*command, "--version"], VERSION_TIMEOUT)
        except OSError:
            code, out = 1, ""
        if code == 0 and current is not None and parse_version(out) == current.version:
            return current
    caps = discover(command, runner)
    with contextlib.suppress(OSError):  # a read-only state directory must not break discovery
        save(caps, path)
    return caps


# ---------------------------------------------------------------- launch checks


def flag_value(args: Sequence[str], flag: str) -> str | None:
    """The last value of ``flag`` in ``--flag=value`` or ``--flag value`` form."""
    value: str | None = None
    items = list(args)
    for index, arg in enumerate(items):
        if arg.startswith(flag + "="):
            value = arg.removeprefix(flag + "=")
        elif arg == flag and index + 1 < len(items):
            value = items[index + 1]
    return value


def launcher_name(caps: Capabilities) -> str:
    shown = " ".join(caps.command) or "project-sandbox"
    return f"{shown} {caps.version}".strip()


def launch_problems(
    caps: Capabilities,
    *,
    agent: str,
    extra_args: Sequence[str] = (),
    runtime: str = "auto",
) -> list[str]:
    """Settings the launcher would reject, by discovered capabilities only.

    ``extra_args`` are the effective switches (``sandbox.extra_args`` then the
    launch's own), so the last ``--runtime``/``--vcs-access`` wins as in argparse.
    A fallback snapshot reports nothing: guesses never block a launch.
    """
    if not caps.discovered:
        return []
    problems: list[str] = []
    who = launcher_name(caps)
    if agent not in caps.agents:
        problems.append(
            f"agent {agent!r} is not supported by {who}; supported: {', '.join(caps.agents)}"
        )
    vcs = flag_value(extra_args, "--vcs-access")
    if vcs is not None and vcs not in caps.vcs_modes:
        problems.append(
            f"--vcs-access={vcs} is not supported by {who}; supported: {', '.join(caps.vcs_modes)}"
            if caps.vcs_modes
            else f"{who} has no --vcs-access option"
        )
    chosen = flag_value(extra_args, "--runtime") or runtime
    if caps.runtime_source != "fallback" and chosen not in caps.runtimes:
        problems.append(
            f"runtime {chosen!r} is not supported by {who}; supported: {', '.join(caps.runtimes)}"
        )
    return problems


def level_problems(caps: Capabilities, cfg: Config, level: int | None) -> list[str]:
    """Why the launcher cannot run ``level``, by discovered capabilities only."""
    if level is None or not caps.discovered:
        return []
    who = launcher_name(caps)
    problems: list[str] = []
    if level >= 2 and not caps.egress_modes:
        problems.append(f"level {level} needs --egress, but {who} has no --egress option")
    elif level >= 3 and cfg.security.egress not in caps.egress_modes:
        problems.append(
            f"level {level}: security.egress = {cfg.security.egress!r} is not supported by "
            f"{who}; supported egress modes: {', '.join(caps.egress_modes)}"
        )
    if level >= 4:
        mode = "read-only" if level == 4 else "hidden"
        if not caps.vcs_modes:
            problems.append(
                f"level {level} needs --vcs-access, but {who} has no --vcs-access option"
            )
        elif mode not in caps.vcs_modes:
            problems.append(f"level {level}: --vcs-access={mode} is not supported by {who}")
    return problems


def provider_availability(
    cfg: Config, caps: Capabilities, row: catalogue.ProviderRow, *, home: Path | None = None
) -> Availability:
    """Whether a provider row can launch on this host, with the reason when not."""
    agent = catalogue.row_agent(cfg, row)
    if row.gateway and not cfg.network.agent_proxy:
        return Availability(False, reason="no agent gateway is configured (network.agent_proxy)")
    if caps.discovered and agent not in caps.agents:
        return Availability(
            False, reason=f"agent {agent} is not supported by {launcher_name(caps)}"
        )
    if row.gateway:
        return Availability(True, (AGENT_GATEWAY,))
    return agent_sources(agent, caps, home=home, cfg=cfg)


def gateway_problems(agent: str, model: str, gateway: bool) -> list[str]:
    """project-sandbox's fixed rules for ``--agent-proxy``; apply without discovery."""
    if not gateway:
        return []
    if agent not in GATEWAY_AGENTS:
        return [
            f"the agent gateway supports only {', '.join(GATEWAY_AGENTS)}, not {agent!r}; "
            "turn gateway off for this launch"
        ]
    if not model:
        return ["the agent gateway requires a model: choose one of the gateway's models"]
    return []


# ---------------------------------------------------------------- setup


def profile_agents(
    caps: Capabilities,
    *,
    gateway: bool = False,
    home: Path | None = None,
    cfg: Config | None = None,
) -> list[tuple[str, Availability]]:
    """Agents a sandbox profile can use, with their sources; for the setup wizard.

    ``gateway`` is True when a gateway is configured or the user plans to use one.
    Agents without any source are left out; unknown availability is kept.
    """
    found = [
        (agent, agent_sources(agent, caps, gateway_configured=gateway, home=home, cfg=cfg))
        for agent in caps.agents
    ]
    return [(agent, result) for agent, result in found if result.available is not False]


def _catalogue_summary(cfg: Config, caps: Capabilities, home: Path | None) -> list[str]:
    from project_sandbox_tui.security import LEVEL_LABELS

    rows = []
    for row in catalogue.ROWS:
        result = provider_availability(cfg, caps, row, home=home)
        state = "available" if result.available else "unavailable"
        if result.available is None:
            state = "availability unknown"
        detail = result.describe()
        rows.append(f"{row.id} ({row.label}: {state}{f', {detail}' if detail else ''})")
    supported = [
        f"{level} ({LEVEL_LABELS[level].split(' · ', 1)[1]})"
        for level in LEVEL_LABELS
        if not level_problems(caps, cfg, level)
    ]
    personas = "; ".join(
        f"{name} ({persona.label()})"
        for name, persona in cfg.personas.items()
        if not persona.internal
    )
    lines = [
        f"Provider rows for catalogue.default_provider: {'; '.join(rows)}.",
        "Security levels the launcher supports with security.egress = "
        f"{cfg.security.egress!r}: {', '.join(supported) or 'none'}.",
        f"Personas (tier / effort / fast / level) for actions.NAME.persona: {personas}.",
    ]
    if caps.discovered and caps.egress_modes and "sidecar" not in caps.egress_modes:
        lines.append(
            "The launcher has no sidecar egress: levels 3 to 5 need "
            'security.egress = "firewall" in the global config.'
        )
    return lines


def setup_summary(
    caps: Capabilities,
    *,
    gateway: bool = False,
    home: Path | None = None,
    cfg: Config | None = None,
) -> str:
    """Discovered capabilities for the setup agent's prompt."""
    agents = "; ".join(
        f"{agent} ({result.describe()})"
        for agent, result in profile_agents(caps, gateway=gateway, home=home, cfg=cfg)
    )
    usable = [
        name for name, rt in caps.runtimes.items() if rt.status == "usable" and name != "auto"
    ]
    if caps.runtime_source == "doctor":
        runtimes = ", ".join(usable) or "none usable"
        if caps.auto_selects:
            runtimes += f" (auto selects {caps.auto_selects})"
    else:
        runtimes = "status unknown; choices: " + ", ".join(caps.runtimes)
    lines = [
        f"Profile agents and how they can run: {agents or 'none'}.",
        "VCS access modes for profile extra_args: "
        + (", ".join(f"--vcs-access={m}" for m in caps.vcs_modes) or "none")
        + ".",
        f"Isolation runtimes: {runtimes}.",
    ]
    if cfg is not None:
        lines += _catalogue_summary(cfg, caps, home)
    if not caps.discovered:
        command = " ".join(caps.command) or "project-sandbox"
        lines.append(
            "These are built-in fallbacks: the launcher could not be probed. "
            f"Check `{command} --help` for supported agents and modes."
        )
    return "\n".join(lines)
