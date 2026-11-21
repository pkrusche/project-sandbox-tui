"""Interview a project owner about sbx-tui setup using an interactive host agent.

Two agent choices are kept apart: the **setup agent** runs the interview on the
host and must be on ``PATH``; **profile agents** run in the sandbox and come from
what the configured project-sandbox supports and how each can run on this host
(capabilities.py), so one setup can combine agents from different sources.
"""

import argparse
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Checkbox, Label, Select, SelectionList, TextArea

from project_sandbox_tui import capabilities
from project_sandbox_tui.capabilities import Capabilities
from project_sandbox_tui.config import SAFETY_SKILL, ConfigError, load
from project_sandbox_tui.initialization import skill_template
from project_sandbox_tui.vcs import VcsError, detect

HOST_AGENTS = {
    "claude": "Claude",
    "codex": "Codex",
    "pi": "Pi",
    "copilot": "GitHub Copilot",
    "opencode": "OpenCode",
}


def agent_label(agent: str) -> str:
    return HOST_AGENTS.get(agent, "Bash" if agent == "bash" else agent)


@dataclass
class Interview:
    host: str
    profiles: list[str]
    instructions: str
    gateway: bool = False
    summary: str = ""  # discovered capabilities for the prompt (capabilities.setup_summary)
    sources: dict[str, str] = field(default_factory=dict)  # profile agent -> its sources

    def prompt(self, available: list[str]) -> str:
        chosen = ", ".join(
            f"{agent} ({self.sources[agent]})" if agent in self.sources else agent
            for agent in self.profiles
        )
        gateway = (
            "I want to use the agent gateway for some profiles: follow the sbx-tui-config "
            "skill's agent gateway section to list its models and set gateway = true."
            if self.gateway
            else "I do not plan to use the agent gateway."
        )
        return f"""Help me set up this project for sbx-tui. You are the interactive setup agent.
You run on the host; the agent profiles you configure run inside the sandbox.
Detected host agents: {", ".join(available)}. Interview agent: {self.host}.
Agents chosen for sandbox profiles: {chosen or "discuss during the interview"}.
{gateway}

What the configured project-sandbox supports here:
{self.summary or "unknown; run sbx-tui doctor to see it."}

Inspect the project instructions, manifests and existing .project-sandbox-tui.toml.
Ask me one question at a time and wait for my answers. Ask which tasks I run, then
suggest named agent profiles for those tasks. For each profile ask what VCS access
and internet egress it needs, including required domains, and map the choices to
profile extra_args using only the modes listed above. Ask how I plan, implement,
test and review changes, then suggest actions and workflow steps grounded in the
project's own commands.

Use the bundled safety skill below to discuss the scenarios that apply. It is
included here so you can use it before this project has been initialized.

{skill_template(SAFETY_SKILL)}

Summarize the proposed project configuration for my review. Once we agree, apply it,
preserving existing settings. Use sbx-tui --init if initialization is needed and
consult the sbx-tui-config skill it installs. Validate with sbx-tui config list and
sbx-tui doctor, and explain any configuration trust review needed.

Additional instructions from me:
{self.instructions or "(none)"}"""


