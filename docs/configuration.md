# Configuration

Settings are deep-merged from global
`$XDG_CONFIG_HOME/project-sandbox-tui/config.toml` (default `~/.config/…`) and
trusted `<repo>/.project-sandbox-tui.toml`. Unknown keys are errors.

## Initialization

Run `sbx-tui --init` from the repository or a subdirectory (or pass a repository
path). It writes missing global and project config files, trusts only the project
config it creates, and downloads the model catalog if missing. Existing files
and their trust status are preserved.

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
The new template explicitly clears unused image settings so that an inherited
global image source or build version cannot conflict with the detected choice.

Initialization also installs the `sbx-tui-config` agent skill at:

- `.agents/skills/sbx-tui-config/SKILL.md` ([Codex skill location](https://developers.openai.com/codex/skills/)).
- `.claude/skills/sbx-tui-config/SKILL.md` ([Claude Code skill location](https://code.claude.com/docs/en/skills)).

Both copies teach image selection, configuration scopes, commands/actions, and
validation and trust handling. Ask the agent to use `sbx-tui-config` to configure
the project; other agents can read either file directly. Only these two skill
directories are added to the root `.gitignore`; other skills and the project
config remain eligible for version control. Existing skill files are preserved.
To refresh a bundled skill, remove its installed `SKILL.md` and rerun `--init`.
Ignored skills are local to each checkout/workspace, so run `--init` there too
when needed.

## Editing

Edit TOML directly or use `config set/unset`. `ctrl+l` reloads both files,
keeping current settings on error; running sessions retain their launch settings.

## Agent profiles

Profiles save an agent setup for reuse in interactive (`i`), background (`r`),
and merge or rebase launch forms. To create one in the TUI:

1. Run `:config-profiles`, leave **new profile** selected, and enter a name,
   for example `Deep review`.
2. Use `tab` / `shift+tab` to move between fields and `h` / `l` on choice
   fields to set the agent, model, effort and fast mode. Choose **custom (enter
   model name)** in the model field to enter a model that is not suggested.
3. Optionally fill **CLI args** with project-sandbox switches, such as
   `--extra-domain example.org --memory '16g'`. Shell quoting groups arguments;
   shell expressions are passed literally. Unclosed quotes prevent saving or
   launching until corrected.
4. Press `ctrl+s` or click **save**. Then open a launch form with `i` or `r`,
   choose `Deep review` in the **profile** field, and check the command preview.
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
| `agent` | `claude`, `codex`, `opencode`, `pi` or `bash` | `claude` |
| `model` | Model name; blank uses the agent's default | `""` |
| `effort` | Blank, `low`, `medium`, `high`, `xhigh` or `max` | `""` |
| `fast` | Fast mode for Codex or Claude; requires project-sandbox ≥ 0.3.1 | `false` |
| `extra_args` | Additional project-sandbox arguments, as an array of strings | `[]` |

A named profile supplies these fields directly. After merging global and project
config, omitted fields use the defaults in the table rather than `[defaults]` or
remembered launch choices. The remaining project, image, network and sandbox
settings still apply. Profile arguments are
appended after `sandbox.extra_args`; a run records the selected settings at
launch, so later profile edits do not change running sessions.

Launch forms offer the configured profiles plus **custom**. Selecting **custom**
shows the individual agent controls and CLI arguments, using remembered launch
choices and [defaults](#settings-reference). Custom CLI arguments are remembered
after a successful Custom launch; launching a named profile preserves those
Custom arguments. Switching to Custom does not copy the selected profile into
the controls. To adapt a profile, save a copy in `:config-profiles`.

The last successfully launched profile (including Custom) is remembered per
repository; before the first choice, the first available profile is selected.
If a remembered profile is removed, the form falls back to Custom.
Prompts and timeouts are not saved in profiles; timeouts use the usual remembered
launch defaults. Cancelling a launch does not change the remembered profile.
Host-agent commands and configured agent actions do not select profiles.

Profiles can also be defined globally or edited from the command line:

```sh
sbx-tui config set --global profiles.review.agent codex
sbx-tui config set --global profiles.review.effort high
sbx-tui config set --project profiles.review.extra_args '["--memory=16g"]'
sbx-tui config get profiles.review.extra_args
```

Project fields override global fields of the same profile; arrays replace the
inherited array. The TUI editor saves all fields as a project profile, including
when editing an inherited global profile. To remove a profile, delete its entire
`[profiles.NAME]` table from the config file and reload with `ctrl+l`. Removing
only its project table reveals any global profile with the same name.

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
- `--trust-config` offers the same review in the terminal. `--print-config` and
  `--cleanup-workspaces` warn about untrusted files.
- `--init` trusts its new template.

## Settings reference

For Custom launches, precedence is remembered launch choices → trusted project
→ global → built-in. The last agent, effort, timeout and per-agent model and
fast mode are remembered per repository. A successful named-profile launch also
updates these agent choices; the profile's fields take precedence when it is
selected. Profile selection and Custom CLI arguments are remembered separately;
see [agent profiles](#agent-profiles).

```toml
[defaults]
agent = "claude"        # claude | codex | opencode | pi | bash
model = ""              # "" = the agent's own default
effort = ""             # "" or low | medium | high | xhigh | max
fast = false            # Codex / Claude only; requires project-sandbox >= 0.3.1
timeout = 0             # seconds for unsupervised runs, 0 = none

[network]
internet_proxy = ""                            # --internet-proxy
agent_proxy = ""                               # --agent-proxy
agent_proxy_key_env = ""
ca_certs = []                                  # --ca-cert …
extra_domains = []                             # --extra-domain …
allow_github = false
no_firewall = false

[sandbox]
command = "project-sandbox"   # or "uvx project-sandbox"
runtime = "auto"              # auto | apple-container | docker | podman
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
effort = "high"         # agent, model, effort, timeout: launch defaults if unset

[profiles.review]       # select in i/r forms; edit with :config-profiles
agent = "codex"
model = ""
effort = "high"
fast = false
extra_args = ["--memory=16g"]

[models]                # extra launch-form suggestions, listed first
claude = ["opus", "sonnet"]
codex = ["gpt-5.5"]

[prompts]               # templates and placeholders: see merging.md
# octopus_merge = "…"
# octopus_rebase = "…"
```

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
letter and use letters, digits, `_` and `-`; built-in commands (`help`, `quit`,
`q`, `refresh`, `reload`, `host`, `config-profiles`) cannot be redefined. Unlike `ui.*_command`, these are
shell code: use `SBX_REPO`, `SBX_BOOKMARK` and `SBX_WORKSPACE` rather than
placeholders, quoted as in `cd "$SBX_WORKSPACE"`.

One command is built in as a default, and a `[commands]` entry of the same name
replaces it:

| Command | Runs |
|---|---|
| `:abandon-empty` | `jj abandon -r '(~empty())..'`: abandons empty commits that have only empty descendants, such as leftover empty heads |

`:abandon-empty` asks for confirmation first: run it only when all workspaces are
inactive. Other workspaces' empty working-copy commits are empty heads too, so
abandoning them leaves those workspaces stale; bookmarks on empty heads are
deleted. A redefined `abandon-empty` runs without asking.

`:&name` runs it once in the background in the repository, ignoring marked rows;
see [background commands](usage.md#background-commands).
`:*name` runs it in the background in each selected workspace instead, with `cwd`,
`SBX_BOOKMARK` and `SBX_WORKSPACE` set for each; see
[running in each workspace](usage.md#running-in-each-workspace).
The [comparison table](usage.md#choosing-how-to-run-a-shell-command) explains
selection, directories, environment variables, output and cancellation for all
three forms. These prefixes choose how to run the same configured shell script;
you do not need separate `[commands]` entries.

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
agent = "claude"        # optional: agent, model, effort, timeout
timeout = 1800

[leader]
rc = "chores"
```

`prompt` is required. `{bookmark}` and `{main}` are replaced; other braces stay
as written. Unset `agent`, `model`, `effort` and `timeout` take the launch
defaults; a `model` is only taken from the defaults for the same agent. Names
follow the `[commands]` rules and must not repeat a `[commands]` name.

```sh
sbx-tui config set --project actions.chores.prompt 'Tidy up {bookmark}.'
sbx-tui config set --project actions.chores.effort low
```

## State directory

`$XDG_STATE_HOME/project-sandbox-tui/` (default `~/.local/state/…`) holds:

| Location | Contents |
|---|---|
| `models.json` | Downloaded model catalog |
| `<repo>-<hash>/last_used.json` | Launch defaults, selected profile, Custom CLI arguments and host command |
| `<repo>-<hash>/prompt_history.json` | Prompt history |
| `<repo>-<hash>/command_history.json` | Command-line history |
| `<repo>-<hash>/opgraph.json` | [Bookmark history](usage.md#bookmark-history) and undo stack |
| `<repo>-<hash>/diff_review.json` | File changes marked seen in the [diff review](usage.md#diff-review) |
| `<repo>-<hash>/trusted-project-config.toml` | Reviewed project config |
| `<repo>-<hash>/jobs/`, `<repo>-<hash>/jobs.lock` | Job records and cross-instance lock |
| `<repo>-<hash>/jobs/history/<bookmark>-<hash>/` | [Replaced and cleared job records](sessions.md#one-sandbox-per-bookmark), the last 50 per bookmark |
| `<repo>-<hash>/logs/` | Agent logs, transcripts, summaries and captured output, including `:&` and `:*` tasks |
| `<repo>-<hash>/tasks/`, `<repo>-<hash>/tasks.lock` | [Background task](usage.md#tasks) records (`:&` and `:*`) |

Job records include full argv and prompts. Repository state, job and log
directories use mode `0700` (user-only access), including older directories.

Diff review positions (commit and file row) are kept only in the current TUI
instance and reset on exit. Seen marks in `diff_review.json` persist across
restarts and are shared between instances.

TUI instances on one repository share these files: each change re-reads its
file under a [lock](architecture.md#concurrency-and-locking) and replaces it
atomically. A history, review or prompt file
that cannot be read is left as it is rather than overwritten; one that does not
parse is renamed to `<name>.corrupt-<time>` and started afresh. Files written
by a newer sbx-tui version are not changed.
