# Configuration

Settings are deep-merged from global
`$XDG_CONFIG_HOME/project-sandbox-tui/config.toml` (default `~/.config/…`) and
trusted `<repo>/.project-sandbox-tui.toml`. Unknown project keys are ignored with
a warning. When the trusted config contains unknown items, `sbx-tui` lists them
and asks for confirmation before opening the TUI. Unknown global keys and
invalid values for recognized settings are errors.

## Initialization

Run `sbx-tui --init` from the repository or a subdirectory (or pass a repository
path). It writes missing global and project config files, trusts only the project
config it creates, and downloads the model catalog if missing. Existing files
and their trust status are preserved.

Inside the TUI, `:init` performs the same setup for the open repository and
the selected global config file. It shows the results and reloads configuration
so newly created settings take effect without restarting.

For a new project config, it checks files at the repository root in this order:

| File | Image source |
| --- | --- |
| `Dockerfile` | `project.dockerfile = "Dockerfile"`, `project.docker_context = "."` |
| `pyproject.toml` or `uv.lock` | `project.build = "python-uv"` |
| `Cargo.toml` | `project.build = "rust-cargo"` |
| `package.json` | `project.build = "node-npm"` |
| No match | `project.base_image = "python:3.12-slim"` |

The first match wins in mixed projects. Detection does not search subdirectories
or infer language versions. Review the starter settings for monorepos, alternate
package managers, or Dockerfiles intended only for production. Rerunning
`--init` does not redetect or change an existing project config.
The template clears unused image settings so inherited global image sources or
build versions cannot conflict with the detected choice.

Initialization installs two configuration skills under both `.agents/skills/`
and `.claude/skills/`. Each skill has a `SKILL.md` that other agents can also
read directly. If either `skills` path is a symlink or a file, initialization
reports it and installs the skills in the other directory. At least one of
these paths must be available as a directory.

- `sbx-tui-config` covers image selection, configuration scopes, commands,
  actions, validation and trust handling.
- `sbx-tui-safety` helps choose access for reviews, implementation, host tasks
  and unattended workflows. The setup wizard includes the skill in the host
  interview even before initialization.

Initialization also
installs `sbx-run-feedback` alongside it and adds conditional reporting guidance
to `AGENTS.md`. The configuration and reporting skill directories are added to
the root `.gitignore`; other skills and the project
config remain eligible for version control. Existing skill files are preserved.
To refresh a bundled skill, remove its installed `SKILL.md` and rerun `--init`.
Reporting guidance is delivered at launch as described in [agent feedback](feedback.md).
Bookmark workspaces get their own configuration skills automatically. See
[workspace skills](#workspace-skills).

## Workspace skills

Bookmark workspaces are separate checkouts, so ignored files such as local
skills are not in them. sbx-tui copies skill directories into a bookmark's
workspace on first use: before `I` / `:host`, `i`, `r` and agent actions start
(queued prompts when dispatched), and before `o` opens an idle workspace. Each
workspace gets a complete copy, including supporting files, so agents can
read and run them using the same relative paths as in the source. Sandboxes see
the copies under `/workspace`. `@` uses the checkout's own files; `:*` tasks do
not deploy skills.

**Bundled skills.** Both configuration skills are
copied under `.agents/skills/` and `.claude/skills/`,
including in projects that never ran `--init`. Where the bookmark's in-tree
`.gitignore` files do not already ignore these directories, sbx-tui appends
one exclusion per skill directory to
the repository's local Git excludes (`info/exclude` in the directory printed by
`jj git root`). These apply to every workspace of the repository and keep the
copies out of jj snapshots without changing a tracked file. A target that is
tracked, re-included by an in-tree `!` rule, or otherwise unsafe is left alone
and reported; the launch continues. Set `project.deploy_config_skill = false`
to stop future copies; existing copies and exclude rules stay, so they cannot
become eligible for tracking.

**Additional skills.** Add top-level `[[workspace_skills]]` tables to the global
config or the trusted project config (review with `sbx-tui --trust-config`):

```toml
[[workspace_skills]]
source = "~/private-skills/release-review"
targets = [".agents/skills/release-review", ".claude/skills/release-review"]
```

`source` is one skill directory with a regular `SKILL.md` and optional
supporting files; `~` and absolute paths are host paths, relative paths start
at the checkout that opened the TUI. Each target is one directory directly in
`.agents/skills/` or `.claude/skills/`, and may appear only once, including the
bundled targets while `deploy_config_skill` is enabled. A project
`workspace_skills` list replaces the global list, like other arrays.

The bookmark itself must ignore each full target directory. For the example,
add these rules to the repository's `.gitignore` and include that change in the
revisions that need the skill:

