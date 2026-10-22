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
from project_sandbox_tui import models as models_mod
from project_sandbox_tui.config import ConfigError
from project_sandbox_tui.vcs import JjBackend, VcsError, detect, sort_branches


def build_parser() -> ArgumentParser:
    p = ArgumentParser(
        prog="sbx-tui",
        description="Minimal vim/ranger-style TUI for project-sandbox sessions per jj bookmark.",
    )
    p.add_argument("repo", nargs="?", default=".", help="repository (default: current directory)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--config", type=Path, help="global config file (default: XDG config dir)")
    p.add_argument(
        "--init",
        action="store_true",
        help="write a commented .project-sandbox-tui.toml into the repo (and the global "
        "config if missing), download the model list if there is none yet, then exit",
    )
    p.add_argument(
        "--update-models",
        nargs="?",
        const=models_mod.DEFAULT_SOURCE,
        metavar="SOURCE",
        help="download the model list for the launch form (default: "
        f"{models_mod.DEFAULT_SOURCE}; or an https:// URL or a local file of the same "
        "format) into the state directory, then exit",
    )
    p.add_argument(
        "--print-config",
        action="store_true",
        help="print the merged configuration and the interactive command for the current "
        "branch, then exit",
    )
    p.add_argument(
        "--cleanup-workspaces",
        action="store_true",
        help="remove idle clean or merged workspaces and merged bookmarks, then exit",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="preview workspace cleanup, or append --dry-run to project-sandbox invocations",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.update_models is not None:  # needs no repository
        return _update_models(args.update_models)
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
        if models_mod.load_store() is None:
            # not fatal: the launch form works without it
            _update_models(models_mod.DEFAULT_SOURCE, required=False)
        return 0

    try:
        cfg = config_mod.load(repo, args.config)
    except ConfigError as exc:
        print(f"sbx-tui: config error: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        cfg.sandbox.extra_args.append("--dry-run")

    if args.cleanup_workspaces:
        from project_sandbox_tui.cleanup import cleanup_workspaces

        try:
            results = cleanup_workspaces(backend, cfg, dry_run=args.dry_run)
        except (VcsError, OSError) as exc:
            print(f"sbx-tui: {exc}", file=sys.stderr)
            return 2
        print("\n".join(results) or f"No workspaces found besides {backend.root}.")
        return 0

    if args.print_config:
        return _print_config(repo, backend, cfg)

    from project_sandbox_tui.app import SandboxTUI  # import Textual only when needed

    SandboxTUI(repo, backend, cfg, config_path=args.config, dry_run=args.dry_run).run()
    return 0


def _update_models(source: str, *, required: bool = True) -> int:
    try:
        path, store = models_mod.update(source)
    except models_mod.ModelsError as exc:
        print(f"sbx-tui: models: {exc}", file=sys.stderr)
        if not required:
            print("sbx-tui: models: run `sbx-tui --update-models` later", file=sys.stderr)
        return 2 if required else 0
    print(f"models: {models_mod.describe(store)} -> {path}")
    return 0


def _print_config(repo: Path, backend: JjBackend, cfg: config_mod.Config) -> int:
    from project_sandbox_tui.sandbox import LaunchSpec, build_argv

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
    branch = branches[0]  # the working copy
    d = config_mod.resolve_launch_defaults(cfg, config_mod.LastUsed.load(repo))
    print("\n# interactive command for", branch.name)
    try:
        spec = LaunchSpec(
            branch=branch.name,
            agent=d.agent,
            model=d.model,
            effort=d.effort,
        )
        print(shlex.join(build_argv(cfg, repo, spec)))
    except ConfigError as exc:
        print(f"# cannot build command: {exc}")
    return 0
