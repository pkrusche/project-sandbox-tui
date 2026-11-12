"""Building ``project-sandbox`` command lines and reading its session records.

The TUI never talks to a container runtime for normal operation; everything
goes through the ``project-sandbox`` CLI so isolation, credential handling and
workspace teardown stay in one place.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from project_sandbox_tui.config import FAST_AGENTS, Config, ConfigError
from project_sandbox_tui.vcs import WORKING_COPY, workspace_path

Mode = Literal["interactive", "background"]


@dataclass
class LaunchSpec:
    branch: str
    agent: str
    model: str = ""
    effort: str = ""
    mode: Mode = "interactive"
    prompt_text: str = ""
    timeout: int = 0  # seconds, 0 = none
    # Octopus integration: the result branch is ``branch``; ``inputs`` were merged
    # (or stacked onto ``base``) by sbx-tui before the session (see octopus.py).
    operation: str = ""  # "" | "merge" | "rebase"
    inputs: list[str] = field(default_factory=list)
    base: str = ""
    fast: bool = False
    profile: str = ""
    extra_args: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "agent": self.agent,
            "model": self.model,
            "effort": self.effort,
            "mode": self.mode,
            "prompt_text": self.prompt_text,
            "timeout": self.timeout,
            "operation": self.operation,
            "inputs": list(self.inputs),
            "base": self.base,
            "fast": self.fast,
            "profile": self.profile,
            "extra_args": list(self.extra_args),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LaunchSpec:
        timeout = raw.get("timeout", 0)
        if type(timeout) is not int or timeout < 0:
            raise ValueError("invalid timeout")
        return cls(
            branch=str(raw.get("branch", "")),
            agent=str(raw.get("agent", "")),
            model=str(raw.get("model", "")),
            effort=str(raw.get("effort", "")),
            mode="background" if raw.get("mode") == "background" else "interactive",
            prompt_text=str(raw.get("prompt_text", "")),
            timeout=timeout,
            operation=str(raw.get("operation") or ""),
            inputs=[str(i) for i in raw.get("inputs") or [] if isinstance(i, str)],
            base=str(raw.get("base") or ""),
            fast=raw.get("fast") is True,
            profile=str(raw.get("profile") or ""),
            extra_args=[str(arg) for arg in raw.get("extra_args") or [] if isinstance(arg, str)],
        )

    @property
    def main_checkout(self) -> bool:
        """Sessions on the working copy run in the repository's own workspace (no ``--branch``)."""
        return self.branch == WORKING_COPY

    def label(self) -> str:
        parts = [self.agent]
        if self.model:
            parts.append(self.model)
        if self.effort:
            parts.append(self.effort)
        if self.fast and self.agent in FAST_AGENTS:
            parts.append("fast")
        return "/".join(parts)


@dataclass
class RunPaths:
    """Where an unsupervised run writes its output."""

    log: Path
    summary: Path
    stdout: Path
    extra: list[Path] = field(default_factory=list)

    def transcript(self) -> Path:
        # project-sandbox writes a Markdown transcript beside the --log file.
        return self.log.with_suffix(".md")


