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
from project_sandbox_tui.vcs import Backend, VcsError, detect, sort_branches


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
    target = Path(args.repo).expanduser()
    if not target.is_dir():
        # detect() walks up parent directories, so a mistyped path would
        # otherwise silently open whatever repository surrounds it.
        print(f"sbx-tui: {args.repo}: no such directory", file=sys.stderr)
        return 2
    try:
        backend = detect(target)
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
            try:
                written = config_mod.write_template(path, template)
            except OSError as exc:
                print(f"sbx-tui: could not write {path}: {exc}", file=sys.stderr)
                return 2
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


def _print_config(repo: Path, backend: Backend, cfg: config_mod.Config) -> int:
    from project_sandbox_tui.sandbox import (
        CheckoutConflict,
        LaunchSpec,
        build_argv,
        uses_main_checkout,
    )

    data = dataclasses.asdict(cfg)
    data["sources"] = [str(p) for p in cfg.sources]
    print(json.dumps(data, indent=2))
    try:
        branches = sort_branches(backend.list_branches())
    except VcsError as exc:
        print(f"sbx-tui: {exc}", file=sys.stderr)
        return 2
    if not branches:
        return 0
    branch = branches[0]  # current branch first, else most recent
    d = config_mod.resolve_launch_defaults(cfg, config_mod.LastUsed.load(repo))
    worktree_dir = (
        cfg.resolve_path(cfg.project.worktree_dir, repo) if cfg.project.worktree_dir else None
    )
    print("\n# interactive command for", branch.name)
    try:
        spec = LaunchSpec(
            branch=branch.name,
            agent=d.agent,
            model=d.model,
            effort=d.effort,
            main_checkout=uses_main_checkout(repo, branch, worktree_dir),
        )
        print(shlex.join(build_argv(cfg, repo, spec)))
    except (ConfigError, CheckoutConflict) as exc:
        print(f"# cannot build command: {exc}")
    return 0
