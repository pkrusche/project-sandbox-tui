---
name: sbx-tui-config
description: Configure project-sandbox-tui container images, launch defaults, shared sandbox settings, agent profiles with per-launch project-sandbox flags, commands, agent actions, and the agent profile used by each action and workflow step.
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
- `vcs access`: the `--vcs-access` modes for profile `extra_args` (for example
  `--vcs-access=read-only` for a review profile);
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

`[security] level` sets the project's default isolation on a 1–5 scale; a
higher level isolates more. sbx-tui compiles it to launcher flags:

| Level | Flags | Use for |
| --- | --- | --- |
| 1 | `--no-firewall` | jobs that need open internet; avoid as a default |
| 2 | `--egress=firewall` plus `--extra-domain` for `project.build`'s package sources and `network.extra_domains` (and `--allow-github` with `network.allow_github`) | implementing and testing with dependency downloads |
| 3 | the egress backend only: agent API hosts | planning and work without downloads |
| 4 | level 3 plus `--vcs-access=read-only` | reviews that need history |
| 5 | level 3 plus `--vcs-access=hidden` | source-only reviews |

Levels 3 to 5 use `security.egress` from the global config: `sidecar` (default,
needs `project-sandbox-egress` set up) or `firewall`. sbx-tui never switches
backends. With `network.internet_proxy` set, level 2 adds no `--extra-domain`:
the proxy's policy decides. Levels 4 and 5 keep source files writable. With a
level in effect, `--no-firewall`, `--egress`, `--extra-domain`, `--allow-github`,
`--vcs-access`, `--pi-ollama` and proxy or CA flags in `sandbox.extra_args`,
profile or custom arguments are refused; `network.no_firewall` is ignored; and
`network.internet_proxy`, `ca_certs`, `build_proxy`, `build_no_proxy`,
`build_ca_certs`, `no_internet_proxy_preflight` and `security.egress` may only be
set in the global config. Without `[security]`, launches keep their earlier flags.

Provider rows pick the agent and model IDs: `codex`, `claude-code`,
`gateway-openai`, `gateway-anthropic`, `gateway-gemini` (through
`network.agent_proxy` with `catalogue.gateway_agent`, `pi` or `opencode`),
`copilot-openai`, `copilot-anthropic` and `copilot-gemini`. Set
`catalogue.default_provider` to one that `sbx-tui doctor` lists as available.
Personas choose tier, effort, fast mode and starting level:

| Persona | Tier | Effort | Fast | Level |
| --- | --- | --- | --- | --- |
| planner | high | high | off | 3 |
| checker | medium | low | on | 2 |
| implementer | medium | medium | off | 2 |
| debugger | frontier | xhigh | off | 2 |
| chore-runner | fast | medium | on | 2 |
| reviewer | high | high | off | 4 |

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
for `--memory` and accumulates repeatable flags like `--extra-domain`. Omit a
domain from the shared allowlist if only one profile should add it. Upstream
rejects `--extra-domain` when using an Internet proxy or sidecar egress.
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

Each action gets its agent settings in one of four ways. Pick one per action:

| Action sets | Agent, model, effort, fast | Extra CLI args |
| --- | --- | --- |
| `persona = "NAME"` | the persona on the remembered or default provider | none |
| `profile = "NAME"` | from `[profiles.NAME]` | the profile's `extra_args` |
| `agent`, `model` or `effort` | those fields; unset ones from the launch defaults | none |
| neither | the profile the `i`/`r` launch forms would select | that profile's, or the remembered Custom args |

`profile` cannot be combined with `agent`, `model`, `effort` or `persona`, and
`persona` cannot be combined with `agent`, `model` or `effort`; config
validation rejects it, as it does the reserved name `custom` and unknown
personas. `timeout` applies in every case. `level = 1..5` sets the action's
security level, else the persona's, else the project's; a run below the
project default asks for confirmation. Prefer personas for unattended work:
workflows then move to another provider without config edits. In the third row, the launch forms select the remembered profile,
or the first configured profile before the user has chosen one. Prefer an
explicit `profile` for unattended work so its setup doesn't depend on the last
interactive launch. Use a profile to set `fast` or extra CLI flags for an action.

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
a setup field for each agent action. Each field starts at the action's persona
(with a provider field), then its profile, then **action settings** for an
action with its own fields, then the launch forms' selection. With a security
level in effect each step also has a level field, red below the project default.
The user can change any field for that run; choices are not remembered, so put
lasting choices in the action. Steps keep the settings chosen at start even if
profiles or the catalogue change later.

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

For a workflow intended to be shared, use a separately trusted
`.project-sandbox-tui/workflows/NAME.toml` with `[workflow]`, `[actions.SHORT]`
and optional `[commands]`. Use short references within the file and
`NAME/SHORT` elsewhere. Bundled actions use personas rather than profiles or
agent settings. Keep commands and prompts portable. Prefer inline workflows
for project-specific dependencies; review workflow files independently with
`sbx-tui --trust-config`. See the workflow file example in the configuration
documentation.
