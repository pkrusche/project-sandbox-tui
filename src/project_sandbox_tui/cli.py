"""Command-line entry point: ``sbx-tui [REPO]``, ``sbx-tui config …``, ``sbx-tui doctor``."""

from __future__ import annotations

import contextlib
import dataclasses
import difflib
import hashlib
import json
import os
import shlex
import sys
import urllib.parse
import uuid
from argparse import ArgumentParser, Namespace
from datetime import UTC, datetime
from pathlib import Path

from project_sandbox_tui import __version__, commands, initialization
from project_sandbox_tui import config as config_mod
from project_sandbox_tui import models as models_mod
from project_sandbox_tui.config import ConfigError
from project_sandbox_tui.vcs import JjBackend, VcsError, detect, sort_branches

SUBCOMMANDS = (
    "config",
    "doctor",
    "workflow",
)  # a repository with one of these names: sbx-tui ./config


def build_parser() -> ArgumentParser:
    p = ArgumentParser(
        prog="sbx-tui",
        description="Minimal vim/ranger-style TUI for project-sandbox sessions per jj bookmark.",
        epilog="subcommands: sbx-tui config {list,get,set,unset,path} …, sbx-tui doctor "
        "(use --help after a subcommand for details)",
    )
    p.add_argument("repo", nargs="?", default=".", help="repository (default: current directory)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--config", type=Path, help="global config file (default: XDG config dir)")
    p.add_argument(
        "--init",
        action="store_true",
        help="detect the project type and write missing config templates, install "
        "gitignored agent skills and feedback guidance, download the model list "
        "if missing, then exit",
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
        help="print the merged configuration and an interactive sandbox command for the "
        "working copy, then exit",
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
        help="preview workspace cleanup or sandbox launches; "
        "saved prompt queues are not dispatched",
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
    group.add_argument(
        "--global", dest="scope", action="store_const", const="global", help="use the global file"
    )
    group.add_argument(
        "--project",
        dest="scope",
        action="store_const",
        const="project",
        help="use the repository's .project-sandbox-tui.toml",
    )

    p = ArgumentParser(prog="sbx-tui")
    sub = p.add_subparsers(dest="command", required=True)
    cfg = sub.add_parser(
        "config",
        help="read or change settings",
        description="Read or change settings. set, unset and path require --global or "
        "--project. --project edits the repository's .project-sandbox-tui.toml "
        "(kept trusted if it was).",
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
        help="check dependencies, runtime, configuration and comparisons to main",
        description="Check dependencies, runtime, configuration and comparisons to main. "
        "Failed checks exit 1; warnings alone exit 0.",
    )
    workflow = sub.add_parser("workflow", help="manage shareable workflows")
    workflow_actions = workflow.add_subparsers(dest="action", required=True)
    import_ = workflow_actions.add_parser(
        "import", parents=[common], help="import and review an HTTPS or local workflow file"
    )
    import_.add_argument("source", help="HTTPS URL or local workflow file")
    import_.add_argument("--name", help="workflow name (default: source filename stem)")
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
        try:
            for message in initialization.initialize(repo, args.config):
                print(message)
        except ConfigError as exc:
            print(f"sbx-tui: {exc}", file=sys.stderr)
            return 2
        if models_mod.load_store() is None:
            # not fatal: the launch form works without it
            _update_models(models_mod.DEFAULT_SOURCE, required=False)
        return 0

    try:
        if args.trust_config:
            return _trust_config(repo, args.config)
        cfg = config_mod.load(repo, args.config)
    except ConfigError as exc:
        print(f"sbx-tui: config error: {exc}", file=sys.stderr)
        return 2
    headless = args.cleanup_workspaces or args.print_config
    if headless:  # the TUI shows these and asks for a review itself
        for warning in cfg.warnings:
            _warn_config(warning)
        _warn_untrusted(repo)
        for path, problem in cfg.workflow_problems:
            _warn_config(f"{path}: {problem}")
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

    if cfg.warnings:
        for warning in cfg.warnings:
            _warn_config(warning)
        if not _confirm("Start sbx-tui with these items ignored? [y/N] "):
            return 1

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
        if args.command == "workflow":
            if repo is None:
                raise ConfigError("workflow import requires a repository")
            return _import_workflow(args, repo)
        return _config(args, repo)
    except ConfigError as exc:
        print(f"sbx-tui: {exc}", file=sys.stderr)
        return 2


def _import_workflow(args: Namespace, repo: Path) -> int:
    import tomlkit

    source = args.source
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme and parsed.scheme != "https":
        raise ConfigError("workflow import accepts only https:// URLs and local files")
    fetch_source = source
    if parsed.scheme == "https" and parsed.hostname == "github.com":
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 5 and parts[2] == "blob":
            fetch_source = "https://raw.githubusercontent.com/" + "/".join(parts[:2] + parts[3:])
    try:
        data = models_mod.fetch(fetch_source).decode("utf-8")
        doc = tomlkit.parse(data)
    except (models_mod.FetchError, UnicodeError, ValueError) as exc:
        raise ConfigError(f"workflow import: {exc}") from exc
    doc.pop("source", None)
    body = tomlkit.dumps(doc)
    if not body.endswith("\n"):  # so the hash survives re-import and doctor's reparse
        body += "\n"
    name = args.name or Path(parsed.path if parsed.scheme else source).stem
    directory = config_mod.workflow_dir(repo)
    if not commands.NAME.fullmatch(name):
        raise ConfigError(f"invalid workflow name {name!r}")
    # Validate before displaying conflict prompts, including when the requested name
    # is already occupied by an inline workflow.
    cfg = config_mod.load(repo, args.config)
    validation_name = name
    if name in cfg.workflows and not (directory / f"{name}.toml").exists():
        validation_name = "import_candidate"
        while validation_name in cfg.workflows:
            validation_name += "_"
    config_mod.validate_workflow_text(repo, validation_name, body, args.config)
    while True:
        if not commands.NAME.fullmatch(name):
            raise ConfigError(f"invalid workflow name {name!r}")
        cfg = config_mod.load(repo, args.config)
        target = directory / f"{name}.toml"
        present = target.exists() or target.is_symlink()
        if target.is_symlink() or (present and not target.is_file()):
            raise ConfigError(f"{target}: refusing to replace a symlink or non-regular file")
        inline = name in cfg.workflows and not present
        if not inline and not present:
            break
        if not sys.stdin.isatty():
            raise ConfigError(f"workflow {name!r} already exists")
        try:
            if inline:
                print(f"workflow {name!r} is defined inline; choose another name")
                name = input("New name (empty cancels): ").strip()
            else:
                choice = (
                    input(f"{target} exists: overwrite, rename or cancel? [o/r/N] ").strip().lower()
                )
                if choice in ("o", "overwrite"):
                    break
                if choice not in ("r", "rename"):
                    return 1
                name = input("New name (empty cancels): ").strip()
        except EOFError:
            return 1
        if not name:
            return 1
    config_mod.validate_workflow_text(repo, name, body, args.config)
    source_table = tomlkit.table()
    if parsed.scheme:
        source_table.add("url", source)
    else:
        source_table.add("path", str(Path(source).expanduser().resolve()))
    source_table.add("imported_at", datetime.now(UTC).isoformat())
    source_table.add("sha256", hashlib.sha256(body.encode()).hexdigest())
    provenance = tomlkit.document()
    provenance.add("source", source_table)
    final = body + tomlkit.dumps(provenance)
    before = _read_workflow_file(target)
    if before is None:
        print(final, end="" if final.endswith("\n") else "\n")
    else:
        print(
            "".join(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    final.splitlines(keepends=True),
                    fromfile=str(target),
                    tofile="import",
                )
            ),
            end="",
        )
    if not _confirm(f"Import and trust {target}? It can run commands on this machine. [y/N] "):
        return 1
    if directory.is_symlink() or directory.parent.is_symlink() or target.is_symlink():
        raise ConfigError(f"{target}: refusing symlink")
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"cannot create {directory}: {exc}") from exc
    trusted_path = config_mod.repo_state_dir(repo) / "trusted-workflows" / target.name
    with config_mod.config_lock():
        if target.is_symlink() or directory.is_symlink() or directory.parent.is_symlink():
            raise ConfigError(f"{target}: refusing symlink")
        if _read_workflow_file(target) != before:
            raise ConfigError(f"{target}: file changed during review")
        _write_workflow_file(target, final)
        try:
            config_mod._write_trusted_file(trusted_path, final)
        except OSError as exc:
            raise ConfigError(f"cannot write {trusted_path}: {exc}") from exc
    print(f"imported and trusted: {target}")
    return 0


