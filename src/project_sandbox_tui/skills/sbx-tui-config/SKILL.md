---
name: sbx-tui-config
description: Configure sbx-tui images, sandbox settings, security levels, providers, personas, profiles, commands, actions and workflow steps.
---

# Configure sbx-tui

Work from the project's jj repository root. Read `.project-sandbox-tui.toml`,
the project manifests, Dockerfile, and development instructions before choosing
settings. Preserve the project's existing choices unless the task changes them.
For decisions about VCS access, egress, mounts, credentials or unattended
workflow steps, consult the companion `sbx-tui-safety` skill.

## Inspect and edit settings

`sbx-tui --init` creates missing global and project config files and installs
this skill in the checkout. It preserves existing files. Image detection only
runs when creating the project config; it is not applied each time the TUI starts.

```sh
sbx-tui config path --global
sbx-tui config list --global
sbx-tui config list --project
sbx-tui config list
```

The project file is merged over the global file. Omitted keys inherit global
values. Unknown project keys are ignored with a warning. Unknown global keys
are errors. Use project scope for repository requirements. Use global scope for
personal defaults only when that is part of the request.
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
`project.build`. See the [image detection order](https://github.com/pkrusche/project-sandbox-tui/blob/main/docs/configuration.md#initialization).

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

## Configure sandbox settings and profiles

Use `[sandbox]` and `[network]` for shared settings such as runtime, memory,
CPUs, proxies, and extra domains. Use `sandbox.extra_args` for shared CLI flags
without a dedicated setting. With a project security level (below), network
and VCS flags come only from the level: keep them out of every `extra_args`. Check the version and `--help` of the command in
`sandbox.command` before adding flags. For runtime support, build options, and
network restrictions, see the [project-sandbox usage guide](https://github.com/pkrusche/project-sandbox/blob/main/docs/usage.md),
[runtimes](https://github.com/pkrusche/project-sandbox/blob/main/docs/runtimes.md),
and [security guide](https://github.com/pkrusche/project-sandbox/blob/main/docs/security.md).

### Check what the sandbox supports

Profile agents run inside the sandbox. They are not the agent you are running as
during setup, which runs on the host. A profile may name any agent the configured
launcher supports, even one that is not installed on the host. Run
`sbx-tui doctor` to see:

- `agent NAME`: each agent `sandbox.command` supports and how it can run here:
  `host credentials` (forwarded from the host), `token login` (copilot),
  `no credentials needed` (bash), or `unavailable` with the reason;
- `vcs access` lists launcher modes used by security levels 4 and 5.
- `runtimes`: which isolation runtimes are usable and what `auto` selects;
- `providers`: each provider row and whether it can run here;
- `egress backend`: `security.egress`, whether the launcher supports it and, for
  the sidecar, the hosts its global `egress.toml` allows.

When doctor warns that capabilities could not be discovered, read the launcher's
`--help` yourself. Combine sources freely, for example Claude with host
credentials for implementation and Pi through the agent gateway for cheap checks.
An unavailable agent can still run with `--no-forward-credentials` in its
profile's `extra_args`: the user logs in inside the sandbox each time.

### Choose a security level, providers and personas

Choose `security.level` for the project's normal work. Use level 2 for dependency
downloads, level 3 for API-only egress, or levels 4–5 for restricted VCS access.
See the [security settings and managed flags](https://github.com/pkrusche/project-sandbox-tui/blob/main/docs/configuration.md#security-levels).

Use `sbx-tui doctor` to select an available `catalogue.default_provider`. The
[provider and persona catalogue](https://github.com/pkrusche/project-sandbox-tui/blob/main/docs/models.md) lists model tiers, effort,
fast mode and starting level.

`[personas.NAME]` overrides fields of a built-in persona or adds one (`tier` is
then required). `[catalogue.models.<vendor|row>] TIER = "model"` pins a model;
an empty value disables that tier. A provider without the tier is shown as
unavailable, never replaced by another model. Copilot rows imply GitHub hosts.

```toml
[security]
level = 3

[catalogue]
default_provider = "claude-code"
```

### Use the agent gateway

Only `pi`, `opencode` and `bash` can use the gateway. Set the gateway once in
`[network]`, then opt profiles in with `gateway = true`; other profiles never
use it:

```sh
sbx-tui config set --global network.agent_proxy http://127.0.0.1:4000/v1
sbx-tui config set --project profiles.pi-gateway.agent pi
sbx-tui config set --project profiles.pi-gateway.gateway true
```

A gateway profile needs a model the gateway serves. List them from the host
before proposing models. Read the key the way project-sandbox does
(`pass show agentgateway-api-key`, else the variable named by
`network.agent_proxy_key_env`, default `AGENTGATEWAY_API_KEY`). Never print the
key or pass it as a command-line argument; send it from a private header file:

```sh
hdr=$(mktemp) && chmod 600 "$hdr"
key=$(pass show agentgateway-api-key 2>/dev/null | head -n1)
[ -n "$key" ] || key=${AGENTGATEWAY_API_KEY:-}
printf 'Authorization: Bearer %s\n' "$key" > "$hdr"; unset key
curl -sS -H @"$hdr" "<agent_proxy>/models" | python3 -c \
  'import json,sys; print("\n".join(m["id"] for m in json.load(sys.stdin)["data"]))'
rm -f "$hdr"
```

Here `<agent_proxy>` is the configured `network.agent_proxy` (it ends in `/v1`).
Propose models from that list and save the chosen plain IDs as suggestions, then
set the profile's model:

```sh
sbx-tui config set --project models.pi '["gpt-5-mini", "claude-sonnet-5-5"]'
sbx-tui config set --project profiles.pi-gateway.model gpt-5-mini
```

Use plain IDs for OpenCode too: sbx-tui adds the `agent-proxy/` provider prefix
that OpenCode needs. If the gateway cannot be reached, tell the user, ask for
model IDs by hand, and still never print the key.

### Save profiles

Use `[profiles.NAME]` to save `agent`, `model`, `effort`, `fast`, `gateway`, and
profile-only CLI flags. After global and project fields are merged, omitted profile fields use
built-in profile defaults. A project `extra_args` array replaces the global array
for that same profile. Set the prompt and timeout in the background launch form.

A browser-testing profile can raise the memory limit and the size of `/dev/shm`.

```toml
[profiles.browser-tests]
agent = "codex"
extra_args = ["--memory=12g", "--shm-size=4g"]
```

Use `:config-profiles` to create or edit a project profile, enter
`--memory=12g --shm-size=4g` in **CLI args**, and save with `ctrl+s`. The CLI
can update the same argument array on an existing profile.

```sh
sbx-tui config set --project profiles.browser-tests.extra_args '["--memory=12g", "--shm-size=4g"]'
```

Each TOML array element is one argument. In the TUI, shell quoting groups words
and shell expressions are passed literally. The TUI appends profile arguments
after its shared flags and `sandbox.extra_args`. Upstream uses the last value
for `--memory`.
Keep the image source in `[project]` and use the profile fields for agent
selection. Leave branch, prompt, log, and summary flags to the TUI.

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

Adapt the example to the project. Actions require a prompt and may set a
`timeout`. Only `{bookmark}` and `{main}` are substituted in action prompts.
Leader mappings name existing command lines; create the command/action before
its mapping.

### Choose agent profiles for actions and workflow steps

Prefer an explicit persona for unattended work that should run across providers.
Choose a named profile when an action needs its own launcher arguments.
With no explicit setup, an action uses the repository's remembered selection.
See [action fields and precedence](https://github.com/pkrusche/project-sandbox-tui/blob/main/docs/configuration.md#agent-actions).

Workflows run agent actions, human review stages, and host commands on a bookmark.
Assign each agent step's launch settings through its action:

```toml
[profiles.deep]
agent = "codex"
effort = "high"
extra_args = ["--memory=16g"]

[actions.spec-plan]
prompt = "Plan {bookmark}; report state.spec_path."
persona = "planner"

[actions.spec-check]
prompt = "Check the spec at previous_state.spec_path."
persona = "checker"

[actions.spec-implement]
prompt = "Implement the spec at previous_state.spec_path."
persona = "implementer"
timeout = 3600

[workflows.spec]
planning_action = "spec-plan"
steps = ["spec-check", "spec-implement", "host:test"]
pass_state_keys = ["spec_path"]
```

That example is an inline, project-specific workflow, so it can use the full
action settings described above. For a workflow intended to travel between
projects, put it in `.project-sandbox-tui/workflows/NAME.toml` with a
`[workflow]` table, actions and any host commands it needs. Inside that file,
use short action and command names; outside it, names are `NAME/SHORT` (for
example `:spec/implement` or `:&spec/test`). File-based workflow actions select
their agent with `persona` only. Keep shared prompts and commands portable, and
use `sbx-tui --trust-config` to review each workflow file independently. Use
`sbx-tui workflow import SOURCE [--name NAME]` to import one from HTTPS or a
local path.

Workflows require `feedback.enabled` (the default), and every referenced action
must exist. Before anything starts, `:workflow NAME [TOPIC]` shows a picker with
a setup field for each agent action. Changes apply to this run. Save lasting
choices in the action.

`host:test` runs `test` from `[commands]`. Config loading verifies the command
name and inserts `human-review` immediately before it unless that stage already
appears there. Add `human-review` anywhere the workflow needs to wait for review
of the entire bookmark diff. In `L`, press `enter` on the review stage to mark
files, or `X` to terminate the run. Host commands start in a fresh jj workspace
of the reviewed revision. Use `"$SBX_WORKSPACE"` for the checkout path.

When a named profile is missing, sbx-tui asks before a direct action uses the
fallback profile. The workflow picker lets the owner choose a replacement for
each affected step. `sbx-tui doctor` lists missing references under `action
profiles`. Fix the name before relying on the action for unattended work.

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
settings and the default working-copy command. To verify a named profile, open
`i` or `r` on the intended bookmark, select the profile, and inspect the command
preview before submitting. Use `esc` to close the form after inspection.
To check workflow step profiles without launching anything, run `sbx-tui
--dry-run`, select a bookmark, run `:workflow NAME`, and press `enter` in the
picker: the preview lists each step's persona and provider or profile, its agent
settings, and its level with the flags it compiles to.
`sbx-tui doctor` checks dependencies, runtime, and configuration, including
actions that name unknown profiles.

Direct TOML edits require review: until trusted, a changed file uses its last
trusted copy, and a never-trusted file is ignored. Explain this if inspection
shows old values. Have the user review with `sbx-tui --trust-config` or the TUI's
reload prompt; do not bypass review by modifying trust state. `ctrl+l` reloads
the TUI; running sessions retain their original settings.

Report the chosen image source, settings changed, validation performed, and any
remaining review or runtime issue.

## Where this skill comes from

This skill is a local, ignored copy at `.agents/skills/sbx-tui-config/` and
`.claude/skills/sbx-tui-config/`. `sbx-tui --init` installs it in the checkout
and adds both directories to the root `.gitignore`. sbx-tui also copies it into
each bookmark workspace when it opens the workspace or launches an agent there
(`I`, `i`, `r`, actions, and `o` while idle), and keeps it out of jj snapshots
through in-tree ignore rules or the repository's local `info/exclude`. Existing
copies, including local edits, are kept: to refresh one, remove its directory
while the workspace is idle and reopen it. `project.deploy_config_skill = false`
stops automatic copies; `[[workspace_skills]]` deploys additional skills.