class SetupDialog(App[Interview]):
    TITLE = "Project setup"
    CSS = """
    Screen { align: center middle; }
    VerticalScroll {
        width: 72; max-width: 100%; height: auto; max-height: 100%; padding: 1 2;
        border: round $primary;
    }
    SelectionList { height: auto; max-height: 8; }
    TextArea { height: 4; }
    Horizontal { height: auto; }
    .hint { color: $text-muted; }
    """
    BINDINGS = [("escape", "quit", "Cancel"), ("ctrl+s", "start_interview", "Start interview")]

    def __init__(
        self,
        agents: list[str],
        caps: Capabilities | None = None,
        *,
        gateway_configured: bool = False,
        home: Path | None = None,
    ) -> None:
        """``agents``: setup agents found on PATH; ``caps``: the launcher's capabilities."""
        super().__init__()
        self.agents = agents
        self.caps = caps or capabilities.fallback()
        self.gateway_configured = gateway_configured
        self.home = home

    def profile_choices(self, gateway: bool) -> list[tuple[str, str]]:
        """(agent, sources) for agents a sandbox profile can use."""
        return [
            (agent, result.describe())
            for agent, result in capabilities.profile_agents(
                self.caps, gateway=gateway, home=self.home
            )
        ]

    def _selection(self, gateway: bool, selected: set[str] | None = None) -> list:
        return [
            (
                f"{agent_label(agent)} — {sources}",
                agent,
                agent in selected if selected is not None else True,
            )
            for agent, sources in self.profile_choices(gateway)
        ]

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("Setup agent (runs the interview on this host)")
            yield Select[str](
                [(HOST_AGENTS[a], a) for a in self.agents],
                value=self.agents[0],
                allow_blank=False,
            )
            yield Checkbox(
                "Use the agent gateway for pi / opencode / bash profiles",
                value=self.gateway_configured,
                id="gateway",
            )
            yield Label("Sandbox profile agents to consider (Space to toggle)")
            yield SelectionList[str](*self._selection(self.gateway_configured), id="profiles")
            if not self.caps.discovered:
                yield Label(
                    "project-sandbox could not be probed: built-in agent list shown",
                    classes="hint",
                )
            yield Label("Additional instructions (optional)")
            yield TextArea(tab_behavior="focus")
            with Horizontal():
                yield Button("Start interview (Ctrl+S)", id="start", variant="primary")
                yield Button("Cancel", id="cancel")

    @on(Checkbox.Changed, "#gateway")
    def _gateway_changed(self, event: Checkbox.Changed) -> None:
        profiles = self.query_one("#profiles", SelectionList)
        selected = set(profiles.selected)
        known = {value for _label, value, _on in self._selection(not event.value)}
        options = self._selection(event.value, selected)
        # Agents that only became available keep the default of being selected.
        options = [(label, value, chosen or value not in known) for label, value, chosen in options]
        profiles.clear_options()
        profiles.add_options(options)

    @on(Button.Pressed, "#start")
    def action_start_interview(self) -> None:
        gateway = self.query_one("#gateway", Checkbox).value
        self.exit(
            Interview(
                str(self.query_one(Select).value),
                list(self.query_one("#profiles", SelectionList).selected),
                self.query_one(TextArea).text,
                gateway=gateway,
                summary=capabilities.setup_summary(self.caps, gateway=gateway, home=self.home),
                sources=dict(self.profile_choices(gateway)),
            )
        )

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.exit()


def discover(repo: Path) -> tuple[Capabilities, bool]:
    """The launcher's capabilities for ``repo`` and whether a gateway is configured."""
    try:
        cfg = load(repo)
        command = cfg.sandbox_command()
    except ConfigError:
        return capabilities.fallback(), False
    return capabilities.refresh(command, repo), bool(cfg.network.agent_proxy)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-R", "--repo", type=Path, default=Path.cwd(), help="project directory")
    args = parser.parse_args(argv)
    target = args.repo.expanduser()
    if not target.is_dir():
        parser.error(f"{target}: no such directory")
    try:
        repo = detect(target).root
    except VcsError as exc:
        parser.error(str(exc))
    paths = {name: Path(path).absolute() for name in HOST_AGENTS if (path := shutil.which(name))}
    if not paths:
        parser.error(f"install a supported host agent on PATH: {', '.join(HOST_AGENTS)}")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        parser.error("the setup interview requires an interactive terminal")
    caps, gateway_configured = discover(repo)
    interview = SetupDialog(list(paths), caps, gateway_configured=gateway_configured).run()
    if interview is None:
        return 0
    executable = paths[interview.host]
    flags = {"copilot": ["-i"], "opencode": ["--prompt"]}.get(interview.host, [])
    try:
        os.chdir(repo)
        os.execv(executable, [str(executable), *flags, interview.prompt(list(paths))])
    except OSError as exc:
        print(
            f"sbx-setup-wizard: could not start {interview.host} in {repo}: {exc}", file=sys.stderr
        )
        return 1
