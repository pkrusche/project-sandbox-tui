"""Command-line entry point: ``sbx-tui [REPO]``, ``sbx-tui config …``, ``sbx-tui doctor``."""

from __future__ import annotations

import dataclasses
import json
import shlex
import sys
from argparse import ArgumentParser, Namespace
from pathlib import Path

from project_sandbox_tui import __version__, initialization
from project_sandbox_tui import config as config_mod
from project_sandbox_tui import models as models_mod
from project_sandbox_tui.config import ConfigError
from project_sandbox_tui.vcs import JjBackend, VcsError, detect, sort_branches

SUBCOMMANDS = ("config", "doctor")  # a repository with one of these names: sbx-tui ./config


def build_parser() -> ArgumentParser:
    p = ArgumentParser(
        prog="sbx-tui",
        description="Minimal vim/ranger-style TUI for project-sandbox sessions per jj bookmark.",
        epilog="subcommands: sbx-tui config {list,get,set,unset,path} …, sbx-tui doctor "
        "(see sbx-tui config --help)",
    )
    p.add_argument("repo", nargs="?", default=".", help="repository (default: current directory)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--config", type=Path, help="global config file (default: XDG config dir)")
    p.add_argument(
        "--init",
        action="store_true",
        help="detect the project type and write missing config templates, install a "
        "gitignored configuration skill, download the model list if missing, then exit",
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
        "bookmark or working copy, then exit",
    )
    p.add_argument(
        "--cleanup-workspaces",
        action="store_true",
        help="remove idle clean or merged workspaces and merged bookmarks, then exit",
    )
    p.add_argument(
        "--trust-config",
        action="store_true",
        help="show how the repository's .project-sandbox-tui.toml differs from the trusted "
        "copy and ask whether to trust it, then exit",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="preview workspace cleanup, or append --dry-run to project-sandbox invocations",
    )
    return p


def build_subcommand_parser() -> ArgumentParser:
    common = ArgumentParser(add_help=False)
    common.add_argument(
        "-R",
        "--repository",
        type=Path,
        default=Path("."),
        help="repository (default: current directory)",
    )
    common.add_argument("--config", type=Path, help="global config file (default: XDG config dir)")
    scope = ArgumentParser(add_help=False)
    group = scope.add_mutually_exclusive_group()
    group.add_argument("--global", dest="scope", action="store_const", const="global")
    group.add_argument("--project", dest="scope", action="store_const", const="project")

    p = ArgumentParser(prog="sbx-tui")
    sub = p.add_subparsers(dest="command", required=True)
    cfg = sub.add_parser(
        "config",
        help="read or change settings",
        description="Read or change settings. --global edits the global config, --project "
        "the repository's .project-sandbox-tui.toml (kept trusted if it was).",
    )
    actions = cfg.add_subparsers(dest="action", required=True)
    actions.add_parser(
        "list",
        parents=[common, scope],
        help="settings in effect, or those one file makes with --global / --project",
    )
    get = actions.add_parser(
        "get", parents=[common], help="a setting in effect, e.g. defaults.agent"
    )
    get.add_argument("key")
    set_ = actions.add_parser(
        "set", parents=[common, scope], help='set KEY to VALUE (lists as TOML: \'["a", "b"]\')'
    )
    set_.add_argument("key")
    set_.add_argument("value")
    unset = actions.add_parser("unset", parents=[common, scope], help="remove KEY from one file")
    unset.add_argument("key")
    actions.add_parser("path", parents=[common, scope], help="the file --global / --project edits")
    sub.add_parser(
        "doctor",
        parents=[common],
        help="check jj, project-sandbox and the container runtime",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in SUBCOMMANDS:
        return _subcommand(build_subcommand_parser().parse_args(argv))
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
            (config_mod.project_config_path(repo), initialization.project_template(repo)),
        ):
            try:
                written = config_mod.write_template(path, template)
                if written and path == config_mod.project_config_path(repo):
                    # Trust only what we wrote, even if the file changes meanwhile.
                    config_mod.ProjectTrust(path, template, None).trust(repo)
            except (OSError, ConfigError) as exc:
                print(f"sbx-tui: could not write {path}: {exc}", file=sys.stderr)
                return 2
            print(f"{'wrote' if written else 'exists'}: {path}")
        try:
            template = initialization.skill_template()
            updated = initialization.ignore_skills(repo)
            print(f"{'updated' if updated else 'exists'}: {repo / '.gitignore'}")
            for directory in initialization.SKILL_DIRS:
                path = repo / directory / "SKILL.md"
                written = config_mod.write_template(path, template)
                print(f"{'wrote' if written else 'exists'}: {path}")
        except (OSError, ValueError) as exc:
            print(f"sbx-tui: could not install configuration skill: {exc}", file=sys.stderr)
            return 2
        if models_mod.load_store() is None:
            # not fatal: the launch form works without it
            _update_models(models_mod.DEFAULT_SOURCE, required=False)
        return 0

    try:
        if args.trust_config:
            return _trust_config(repo, args.config)
        trust = config_mod.ProjectTrust.check(repo)
        cfg = config_mod.load(repo, args.config)
    except ConfigError as exc:
        print(f"sbx-tui: config error: {exc}", file=sys.stderr)
        return 2
    headless = args.cleanup_workspaces or args.print_config
    if trust.needs_review and headless:  # the TUI asks for a review itself
        print(f"sbx-tui: {trust.describe()} (review: sbx-tui --trust-config)", file=sys.stderr)
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


