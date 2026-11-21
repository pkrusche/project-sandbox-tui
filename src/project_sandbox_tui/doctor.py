"""Dependency checks and diagnostics for unavailable comparisons to main."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Literal

from project_sandbox_tui import initialization, launchers, notify
from project_sandbox_tui.config import Config, ConfigError, ProjectTrust, _load, load
from project_sandbox_tui.sandbox import runtime_cli
from project_sandbox_tui.vcs import Branch, VcsError, detect, main_branch

# The jj release CI tests against; older ones may lack template keywords or flags.
TESTED_JJ = (0, 46, 0)
# --node-npm (project.build = "node-npm") first shipped in project-sandbox 0.2.2.
MIN_PROJECT_SANDBOX = (0, 2, 2)

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
Runner = Callable[[list[str], float], tuple[int, str]]


def run(argv: list[str], timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


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
    cli = runtime_cli(requested)
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


def check_feedback(cfg: Config, repo: Path | None, runner: Runner) -> list[Check]:
    if not cfg.feedback.enabled:
        return [Check("run feedback", "ok", "disabled")]
    required = cfg.feedback.requires_reporting or bool(cfg.workflows)
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
    checks += [check_project_sandbox(cfg, runner), check_runtime(cfg, runner, which)]
    checks += check_config(cfg, repo)
    checks += check_feedback(cfg, repo, runner)
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