def _read_workflow_file(target: Path) -> str | None:
    """The workflow file's text; None when absent. Refuses anything but a regular file."""
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ConfigError(f"{target}: refusing to replace a symlink or non-regular file")
    try:
        return target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read {target}: {exc}") from exc


def _write_workflow_file(target: Path, text: str) -> None:
    """Replace ``target`` atomically, keeping an existing file's mode."""
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        try:
            mode: int | None = target.stat().st_mode & 0o777
        except FileNotFoundError:
            mode = None
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666 if mode is None else 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            if mode is not None:
                os.fchmod(stream.fileno(), mode)
            stream.write(text)
        os.replace(tmp, target)
    except OSError as exc:
        raise ConfigError(f"cannot write {target}: {exc}") from exc
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def _config(args: Namespace, repo: Path | None) -> int:
    from project_sandbox_tui import settings

    scope: config_mod.ConfigScope | None = getattr(args, "scope", None)
    if args.action == "list":
        if scope is None:
            data = settings.effective_values(repo, args.config, warn=_warn_config)
            _warn_untrusted(repo)
        else:
            data = settings.file_values(scope, repo, args.config)
        for key, value in settings.flatten(data):
            print(f"{key} = {settings.format_value(value)}")
        return 0

    if args.action == "get":
        key = settings.parse_key(args.key)
        _warn_untrusted(repo)
        table = settings.effective_values(repo, args.config, warn=_warn_config)
        for part in key.path[:-1]:
            table = table.get(part, {})
        if key.path[-1] not in table:
            return 1  # like git config: unset
        value = table[key.path[-1]]
        print(value if isinstance(value, str) else settings.format_value(value))
        return 0

    if scope is None:
        raise ConfigError(f"config {args.action} needs --global or --project")
    editor = settings.Editor(scope, repo, args.config, warn=_warn_config)
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