def _subcommand(args: Namespace) -> int:
    path = args.repository.expanduser()
    if not path.is_dir():
        print(f"sbx-tui: {path}: no such directory", file=sys.stderr)
        return 2
    if args.command == "doctor":
        from project_sandbox_tui import doctor

        return doctor.main(path, args.config)
    try:
        repo: Path | None = detect(path).root
    except VcsError:
        repo = None  # global settings work outside a repository
    try:
        return _config(args, repo)
    except ConfigError as exc:
        print(f"sbx-tui: {exc}", file=sys.stderr)
        return 2


def _config(args: Namespace, repo: Path | None) -> int:
    from project_sandbox_tui import settings

    scope: settings.Scope | None = getattr(args, "scope", None)
    if args.action == "list":
        if scope is None:
            data = settings.effective_values(repo, args.config)
            _warn_untrusted(repo)
        else:
            data = settings.file_values(scope, repo, args.config)
        for key, value in settings.flatten(data):
            print(f"{key} = {settings.format_value(value)}")
        return 0

    if args.action == "get":
        key = settings.parse_key(args.key)
        _warn_untrusted(repo)
        table = settings.effective_values(repo, args.config)
        for part in key.path[:-1]:
            table = table.get(part, {})
        if key.path[-1] not in table:
            return 1  # like git config: unset
        value = table[key.path[-1]]
        print(value if isinstance(value, str) else settings.format_value(value))
        return 0

    if scope is None:
        raise ConfigError(f"config {args.action} needs --global or --project")
    editor = settings.Editor(scope, repo, args.config)
    if args.action == "path":
        print(editor.path)
        return 0
    key = settings.parse_key(args.key)
    untrusted = (
        scope == "project"
        and repo is not None
        and (config_mod.ProjectTrust.check(repo).needs_review)
    )
    if args.action == "set":
        editor.set(key, settings.parse_value(key, args.value))
    elif not editor.unset(key):
        print(f"{key} is not set in {editor.path}", file=sys.stderr)
        return 1
    if untrusted:
        print(
            f"note: {editor.path} is not trusted; review it with sbx-tui --trust-config",
            file=sys.stderr,
        )
    return 0


def _warn_untrusted(repo: Path | None) -> None:
    if repo is None:
        return
    trust = config_mod.ProjectTrust.check(repo)
    if trust.needs_review:
        print(f"sbx-tui: {trust.describe()} (review: sbx-tui --trust-config)", file=sys.stderr)


def _trust_config(repo: Path, global_path: Path | None) -> int:
    trust = config_mod.ProjectTrust.check(repo)
    if not trust.needs_review:
        state = "trusted" if trust.current is not None else "not present"
        print(f"{trust.path}: {state}")
        return 0
    config_mod.load_candidate(trust, global_path)  # never trust a config that cannot load
    print(trust.diff(), end="")
    try:
        answer = input(f"Trust {trust.path}? It can run commands on this machine. [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        print("not trusted")
        return 1
    trust.trust(repo)
    print(f"trusted: {trust.path}")
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
            fast=d.fast,
        )
        print(shlex.join(build_argv(cfg, repo, spec)))
    except ConfigError as exc:
        print(f"# cannot build command: {exc}")
    return 0