```gitignore
/.agents/skills/release-review/
/.claude/skills/release-review/
```

Ignore rules are evaluated in the destination workspace with Git's
`check-ignore --no-index` against disposable metadata, so only in-tree
`.gitignore` files count: global excludes and `info/exclude` exist only on the
host. When the sandbox sends the workspace as an image build context, its
`.dockerignore` also needs matching exclusions to keep the copies out of the
image.

**Rules.** Before copying, sbx-tui rechecks under the launch lock that the
workspace is idle, then validates every source and target. Symlinks and special
files in a skill, symlinked destination directories, tracked entries at or below
a target, and an existing target without a regular `SKILL.md` are errors that
name the bookmark and path. Each skill is staged in the workspace's `.jj`
directory and moved into place with one rename, keeping executable bits. A
failure in a configured skill stops the launch (a queued prompt pauses its
queue); fix it and retry. Directories that were published stay complete and are
reused. All sandbox launches reserve the selected bookmark. The launcher refuses
`--branch` and `--worktree-dir` in shared or per-run extra arguments because
they can redirect the run outside that reservation. Use the bookmark selector
and `project.worktree_dir` to set the destination. Dry runs
and the launch form preview list the planned destinations without creating a
workspace or copying.

Copies are made once. An existing valid copy, including local edits, is kept; to
refresh it, remove that skill directory while the workspace is idle and reopen
or relaunch. Removing a `[[workspace_skills]]` entry stops future copies.
[Workspace cleanup](cleanup.md) treats copies like other ignored files, and a
recreated workspace gets fresh copies. A failed preparation can leave a newly
created workspace in place for inspection and retry.

## Editing

Edit TOML directly or use `config set/unset`. `ctrl+l` reloads both files,
keeping current settings on error; running sessions retain their launch settings.

## Commit review

```toml
[commit_review]
provider = "claude-code"     # or: profile = "Deep review"
```

**Summarize commits** runs the internal `commit-reviewer` persona (fast tier,
low effort, fast mode off) on `commit_review.provider`, Codex by default: with
Codex that is `gpt-6-luna`, with Claude Code `claude-haiku-5-5`. `profile`
instead selects an existing named agent profile and cannot be combined with
`provider`. Either way the remembered launch selection stays unchanged. A
missing profile, a Bash profile, or a provider that cannot serve the persona
(for example a gateway row without `network.agent_proxy`) produces an error
when the action is invoked.

