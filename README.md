# project-sandbox-tui

[![PyPI](https://img.shields.io/pypi/v/project-sandbox-tui)](https://pypi.org/project/project-sandbox-tui/)

A [vim](https://www.vim.org/)/[ranger](https://ranger.fm/)-style terminal UI for
running coding agents in [project-sandbox](https://github.com/pkrusche/project-sandbox)
containers, with one sandbox per [jj bookmark](https://docs.jj-vcs.dev/latest/bookmarks/).

> 🤖 Written largely by AI, reviewed by a human. Use at your own risk.

- Run agents interactively or in the background, with completion notifications.
- Collect [structured outcomes](docs/feedback.md), pass selected state between tasks,
  and set bounded task loops.
- Start [project workflows](docs/feedback.md#project-workflows) with interactive
  planning and follow-up agents on the same bookmark.
- Save agent, model and sandbox options as named [profiles](docs/configuration.md#agent-profiles).
- [Review changes commit by commit](docs/usage.md#diff-review), with optional
  agent summaries and shared seen marks.
- Merge or rebase tasks from separate jj workspaces with agent assistance.
- Track prompts and merges in a history graph (`O`); delete and undo with `dd` / `u`.
- Run shell commands in the foreground (`:!make`, `:test`), once in the
  repository background (`:&test`), or in each selected workspace (`:*test`).
  [Compare the command modes](docs/usage.md#choosing-how-to-run-a-shell-command),
  or map them to leader keys.
- Let project-sandbox manage isolation, credentials and workspace teardown.

```text
 sbx-tui   ~/workspace/myproject   5 bookmarks · 1 running
         bookmark          vs main    reviewed  head      subject                       status
         @ working copy ●  ↑1                   qzmvtlno  (no description set)
 ▶─╮     merge/parser-tui  ↑6 Δ9      0/9       wnrtlxqz  Merge feature/parser, spike…  merging
 ✓ │     feature/parser    ↑3 Δ5      5/5       sutnpmvo  Add streaming parser          completed
 │ ✗     spike/tui         ↑2 ↓1 Δ4   1/4       zsuskuln  Try a ranger-style layout     failed (1)
 │ │ ○   fix/flaky-tests   ↑1 Δ2      0/2       rlvkpnrz  Stabilise integration tests
 ○─┴─╯   main ●            =                    kxyrwpsn  Initial import
```

## Requirements

- Linux or macOS (WSL on Windows)
- Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/)
- [jj](https://github.com/jj-vcs/jj) ≥ 0.46 on `PATH`. For an existing Git
  repository, run `jj git init --colocate`.
- [project-sandbox](https://github.com/pkrusche/project-sandbox) ≥ 0.4.1 on `PATH`, plus
  Apple Containers, Docker or Podman.

## Getting started

```sh
uv tool install project-sandbox
uv tool install project-sandbox-tui
# For the latest version from GitHub:
# uv tool install git+https://github.com/pkrusche/project-sandbox-tui
# From a checkout: uv tool install . (or uv sync && uv run sbx-tui)

cd ~/workspace/myproject
sbx-tui --init      # detect the project type, write config and install setup/reporting guidance
sbx-tui doctor      # check dependencies, runtime and configuration
sbx-tui --dry-run   # preview project-sandbox calls
sbx-tui             # launch
```

For guided setup, run `sbx-setup-wizard` in your jj checkout. Choose an installed
host agent (Claude, Codex, Pi, GitHub Copilot or OpenCode) to conduct the interview,
then select agents for sandbox profiles. The wizard lists agents supported by your
configured project-sandbox and explains how each can authenticate on this host;
they do not all need to be installed locally. Select **Use the agent gateway**
to offer Pi and OpenCode through `network.agent_proxy`. Add any instructions and
press **Start interview** (`ctrl+s`) to open the setup agent in the current
terminal. It receives discovered agents, VCS access modes and runtimes, and uses
the `sbx-tui-safety` skill to discuss each task's access needs. Run the wizard
before or after `sbx-tui --init`.

Press `?` for full help.

Highlight `@` or a feature bookmark and press `i` for an interactive sandbox
session or `r` to run or queue a background prompt (`L` manages the queue).
With no saved profile or prior choice, the form opens on the implementer persona.
Choose a provider and persona, a saved profile (`:config-profiles`), or **custom**.
Set `security.level` for project-wide isolation and review the level on each launch.

See [initialization](docs/configuration.md#initialization) for project detection
and the `sbx-tui-config` and `sbx-tui-safety` skills. Existing config and skill
files are preserved. Bookmark workspaces get ignored copies, along with any
[workspace skills](docs/configuration.md#workspace-skills) you configure.

## Documentation

- [Usage and keys](docs/usage.md)
- [Worked example: foreground and background commands](docs/command-workflow.md)
- [Sessions, terminals and notifications](docs/sessions.md)
- [Merging and rebasing](docs/merging.md)
- [Workspace cleanup](docs/cleanup.md)
- [Configuration](docs/configuration.md)
- [Model suggestions](docs/models.md)
- [Run feedback and workflows](docs/feedback.md)
- [Architecture](docs/architecture.md)
- [Development](docs/development.md)

## License

[MIT](LICENSE)
