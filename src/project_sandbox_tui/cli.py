"""Command-line entry point: ``sbx-tui [REPO]``."""

from __future__ import annotations

import dataclasses
import json
import shlex
import sys
from argparse import ArgumentParser
from pathlib import Path

from project_sandbox_tui import __version__
from project_sandbox_tui import config as config_mod
from project_sandbox_tui.config import ConfigError
from project_sandbox_tui.vcs import VcsError, detect


def build_parser() -> ArgumentParser:
    p = ArgumentParser(
        prog="sbx-tui",
        description="Minimal vim/ranger-style TUI for running project-sandbox sessions per branch.",
    )
    p.add_argument("repo", nargs="?", default=".", help="repository (default: current directory)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--config", type=Path, help="global config file (default: XDG config dir)")
    p.add_argument(
        "--init",
        action="store_true",
        help="write a commented .project-sandbox-tui.toml into the repo (and the global "
        "config if missing), then exit",
    )
    p.add_argument(
        "--print-config",
        action="store_true",
        help="print the merged configuration and the interactive command for the current "
        "branch, then exit",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="append --dry-run to every project-sandbox invocation (try the TUI safely)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        backend = detect(Path(args.repo))
    except VcsError as exc:
        print(f"sbx-tui: {exc}", file=sys.stderr)
        return 2
    repo = backend.root

    if args.init:
        gpath = args.config or config_mod.global_config_path()
        for path, template in (
            (gpath, config_mod.GLOBAL_TEMPLATE),
            (config_mod.project_config_path(repo), config_mod.PROJECT_TEMPLATE),
        ):
            written = config_mod.write_template(path, template)
            print(f"{'wrote' if written else 'exists'}: {path}")
        return 0

    try:
        cfg = config_mod.load(repo, args.config)
    except ConfigError as exc:
        print(f"sbx-tui: config error: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        cfg.sandbox.extra_args.append("--dry-run")

    if args.print_config:
        return _print_config(repo, backend, cfg)

    from project_sandbox_tui.app import SandboxTUI  # import Textual only when needed

    SandboxTUI(repo, backend, cfg).run()
    return 0


def _print_config(repo: Path, backend: object, cfg: config_mod.Config) -> int:
    from project_sandbox_tui.sandbox import LaunchSpec, build_argv

    data = dataclasses.asdict(cfg)
    data["sources"] = [str(p) for p in cfg.sources]
    print(json.dumps(data, indent=2))
    branches = backend.list_branches()  # type: ignore[attr-defined]
    current = next((b.name for b in branches if b.is_current), branches[0].name if branches else "")
    if not current:
        return 0
    d = config_mod.resolve_launch_defaults(cfg, config_mod.LastUsed.load(repo))
    spec = LaunchSpec(branch=current, agent=d.agent, model=d.model, effort=d.effort)
    try:
        print("\n# interactive command for", current)
        print(shlex.join(build_argv(cfg, repo, spec)))
    except ConfigError as exc:
        print(f"# cannot build command: {exc}")
    return 0