def _warn_config(message: str) -> None:
    print(f"sbx-tui: config warning: {message}", file=sys.stderr)


def _confirm(prompt: str) -> bool:
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _warn_untrusted(repo: Path | None) -> None:
    if repo is None:
        return
    trust = config_mod.ProjectTrust.check(repo)
    files, warnings = config_mod.WorkflowTrust.scan(repo)
    for warning in warnings:
        _warn_config(warning)
    for item in [trust, *files]:
        if item.needs_review:
            print(f"sbx-tui: {item.describe()} (review: sbx-tui --trust-config)", file=sys.stderr)


def _trust_config(repo: Path, global_path: Path | None) -> int:
    """Review the project config and each workflow file that changed, one by one.

    Exits 2 if a candidate is invalid, otherwise 1 if one was declined.
    """
    project = config_mod.ProjectTrust.check(repo)
    files, warnings = config_mod.WorkflowTrust.scan(repo)
    for warning in warnings:
        _warn_config(warning)
    for item in [project, *files]:
        if not item.needs_review and item.current is not None and item.current == item.trusted:
            print(f"{item.path}: trusted")
    if project.current is None:
        print(f"{project.path}: not present")
    pending = [item for item in [project, *files] if item.needs_review]
    invalid = declined = False
    shown: set[str] = set()
    for trust in pending:
        try:
            if isinstance(trust, config_mod.ProjectTrust):
                cfg = config_mod.load_candidate(trust, global_path)
            else:
                cfg = config_mod.load_workflow_candidate(trust, repo, global_path)
        except ConfigError as exc:
            print(f"sbx-tui: config error: {exc}", file=sys.stderr)
            print(f"not trusted: {trust.path}")
            invalid = True
            continue
        for warning in cfg.warnings:
            if warning not in shown:
                shown.add(warning)
                _warn_config(warning)
        print(trust.diff(), end="")
        if not _confirm(f"Trust {trust.path}? It can run commands on this machine. [y/N] "):
            print("not trusted")
            declined = True
            continue
        trust.trust(repo)
        print(f"trusted: {trust.path}")
    return 2 if invalid else 1 if declined else 0


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
    del data["profile_sources"]
    del data["warnings"]
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
            gateway=d.gateway,
        )
        print(shlex.join(build_argv(cfg, repo, spec)))
    except ConfigError as exc:
        print(f"# cannot build command: {exc}")
    return 0