Commit summaries run in a disposable `python:3.12-slim` sandbox with a
read-only mount for the staged patches and a separate feedback output mount.
The workspace is an empty directory, so no repository metadata is visible.
Isolation is pinned at [level 3](#security-levels) whatever the project's level,
network settings or selected profile: the run gets only the egress backend's
API-only flag (`--egress=sidecar` or, with `security.egress = "firewall"`,
`--egress=firewall`) and never `--no-firewall`, `--extra-domain`,
`--allow-github` or `--pi-ollama`. This also applies to projects without
`[security]`. The default sidecar backend must be set up with
`project-sandbox-egress`; otherwise summaries fail and `sbx-tui doctor` reports
the egress backend with both fixes. Corporate proxy and CA settings still apply.
The launch uses the configured runtime, resources, credentials, and
background timeout. Commit summaries always enable run feedback. Shared and
profile `extra_args` can override `--memory`, `--cpus`, or `--shm-size`.
Other extra arguments are rejected, except a shared `--dry-run` that previews
the summary run; a profile with a network or VCS flag such as `--allow-github` is
reported as widening commit review access. Move those options into profiles used for ordinary launches
if they prevent summaries from starting. Put resource overrides in `extra_args`
when using a `sandbox.command` prefix such as `uvx project-sandbox`; arguments
after `project-sandbox` in that command are rejected. Custom launcher scripts
are trusted host code.

## Agent profiles

Profiles save agent settings for interactive (`i`), background (`r`), merge and
rebase launches. To create one in the TUI:

1. Run `:config-profiles`, leave **new profile** selected, and enter a name,
   for example `Deep review`.
2. Use `tab` / `shift+tab` to move between fields and `h` / `l` on choice
   fields to set the agent, model, effort and fast mode. Choose **custom (enter
   model name)** in the model field to enter a model that is not suggested.
3. Optionally fill **CLI args** with project-sandbox switches, such as
   `--cpus 4 --memory '16g'`. Shell quoting groups arguments;
   shell expressions are passed literally. Unclosed quotes prevent saving or
   launching until corrected.
4. Press `ctrl+s` or click **save**. Then open a launch form with `i` or `r`,
   choose `Deep review` in the **setup** field, and check the command preview.
   Background runs still need a prompt and can have a timeout.

The editor writes to the project's `.project-sandbox-tui.toml`, preserving
other settings and comments. Select an existing profile to edit it. Saving an
existing name updates it; changing the name saves a copy and keeps the original.
`esc` discards edits. Names can contain spaces and punctuation but cannot have
surrounding whitespace; `custom` is reserved, regardless of case.

Saving to a trusted project config keeps it trusted. If the file already has
unreviewed changes, review and trust the resulting diff before the profile takes
effect; see [config trust](#trusting-the-project-config).

Profiles use TOML tables such as:

```toml
[profiles."Deep review"]
agent = "codex"
model = ""                  # agent default
effort = "high"
fast = false
extra_args = ["--memory=16g"]
```

| Field | Meaning | Default when omitted |
|---|---|---|
| `agent` | An agent the launcher supports, such as `claude`, `codex`, `opencode`, `pi`, `copilot` or `bash` | `claude` |
| `model` | Model name; blank uses the agent's default | `""` |
| `effort` | Blank, `low`, `medium`, `high`, `xhigh` or `max` | `""` |
| `fast` | Fast mode for Codex or Claude; requires project-sandbox ≥ 0.3.1 | `false` |
| `gateway` | Launch through the [agent gateway](#agent-gateway) (`pi`, `opencode` or `bash`) | `false` |
| `extra_args` | Additional project-sandbox arguments, as an array of strings | `[]` |

Agent and runtime names are checked for form when the configuration loads
(lowercase letters, digits and dashes), not against a fixed list. Which agents,
`--vcs-access` modes and runtimes are supported comes from the configured
`sandbox.command`: its `--help` and `doctor --json` output (project-sandbox 0.4.1
supports the `copilot` agent and the `colima` and `chroot` runtimes). `sbx-tui doctor`
reports unsupported settings. A launch is refused before the launcher runs when
discovered capabilities show that its agent, `--vcs-access` mode or `--runtime`
is unsupported. When discovery fails, launches are never blocked and the
launcher reports any error itself.

A named profile supplies these fields directly. After merging global and project
config, omitted fields use the defaults in the table rather than `[defaults]` or
remembered launch choices. The remaining project, image, network and sandbox
settings still apply. Profile arguments are appended after `sandbox.extra_args`.
The selected settings are saved when submitted, so later profile edits do not
change queued or running tasks.

Launch forms offer personas, configured profiles and **custom**. Selecting **custom**
shows the individual agent controls and CLI arguments, using remembered launch
choices and [defaults](#settings-reference). Custom CLI arguments are remembered
after an interactive launch or accepted `r` submission, including queued prompts;
using a named profile preserves those Custom arguments. Switching to Custom does
not copy the selected profile into the controls. To adapt a profile, save a copy
in `:config-profiles`.

The setup selected for that launch or submission (a persona with its provider,
a profile, or Custom) is remembered per repository; before the first choice, the
first available profile is selected, or persona `implementer` on the default
provider when no profile is configured.
If a remembered profile is removed, the form falls back to Custom.
Prompts and timeouts are not saved in profiles; timeouts use the usual remembered
launch defaults. Cancelling a launch or editing the queue in `L` does not change
the remembered choices. Host-agent commands do not select profiles; configured
[agent actions](#agent-actions) can name one with `profile`.

Profiles can also be defined globally or edited from the command line:

```sh
sbx-tui config set --global profiles.review.agent codex
sbx-tui config set --global profiles.review.effort high
sbx-tui config set --project profiles.review.extra_args '["--memory=16g"]'
sbx-tui config get profiles.review.extra_args
```

Project fields override global fields of the same profile; arrays replace the
inherited array. The TUI editor saves all fields as a project profile, including
when editing an inherited global profile. The **source** field shows `project`,
`global`, or `project + global` for the selected profile. Press `ctrl+d`, then
`y` to delete it from the config file shown in the confirmation. For a profile
defined in both files, this removes the project entry and the global profile
remains available. Deleting a global entry affects all projects using that file.
You can also remove the whole `[profiles.NAME]` table manually and reload with
`ctrl+l`.

### Agent gateway

`network.agent_proxy` (and optionally `network.agent_proxy_key_env`) defines an
authenticated model gateway such as [agentgateway](https://agentgateway.dev).
A launch uses it only when it opts in with `gateway = true`, from a profile, the
launch form's **gateway** field, or `defaults.gateway`. Only `pi`, `opencode` and
`bash` can use the gateway. Other agents never receive `--agent-proxy`, so one
setup can combine, for example, Claude with forwarded host credentials and Pi
through the gateway:

```toml
[network]
agent_proxy = "http://127.0.0.1:4000/v1"

[models]
pi = ["gpt-5-mini", "claude-sonnet-5-5"]   # suggestions; IDs from the gateway's /v1/models

[profiles."Pi via gateway"]
agent = "pi"
model = "gpt-5-mini"
gateway = true

[profiles.Claude]
agent = "claude"                          # host credentials; no gateway flags
```

A gateway launch needs a model, which must be one the gateway lists:
project-sandbox checks this against the gateway's `/models` before the session
starts. Use the plain model ID for every agent. For OpenCode, sbx-tui passes
`agent-proxy/<id>`, the provider name project-sandbox generates. `defaults.gateway`
is the starting value of the launch form's gateway field for `pi`, `opencode` and
`bash`, and the field is remembered per agent like fast mode. A profile or
`defaults.gateway` set to `true` without `network.agent_proxy` still loads, so a
shared project config can offer gateway profiles that only some users' own
configs can serve. `sbx-tui doctor` warns about it; launches of such a profile
are refused, and `defaults.gateway` has no effect. A profile with
`gateway = true` and another agent is reported by `sbx-tui doctor`, and its
launches are refused.

## Model catalogue and personas

The launch forms' **persona** setup, persona actions and workflow steps pick a
provider row and a persona; see [models, providers and personas](models.md) for
the built-in rows, tier models and personas.

```toml
[catalogue]
default_provider = "claude-code"   # "" = the first available provider
gateway_agent = "pi"               # agent of the gateway rows: pi | opencode
providers = ["claude-code", "copilot-fusion"]  # rows the forms offer; [] = all

[catalogue.models.anthropic]       # a vendor: openai | anthropic | google
frontier = "claude-fable-5-2"

[catalogue.models.gateway-anthropic]  # a provider row; wins over its vendor
high = "claude-opus-5"
frontier = ""                      # empty: the tier is unavailable for this row

[personas.reviewer]                # override fields of a built-in persona
tier = "frontier"

[personas.triage]                  # or define a new one; tier is required
tier = "fast"
effort = "low"                     # "" or low | medium | high | xhigh | max
fast = false
level = 3                          # starting security level 1-5; unset = project default
```

`[catalogue.models.<vendor>]` changes the tier for every row of that vendor,
including Copilot rows, and `[catalogue.models.<row>]` for that row only. An
unknown vendor, row or tier is a load error naming the key, as is a
`gateway_agent` other than `pi` or `opencode`. Overridden personas keep their
built-in values for unset fields. Persona names use lowercase letters, digits
and dashes; `commit-reviewer` is internal and cannot be used by actions.
`catalogue.default_provider` names the provider a selection starts with when
none is remembered; `sbx-tui doctor` fails when it names an unavailable provider.

`catalogue.providers` limits the provider rows that launch forms and the
workflow picker offer, kept in catalogue order; an empty list (the default)
offers every row. A remembered provider that is not listed is replaced by the
default, and `catalogue.default_provider` must be in the list when both are
set. Doctor shows the other rows as hidden. `sbx-setup-wizard` writes this key to
the project config from its provider checklist. If that file has unreviewed
changes, review it with `sbx-tui --trust-config` before changing the selection.

## Security levels

`[security] level` gives the project a default isolation level on a linear scale.
A higher level means more isolation. sbx-tui compiles each level to launcher
flags:

| Level | Flags |
| --- | --- |
| 1 | `--no-firewall` |
| 2 | `--egress=firewall`, `--extra-domain` for the package sources of `project.build` and then each `network.extra_domains` entry, and `--allow-github` with `network.allow_github` |
| 3 | the egress backend only: `--egress=sidecar`, or `--egress=firewall` with no extra domains |
| 4 | level 3 plus `--vcs-access=read-only` |
| 5 | level 3 plus `--vcs-access=hidden` |

Level 2's package sources are `pypi.org` and `files.pythonhosted.org` for
`python-uv`; `crates.io`, `index.crates.io` and `static.crates.io` for
`rust-cargo`; and `registry.npmjs.org` for `node-npm`. A Dockerfile or base
image adds none. With `network.internet_proxy` set, level 2 adds no
`--extra-domain` (the launcher rejects it) and its preview says the proxy's
policy decides which hosts are reachable. Levels 4 and 5 restrict repository
metadata; source files stay writable.

```toml
# .project-sandbox-tui.toml
[security]
level = 3

# ~/.config/project-sandbox-tui/config.toml
[security]
egress = "sidecar"      # sidecar (default) | firewall: the backend of levels 3-5

[network]
internet_proxy = "http://proxy.example:3128"
ca_certs = ["/etc/ssl/corp-root.pem"]
build_proxy = "http://proxy.example:3128"    # --build-proxy (image builds only)
build_no_proxy = [".corp.example"]           # --build-no-proxy …
build_ca_certs = ["/etc/ssl/corp-root.pem"]  # --build-ca-cert …
```

The sidecar backend routes traffic through the runtime's global Squid sidecar,
whose `$XDG_CONFIG_HOME/project-sandbox/egress.toml` decides the allowed hosts
for every project on that runtime; set it up with `project-sandbox-egress`.
`sbx-tui doctor` lists the hosts it can read and never changes the file. A
selected backend that the launcher does not offer fails the launch; sbx-tui
never switches backends. Level 3 is the launcher's minimum, not a guarantee:
it is as open as the sidecar policy.

Each job's level starts at the action's `level`, else its persona's level, else
the project default, and can be changed per job in the `i` / `r` launch forms
and the workflow picker. A level below the project default is shown in red, and
`:NAME` asks for confirmation before running an action below it. Levels are not
remembered. A level the discovered launcher cannot run (no `--egress` for levels
2 to 5, a missing firewall backend for level 2, a missing configured backend
for levels 3 to 5, or missing VCS access modes for levels 4 and 5) is marked
unavailable and refused; a launcher that could not be probed restricts nothing.
Tasks queued before a project opted in run at its default level when they start.

When a security level is active, it is the only source of network and VCS flags:

- `--no-firewall`, `--egress`, `--extra-domain`, `--allow-github`,
  `--vcs-access`, `--pi-ollama`, `--internet-proxy`,
  `--no-internet-proxy-preflight`, `--ca-cert`, `--build-proxy`,
  `--build-no-proxy` and `--build-ca-cert` in `sandbox.extra_args`, profile
  `extra_args` or custom CLI args refuse the launch, naming the setting.
  Doctor reports them per config key. Use `network.extra_domains` with level 2
  instead of `--extra-domain`, and level 1 for jobs that need no firewall.
- `network.no_firewall` is ignored; doctor warns about it.
- `network.internet_proxy`, `no_internet_proxy_preflight`, `ca_certs`,
  `build_proxy`, `build_no_proxy` and `build_ca_certs` may only be set in the
  global config: a project config that sets them fails to load. `security.egress`
  is global-only in every project, since it describes the machine's runtime.

Corporate proxy and CA settings apply at every level. Persona and action
launches can carry a level even in projects without `security.level`; all other
launches there keep the network flags they had before levels existed.

## From the command line

```sh
sbx-tui config list                     # effective settings
sbx-tui config list --project           # one file (--global / --project)
sbx-tui config get defaults.agent
sbx-tui config set --global defaults.agent codex
sbx-tui config set --project project.build python-uv
sbx-tui config set --global network.extra_domains '["pypi.org", "files.pythonhosted.org"]'
sbx-tui config unset --project defaults.effort
sbx-tui config path --global
```

Use `section.key` and typed values (booleans, numbers, TOML arrays). Edits preserve
comments and validate against the other file before writing. New or trusted
project files remain trusted; unreviewed changes remain untrusted.

## Trusting the project config

Project config can run host commands (`sandbox.command`, `ui.*_command`, `[commands]`),
start agents with its own prompts (`[actions]`) and
loosen network, credential or sandbox restrictions (`extra_args`). Only reviewed
settings take effect:

- Startup and `ctrl+l` show new or changed files as a diff against the trusted
  copy; `y` trusts exactly that version. Invalid files are not offered.
- Until reviewed, new files are ignored; changed files use the trusted copy.
- `--trust-config` offers the same review in the terminal, one file at a time.
  It reports invalid files and continues with the rest; it exits 2 if a file was
  invalid, 1 if one was declined. `--print-config` and `--cleanup-workspaces`
  warn about untrusted files.
- `--init` trusts its new template.

## Settings reference

For Custom fields, remembered values take precedence over trusted project settings,
then global settings and built-in defaults. See [launch setup selection](#agent-profiles)
and [action settings](#agent-actions). Security levels apply per run.

```toml
[defaults]
agent = "claude"        # claude | codex | opencode | pi | copilot | bash
model = ""              # "" = the agent's own default
effort = ""             # "" or low | medium | high | xhigh | max
fast = false            # Codex / Claude only; requires project-sandbox >= 0.3.1
gateway = false         # pi / opencode / bash through network.agent_proxy
timeout = 0             # seconds for unsupervised runs, 0 = none

[network]
internet_proxy = ""                            # --internet-proxy
no_internet_proxy_preflight = false            # --no-internet-proxy-preflight
agent_proxy = ""                               # --agent-proxy, for launches with gateway = true
agent_proxy_key_env = ""
ca_certs = []                                  # --ca-cert …
build_proxy = ""                               # --build-proxy (image builds)
build_no_proxy = []                            # --build-no-proxy …
build_ca_certs = []                            # --build-ca-cert …
extra_domains = []                             # --extra-domain …; level 2 only with [security]
allow_github = false                           # level 2 only with [security]
no_firewall = false                            # ignored with [security]

[security]              # see "Security levels"
level = 3               # project default 1-5; unset = earlier network behaviour
egress = "sidecar"      # global config only: sidecar | firewall

[catalogue]             # see "Model catalogue and personas"
default_provider = ""
gateway_agent = "pi"
providers = []          # provider rows the forms offer; [] = all

[commit_review]
provider = ""           # "" = codex; or profile = "NAME"

[sandbox]
command = "project-sandbox"   # or "uvx project-sandbox"
runtime = "auto"              # auto | apple-container | docker | podman | colima | chroot
memory = ""                   # e.g. "16g"
cpus = 0
no_forward_credentials = false
api_key_env = []
api_key_env_file = []
extra_args = []               # appended verbatim

[project]                     # usually in .project-sandbox-tui.toml
# Choose exactly one: base_image, dockerfile or build.
base_image = "python:3.12-slim"
# dockerfile = "Dockerfile"
# docker_context = "."
# build = "python-uv"          # python-uv | rust-cargo | node-npm
build_version = ""                # --python / --rust for a generated build
worktree_dir = ""                 # default: sibling <repo>-workspaces
keep_workspace = true             # reuse the workspace across runs
default_branch_start = ""         # new bookmarks: auto main/master/trunk, else @- (or @ with no bookmarks)
main_branch = ""                  # main for diff, fast-forward, @ saves and cleanup; auto main/master/trunk
deploy_config_skill = true        # copy both bundled configuration skills into bookmark workspaces

[ui]
notify_command = ""     # "" = platform default; {title} {message}
terminal = "inline"     # inline | tmux | zellij | screen | custom
terminal_command = ""   # with terminal = "custom"; {cmd} {branch} {repo}
editor_command = ""     # GUI editor, e.g. 'code {path}'; "" = $EDITOR in this terminal
host_agent_command = "" # I before first :host: last-used agent; {agent} {path} {branch}
refresh_seconds = 3
bell = true
cheatsheet = true
leader = '\'           # starts [leader] mappings; "" = none

[commands]              # :name runs the shell command; see below
test = "uv run pytest"

[leader]                # ui.leader, then these keys, runs a command line
t = "test"
T = "*test"             # in each selected workspace

[actions.review]        # :review starts the prompt on each selected bookmark
prompt = "Review {bookmark} against {main} and fix what you find."
persona = "reviewer"    # or profile, or agent / model / effort; timeout, level

[personas.reviewer]     # tier, effort, fast, level over the built-in persona
tier = "frontier"

[models]                # extra launch-form suggestions, listed first
claude = ["opus", "sonnet"]
codex = ["gpt-5.5"]

[prompts]               # templates and placeholders: see merging.md
# octopus_merge = "…"
# octopus_rebase = "…"

[[workspace_skills]]    # top level; see "Workspace skills"
source = "~/private-skills/release-review"
targets = [".agents/skills/release-review", ".claude/skills/release-review"]
```

`[[workspace_skills]]` is edited in the TOML files; `config list` shows the
effective entries as `workspace_skills[N].source` and `.targets`.

Command placeholders must occupy a whole argument, e.g. `code {path}` or
`notifier --title {title}`. Embedding placeholders inside arguments or shell
scripts is rejected: quoting alone cannot safely insert data into arbitrary
shell code. Pass data to scripts as positional arguments, e.g.
`sh -c 'printf %s "$1"' sh {branch}`. Treat these values as data; commands such
as `eval` can execute them even when passed safely.

`{cmd}` is a command line, so pass it to a shell:
`'wezterm start -- sh -c {cmd}'`. `alacritty -e {cmd}` treats it as one word.

## Commands and leader key

`[commands]` names shell commands for the [command line](usage.md#command-line):
`:name args` runs the command with `sh -c` in the repository foreground, with `args`
appended (`:test -k merge` runs `uv run pytest -k merge`). Names start with a
letter and use letters, digits, `_` and `-`; built-in commands (`help`, `doctor`,
`init`, `update-models`, `quit`, `q`, `refresh`, `reload`, `host`, `workflow`, `reorganise`,
`reorganize`, `config-profiles`) cannot be redefined.
Unlike `ui.*_command`, these are shell code: use `SBX_REPO`, `SBX_BOOKMARK` and
`SBX_WORKSPACE` rather than placeholders, quoted as in `cd "$SBX_WORKSPACE"`.

One command is built in as a default, and a `[commands]` entry of the same name
replaces it:

| Command | Runs |
|---|---|
| `:abandon-empty` | `jj abandon -r '(~empty())..'`: abandons empty commits that have only empty descendants, such as leftover empty heads |

`:abandon-empty` asks for confirmation first: run it only when all workspaces are
inactive. Other workspaces' empty working-copy commits are empty heads too, so
abandoning them leaves those workspaces stale; bookmarks on empty heads are
deleted. A redefined `abandon-empty` runs without asking.

See the
[command comparison](usage.md#choosing-how-to-run-a-shell-command) for selection,
directories, environment variables, output and cancellation.

`[leader]` maps key sequences typed after `ui.leader` (one character, `\` by
default) to command lines as typed after `:`, e.g. `t = "test"`, `b = "&test"`, `T = "*test"`,
`s = "!jj st"`, `rc = "chores"` or `r = "reload"`. Sequences may be longer than one key
(`tf = "test -x"`), but none may be a prefix of another, so a mapping runs as
soon as it is complete. Unknown command names are configuration errors. A
leader that is also a list key (e.g. `" "`) replaces that key's action.

```sh
sbx-tui config set --project commands.test 'uv run pytest'
sbx-tui config set --global leader.s '!jj st'
```

## Agent actions

`[actions.NAME]` names a prompt that `:NAME` starts as an unsupervised sandbox
run on each selected bookmark (or the highlighted one); see
[agent actions](usage.md#agent-actions). Map them to leader keys like any
command line:

```toml
[actions.chores]
prompt = '''
Tidy up {bookmark}: update the docs and tests for its changes against {main}.
'''
agent = "claude"        # optional: persona, profile, or agent / model / effort; timeout, level
timeout = 1800

[leader]
rc = "chores"
```

`prompt` is required. `{bookmark}` and `{main}` are replaced; other braces stay
as written. Names follow the `[commands]` rules and must not repeat a
`[commands]` name. An action chooses its agent settings in one of four ways:

- `persona = "NAME"` runs it as a built-in or configured
  [persona](#model-catalogue-and-personas) on the repository's remembered
  provider, or `catalogue.default_provider` when none is remembered. It adds no
  profile or custom CLI args and cannot be combined with `profile`, `agent`,
  `model` or `effort`; an unknown persona is a load error. With
  `persona = "reviewer"` and Claude Code remembered, `:review` runs
  `--agent=claude --model=claude-opus-5-5 --effort=high` at level 4.
- `profile = "NAME"` runs it with the [agent profile](#agent-profiles)'s agent,
  model, effort, fast mode and CLI args. It cannot be combined with `agent`,
  `model` or `effort`, and `custom` is not a profile name.
- `agent`, `model` or `effort` set on the action apply directly; unset ones take
  the launch defaults, and a `model` is only taken from the defaults for the same
  agent. No profile CLI args are added.
- With none of these, the action uses a remembered persona selection, else the
  profile that the `i` / `r` launch forms would select: the remembered profile,
  or the first configured one before any choice. When that is **custom**, the
  remembered custom agent settings and CLI args apply.

`timeout` applies in every case; unset, it takes the launch default. `level`
(1 to 5) sets the run's [security level](#security-levels); unset, it takes the
persona's level, else the project default. It also works without
`security.level`. When the resolved level is below the project default, `:NAME`
asks for confirmation naming both levels before launching or queueing, and
nothing starts if you decline. If `profile`
names a profile that is not configured, for example after deleting it in
`:config-profiles`, sbx-tui asks before launching or queuing the action with the
fallback profile. The prompt names the missing profile and shows the replacement
and its extra arguments. `sbx-tui doctor` lists such actions as an `action profiles`
warning.

```toml
[actions.deep-review]
prompt = "Review {bookmark} against {main}."
profile = "Deep review"
timeout = 1800
```

```sh
sbx-tui config set --project actions.chores.prompt 'Tidy up {bookmark}.'
sbx-tui config set --project actions.chores.effort low
sbx-tui config set --project actions.deep-review.profile 'Deep review'
sbx-tui config set --project actions.review.persona reviewer
sbx-tui config set --project actions.e2e.level 1
```

## State directory

`$XDG_STATE_HOME/project-sandbox-tui/` (default `~/.local/state/…`) holds:

| Location | Contents |
|---|---|
| `models.json` | Downloaded model catalog |
| `config.lock` | Serializes config edits, template creation and project trust approvals |
| `<repo>-<hash>/last_used.json`, `last_used.lock` in the same directory | Launch defaults, selected profile, Custom CLI arguments and host command |
| `<repo>-<hash>/prompt_history.json` | Prompt history |
| `<repo>-<hash>/command_history.json` | Command-line history |
| `<repo>-<hash>/opgraph.json` | [Bookmark history](usage.md#bookmark-history) and undo stack |
| `<repo>-<hash>/diff_review.json` | File changes marked seen in the [diff review](usage.md#diff-review) |
| `<repo>-<hash>/capabilities.json` | Cached launcher capabilities |
| `<repo>-<hash>/guidance.lock` | Serializes installation of reporting guidance |
| `<repo>-<hash>/trusted-workflows/` | Reviewed copies of workflow files |
| `<repo>-<hash>/runs/<run_id>/` | Per-run feedback context, helper, skill and report |
| `<repo>-<hash>/trusted-project-config.toml` | Reviewed project config |
| `<repo>-<hash>/queue.json`, `<repo>-<hash>/queue.lock` | Prompt task IDs and order, pause state, pending-task undo/redo and named marks |
| `<repo>-<hash>/queue-tasks/` | Records for pending, running and completed prompt tasks and workflow steps |
| `<repo>-<hash>/workflow-workspaces/` | Temporary jj workspaces for reviewed host commands |
| `<repo>-<hash>/queue-transaction.json` | Temporary recovery journal for updates spanning task records and the queue index |
| `<repo>-<hash>/jobs/`, `<repo>-<hash>/jobs.lock` | Job records and cross-instance lock |
| `<repo>-<hash>/jobs/history/<bookmark>-<hash>/` | [Replaced and cleared job records](sessions.md#one-sandbox-per-bookmark), the last 50 per bookmark |
| `<repo>-<hash>/logs/` | Agent logs, transcripts, summaries and captured output, including `:&` and `:*` tasks |
| `<repo>-<hash>/tasks/`, `<repo>-<hash>/tasks.lock` | [Background task](usage.md#tasks) records (`:&` and `:*`) |

Job records include full argv and prompts. Repository state, job and log
directories use mode `0700` (user-only access), including older directories.

The hash comes from the resolved checkout path. Instances opened from the same
checkout share job and task locks. Other checkouts of the same jj repository
have independent locks and records, even when they share `project.worktree_dir`.
Cleanup surveys all registered jj workspaces. See
[cleanup from a second checkout](cleanup.md#cleanup-from-a-second-checkout).

Each state update re-reads its file under a lock and replaces it atomically.
See [concurrency and locking](architecture.md#concurrency-and-locking). A history, review or prompt file
that cannot be read is left as it is rather than overwritten; one that does not
parse is renamed to `<name>.corrupt-<time>` and started afresh. Files written
by a newer sbx-tui version are not changed.

## Feedback settings

The `[feedback]` section controls reporting, success-only queue progression,
selected state handoff, and bounded retries/successor actions. Reporting defaults
to enabled with capability detection; queue behavior is unchanged until a policy
is configured. See [agent feedback](feedback.md#queue-policies) for all defaults
and examples.

Use `[workflows.NAME]` to run `[actions.NAME]` prompts, `human-review` stages,
and `host:NAME` commands on the same bookmark. Set `first_step_interactive = false`
to queue the planning action. See [project workflows](feedback.md#project-workflows)
for the configuration and task controls.

## Shareable workflow files

Put one workflow in `.project-sandbox-tui/workflows/NAME.toml`, using a
`[workflow]` table, `[actions.SHORT]` tables and an optional `[commands]`
table. The filename supplies the workflow name. References inside the file use
short names; outside it, actions and commands are `NAME/SHORT`. For example:

```toml
[workflow]
planning_action = "plan"
steps = ["host:test"]

[actions.plan]
prompt = "Plan the change."
persona = "planner"

[commands]
test = '''
echo preparing
scripts/test.sh
'''
```

Workflow-file actions select agents using `persona`, or use the launch-form
selection. They cannot set `profile`, `agent`, `model`, or `effort`. Commands
are shell scripts; extra words typed after a command are appended to the script
and therefore land on its last line. Keep paths and prompts portable when
sharing; use inline `[workflows.NAME]` for project-specific dependencies.

Each file is reviewed separately with `sbx-tui --trust-config` or at TUI
startup. A new file is ignored until trusted, and a changed file keeps its
previously trusted contents until reviewed, as does a file that cannot be read
as UTF-8. Deleting it removes the workflow. Symlinked files, and a symlinked
`.project-sandbox-tui` or `workflows` directory, are ignored with a warning.
To copy a file into the current project, run
`sbx-tui workflow import SOURCE [--name NAME]`, where SOURCE is an HTTPS URL or
local path. Import validates and previews the file and its provenance before
confirmation; re-import to update it. `sbx-tui workflow import --help` lists the
options. Doctor reports trust and whether an imported file has been edited.
