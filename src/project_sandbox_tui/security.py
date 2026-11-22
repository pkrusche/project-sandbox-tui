"""Security levels: a linear 1-5 isolation scale compiled to launcher flags.

1. ``--no-firewall``
2. ``--egress=firewall`` with ``--extra-domain`` for the project's package sources
   and ``network.extra_domains`` (plus ``--allow-github`` when configured)
3. the egress backend only (``security.egress``: sidecar or firewall), i.e. agent API
4. level 3 plus ``--vcs-access=read-only``
5. level 3 plus ``--vcs-access=hidden``

When a level is in effect it is the only source of network and VCS flags:
the same flags in ``extra_args`` are refused rather than appended, because the
launcher accumulates repeatable flags and has no negation for ``--no-firewall``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from project_sandbox_tui.config import Config

if TYPE_CHECKING:
    from project_sandbox_tui.sandbox import LaunchSpec

LEVEL_LABELS: dict[int, str] = {
    1: "1 · no firewall",
    2: "2 · firewall + package sources",
    3: "3 · agent API only",
    4: "4 · API only, VCS read-only",
    5: "5 · API only, no VCS",
}

BUILD_SOURCES: dict[str, tuple[str, ...]] = {
    "python-uv": ("pypi.org", "files.pythonhosted.org"),
    "rust-cargo": ("crates.io", "index.crates.io", "static.crates.io"),
    "node-npm": ("registry.npmjs.org",),
}

# Flags levels own; with a level in effect they may not come from extra_args.
MANAGED_FLAGS: tuple[str, ...] = (
    "--no-firewall",
    "--egress",
    "--extra-domain",
    "--allow-github",
    "--vcs-access",
    "--pi-ollama",
    "--internet-proxy",
    "--no-internet-proxy-preflight",
    "--ca-cert",
    "--build-proxy",
    "--build-no-proxy",
    "--build-ca-cert",
)
_HINTS: dict[str, str] = {
    "--no-firewall": "use level 1 for the jobs that need it",
    "--extra-domain": "use network.extra_domains with level 2",
    "--allow-github": "use network.allow_github with level 2",
    "--vcs-access": "use level 4 (read-only) or 5 (hidden)",
    "--egress": "set security.egress in the global config",
    "--pi-ollama": "remove it; no security level reaches a host-run Ollama",
}
_CORPORATE_HINT = "set the network setting in the global config"


def effective_level(cfg: Config, spec: LaunchSpec) -> int | None:
    """The job's level, else the project default; None keeps the earlier behaviour."""
    return spec.level or cfg.security.level or None


def level_flags(cfg: Config, level: int, agent: str = "") -> list[str]:
    """Exactly the network and VCS flags ``level`` compiles to."""
    if level == 1:
        return ["--no-firewall"]
    if level == 2:
        flags = ["--egress=firewall"]
        n = cfg.network
        if not n.internet_proxy:  # the launcher rejects --extra-domain with a proxy
            domains = [*BUILD_SOURCES.get(cfg.project.build, ()), *n.extra_domains]
            flags += [f"--extra-domain={domain}" for domain in dict.fromkeys(domains)]
        if n.allow_github:
            flags.append("--allow-github")
        return flags
    flags = [f"--egress={cfg.security.egress}"]
    if level == 4:
        flags.append("--vcs-access=read-only")
    elif level == 5:
        flags.append("--vcs-access=hidden")
    return flags


def level_hint(cfg: Config, level: int | None, agent: str = "") -> str:
    """One line for previews: the level, its backend, flags and caveats."""
    if level is None:
        return "no security level: network flags from [network] and extra arguments"
    parts = [f"level {LEVEL_LABELS.get(level, str(level))}"]
    if level >= 3:
        parts.append(f"egress {cfg.security.egress}")
    parts.append(" ".join(level_flags(cfg, level, agent)))
    if level == 2 and cfg.network.internet_proxy:
        parts.append("the internet proxy's policy decides which hosts are reachable")
    if agent == "copilot" and level >= 2:
        parts.append("Copilot also allows GitHub hosts (--allow-github is implied)")
    return " · ".join(parts)


def _option(arg: str) -> str:
    return arg.partition("=")[0]


def managed_conflicts(args: Sequence[str]) -> list[str]:
    """Managed flags in ``args``, in ``--flag=value`` or ``--flag value`` form.

    argparse also accepts unambiguous abbreviations, so a prefix of a managed
    flag counts as that flag.
    """
    found: list[str] = []
    for arg in args:
        option = _option(arg)
        if not option.startswith("--") or len(option) <= 2:
            continue
        found += [flag for flag in MANAGED_FLAGS if flag.startswith(option)]
    return list(dict.fromkeys(found))


def conflict_message(key: str, flags: Sequence[str]) -> str:
    hints = list(dict.fromkeys(_HINTS.get(flag, _CORPORATE_HINT) for flag in flags))
    return f"{key} contains {', '.join(flags)}, which security levels control; " + "; ".join(hints)


def security_problems(cfg: Config, spec: LaunchSpec) -> list[str]:
    """Managed flags in the shared, profile or custom extra arguments of a launch
    that runs at a security level."""
    if effective_level(cfg, spec) is None:
        return []
    problems = []
    if flags := managed_conflicts(cfg.sandbox.extra_args):
        problems.append(conflict_message("sandbox.extra_args", flags))
    if flags := managed_conflicts(spec.extra_args):
        key = f"profiles.{spec.profile}.extra_args" if spec.profile else "custom CLI args"
        problems.append(conflict_message(key, flags))
    return problems