def build_argv(
    cfg: Config,
    repo: Path,
    spec: LaunchSpec,
    run_paths: RunPaths | None = None,
) -> list[str]:
    """Return the full ``project-sandbox`` argv for ``spec``."""
    if not cfg.has_image_source():
        raise ConfigError(
            "no image configured: set project.base_image, project.dockerfile or "
            f"project.build in {repo / '.project-sandbox-tui.toml'} (try `sbx-tui --init`)"
        )
    if spec.mode == "background" and not spec.prompt_text.strip():
        raise ConfigError("an unsupervised run needs a prompt")
    if spec.mode == "background" and run_paths is None:
        raise ValueError("background runs need run paths")

    p, n, s = cfg.project, cfg.network, cfg.sandbox
    argv = [*cfg.sandbox_command(), str(repo.resolve())]

    # Values go in --flag=value form: a separate word starting with "-" (a remote
    # bookmark "-x", a prompt "--help") would be parsed as an option.

    # image source (mutually exclusive, validated in Config.validate)
    if p.base_image:
        argv.append(p.base_image)
    elif p.dockerfile:
        argv.append(f"--dockerfile={cfg.resolve_path(p.dockerfile, repo)}")
        if p.docker_context:
            argv.append(f"--docker-context={cfg.resolve_path(p.docker_context, repo)}")
    else:
        argv.append(f"--{p.build}")
        if p.build_version:
            flag = "--python" if p.build == "python-uv" else "--rust"
            argv.append(f"{flag}={p.build_version}")

    if s.runtime != "auto":
        argv.append(f"--runtime={s.runtime}")
    if s.memory:
        argv.append(f"--memory={s.memory}")
    if s.cpus:
        argv.append(f"--cpus={s.cpus}")

    # branch / workspace (project-sandbox rejects the workspace flags without --branch)
    if not spec.main_checkout:
        argv.append(f"--branch={spec.branch}")
        if p.worktree_dir:
            argv.append(f"--worktree-dir={cfg.resolve_path(p.worktree_dir, repo)}")
        if p.keep_workspace:
            argv.append("--keep-workspace")

    # agent
    argv.append(f"--agent={spec.agent}")
    if spec.agent != "bash":
        if spec.model:
            argv.append(f"--model={spec.model}")
        if spec.effort:
            argv.append(f"--effort={spec.effort}")
    if spec.fast and spec.agent in FAST_AGENTS:
        argv.append("--fast")

    # network
    if n.internet_proxy:
        argv.append(f"--internet-proxy={n.internet_proxy}")
    if n.agent_proxy:
        argv.append(f"--agent-proxy={n.agent_proxy}")
        if n.agent_proxy_key_env:
            argv.append(f"--agent-proxy-key-env={n.agent_proxy_key_env}")
    for cert in n.ca_certs:
        argv.append(f"--ca-cert={cfg.resolve_path(cert, repo)}")
    for domain in n.extra_domains:
        argv.append(f"--extra-domain={domain}")
    if n.allow_github:
        argv.append("--allow-github")
    if n.no_firewall:
        argv.append("--no-firewall")

    # credentials
    if s.no_forward_credentials:
        argv.append("--no-forward-credentials")
    for var in s.api_key_env:
        argv.append(f"--api-key-env={var}")
    for env_file in s.api_key_env_file:
        argv.append(f"--api-key-env-file={cfg.resolve_path(env_file, repo)}")

    argv += s.extra_args
    argv += spec.extra_args

    if spec.mode == "background":
        assert run_paths is not None
        argv += [
            f"--prompt-text={spec.prompt_text}",
            f"--log={run_paths.log}",
            f"--json-summary={run_paths.summary}",
        ]
        if spec.timeout:
            argv.append(f"--timeout={spec.timeout}")
    return argv


# ---------------------------------------------------------------- session records


def sessions_dir() -> Path:
    configured = os.environ.get("XDG_STATE_HOME", "")
    base = Path(configured) if configured.startswith("/") else Path.home() / ".local" / "state"
    return base / "project-sandbox" / "sessions"


def pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OverflowError:
        return False
    return True


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    container_name: str
    project_path: str
    workspace_path: str
    agent: str
    status: str
    pid: int | None
    started_at: str
    runtime: str = ""

    @property
    def running(self) -> bool:
        return self.status == "running"


def read_sessions(directory: Path | None = None) -> list[SessionRecord]:
    """Read project-sandbox session records directly from its state directory.

    Equivalent to ``project-sandbox sessions list --json`` (including the
    running→orphaned downgrade for dead PIDs) without spawning a process on
    every refresh tick.
    """
    directory = directory or sessions_dir()
    try:
        files = sorted(directory.glob("*.json"))
    except OSError:
        return []
    records: list[SessionRecord] = []
    for path in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        status = str(raw.get("status") or "")
        pid = raw.get("pid") if type(raw.get("pid")) is int else None
        if status == "running" and not pid_alive(pid):
            status = "orphaned"
        records.append(
            SessionRecord(
                session_id=str(raw.get("session_id") or path.stem),
                container_name=str(raw.get("container_name") or ""),
                project_path=str(raw.get("project_path") or ""),
                workspace_path=str(raw.get("workspace_path") or ""),
                agent=str(raw.get("agent") or ""),
                status=status,
                pid=pid,
                started_at=str(raw.get("started_at") or ""),
                runtime=str(raw.get("runtime") or ""),
            )
        )
    return records


def running_by_branch(
    records: list[SessionRecord],
    repo: Path,
    branches: list[str],
    worktree_dir: Path | None,
) -> dict[str, SessionRecord]:
    """Map branch name → running session, matching on the workspace path.

    A session whose workspace is the repository itself (started without
    ``--branch``) belongs to the working copy.
    """
    by_path = {str(workspace_path(repo, name, worktree_dir).resolve()): name for name in branches}
    by_path[str(repo.resolve())] = WORKING_COPY
    out: dict[str, SessionRecord] = {}
    for rec in records:
        if not rec.running or not rec.workspace_path:
            continue
        name = by_path.get(str(Path(rec.workspace_path).resolve()))
        if name is not None:
            out[name] = rec
    return out


def runtime_cli(runtime: str) -> str | None:
    """The container CLI to use for a fallback ``stop``."""
    if runtime == "apple-container":
        return "container"
    if runtime in ("docker", "podman"):
        return runtime
    if platform.system() == "Darwin" and shutil.which("container"):
        return "container"
    for candidate in ("docker", "podman"):
        if shutil.which(candidate):
            return candidate
    return None
