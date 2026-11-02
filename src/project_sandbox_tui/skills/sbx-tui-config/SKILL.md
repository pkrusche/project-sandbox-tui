---
name: sbx-tui-config
description: Set up or edit sbx-tui configuration in a project using project-sandbox-tui, including container image selection, launch defaults, commands, and agent actions.
---

# Configure sbx-tui

Work from the project's jj repository root. Read `.project-sandbox-tui.toml`,
the project manifests, Dockerfile, and development instructions before choosing
settings. Preserve the project's existing choices unless the task changes them.

## Inspect and edit settings

`sbx-tui --init` creates missing global and project config files and installs
this skill. It preserves existing files. Image detection only runs when creating
the project config; it is not applied each time the TUI starts.

```sh
sbx-tui config path --global
sbx-tui config list --global
sbx-tui config list --project
sbx-tui config list
```

The project file is merged over the global file; omitted keys inherit global
values. Unknown keys are errors. Use project scope for repository requirements;
use global scope for personal defaults only when that is part of the request.
Remembered launch choices can override configured agent, model, effort, timeout,
and fast mode. Model and fast-mode choices are remembered separately per agent.
`defaults.fast = true` enables fast mode for Codex and Claude and requires
project-sandbox 0.3.1 or later; other agents ignore it.

Prefer `sbx-tui config set --project KEY VALUE` for individual changes: it
preserves comments, validates the merged config, and keeps trusted files trusted.
Use `-R PATH` on config subcommands when outside the project.

```sh
sbx-tui config set --project defaults.agent codex
sbx-tui config set --project defaults.timeout 1800
sbx-tui config set --project network.extra_domains '["registry.example.org"]'
```

## Choose one image source

Set exactly one nonempty `project.base_image`, `project.dockerfile`, or
`project.build`. Initialization checks root-level files in this order:

| Evidence | Starter settings |
| --- | --- |
| `Dockerfile` | `dockerfile = "Dockerfile"`, `docker_context = "."` |
| `pyproject.toml` or `uv.lock` | `build = "python-uv"` |
| `Cargo.toml` | `build = "rust-cargo"` |
| `package.json` | `build = "node-npm"` |
| None of these | `base_image = "python:3.12-slim"` |

Detection is a starting point. Check whether the Dockerfile provides a useful
development environment and whether the generated build fits the actual package
manager and monorepo layout. Set nested Dockerfile paths manually; relative paths
resolve from the repository root. `docker_context` requires `dockerfile`.
`build_version` is supported only for `python-uv` and `rust-cargo`, not Node.
Read version requirements from the project rather than guessing them.

When switching sources, clear incompatible values before setting the new source.
Each CLI edit validates the entire merged config. For example, when switching a
base image to a Python build:

```sh
sbx-tui config set --project project.base_image ''
sbx-tui config set --project project.build python-uv
```

Clear `project.docker_context` before clearing `project.dockerfile`; clear
`project.build_version` before moving away from Python or Rust builds. An empty
string overrides an inherited global setting; `config unset` would expose it
again. If the config is already invalid, edit the related TOML keys together.

## Add project workflows

Derive test and lint commands from project instructions and scripts.
`[commands]` runs shell commands on the host in the repository (`:test`), or in
selected workspaces (`:*test`). `[actions.NAME]` starts an agent prompt in the
sandbox on each target bookmark. Command and action names must be distinct and
cannot shadow built-ins such as `help`, `quit`, `refresh`, or `reload`.

```toml
[commands]
test = "scripts/test.sh"

[actions.review]
prompt = "Review {bookmark} against {main} and report issues."
effort = "high"

[leader]
t = "test"
r = "review"
```

Adapt the example to the project. Actions require a prompt and may override
`agent`, `model`, `effort`, and `timeout`. Only `{bookmark}` and `{main}` are
substituted in action prompts. Leader mappings name existing command lines;
create the command/action before its mapping.
Actions use the selected agent's remembered fast-mode choice, falling back to
`defaults.fast`; `fast` is not an action-specific setting.

For host commands, use quoted environment variables such as `"$SBX_WORKSPACE"`.
`ui.host_agent_command` customizes `I`, the interactive host agent shortcut,
until a successful `:host AGENT [ARGS...]` remembers a command for that repository.
`I` then reuses the full remembered command, independently of sandbox defaults.
It defaults to the last-used agent executable, runs in the selected workspace,
and accepts `{agent}`, `{path}` and `{branch}`. `ui.terminal` controls where it opens.
For `ui.editor_command`, `ui.host_agent_command`, `ui.terminal_command`, and `ui.notify_command`,
placeholders must be whole arguments. A custom terminal needs
`ui.terminal = "custom"` and a command such as `wezterm start -- sh -c {cmd}`.
Edit these dependent settings together if setting either alone fails validation.

## Validate and reload

Run `sbx-tui config list` and `sbx-tui --print-config` to inspect effective
settings and the generated sandbox command. `sbx-tui doctor` checks dependencies,
runtime and configuration; it does not prove a container can build the project.

Direct TOML edits require review: until trusted, a changed file uses its last
trusted copy, and a never-trusted file is ignored. Explain this if inspection
shows old values. Have the user review with `sbx-tui --trust-config` or the TUI's
reload prompt; do not bypass review by modifying trust state. `ctrl+l` reloads
the TUI; running sessions retain their original settings.

Report the chosen image source, settings changed, validation performed, and any
remaining review or runtime issue. This skill is local and gitignored; separate
checkouts/workspaces need their own `sbx-tui --init` to install it.
