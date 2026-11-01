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

Precedence: remembered launch choices → trusted project → global → built-in.
The last agent, effort, timeout and per-agent model and fast mode are remembered
per repository.

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
main_branch = ""                  # main for diff, fast-forward and cleanup; auto main/master/trunk

[ui]
notify_command = ""     # "" = platform default; {title} {message}
terminal = "inline"     # inline | tmux | zellij | screen | custom
terminal_command = ""   # with terminal = "custom"; {cmd} {branch} {repo}
editor_command = ""     # GUI editor, e.g. 'code {path}'; "" = $EDITOR in this terminal
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
`:name args` runs the command with `sh -c` in the repository, with `args`
appended (`:test -k merge` runs `uv run pytest -k merge`). Names start with a
letter and use letters, digits, `_` and `-`; built-in commands (`help`, `quit`,
`q`, `refresh`, `reload`) cannot be redefined. Unlike `ui.*_command`, these are
shell code: use `SBX_REPO`, `SBX_BOOKMARK` and `SBX_WORKSPACE` rather than
placeholders, quoted as in `cd "$SBX_WORKSPACE"`.

`:*name` runs it in each selected workspace instead, with `cwd`,
`SBX_BOOKMARK` and `SBX_WORKSPACE` set for each; see
[running in each workspace](usage.md#running-in-each-workspace).

`[leader]` maps key sequences typed after `ui.leader` (one character, `\` by
default) to command lines as typed after `:`, e.g. `t = "test"`, `T = "*test"`,
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
| `<repo>-<hash>/last_used.json` | Launch defaults |
| `<repo>-<hash>/prompt_history.json` | Prompt history |
| `<repo>-<hash>/command_history.json` | Command-line history |
| `<repo>-<hash>/opgraph.json` | [Bookmark history](usage.md#bookmark-history) and undo stack |
| `<repo>-<hash>/diff_review.json` | Files marked seen in the [diff review](usage.md#diff-review) |
| `<repo>-<hash>/trusted-project-config.toml` | Reviewed project config |
| `<repo>-<hash>/jobs/`, `<repo>-<hash>/jobs.lock` | Job records and cross-instance lock |
| `<repo>-<hash>/jobs/history/<bookmark>-<hash>/` | [Replaced and cleared job records](sessions.md#one-sandbox-per-bookmark), the last 50 per bookmark |
| `<repo>-<hash>/logs/` | Agent logs, transcripts, summaries and captured output, including `:*` tasks |
| `<repo>-<hash>/tasks/`, `<repo>-<hash>/tasks.lock` | [`:*` task](usage.md#running-in-each-workspace) records |

Job records include full argv and prompts. Repository state, job and log
directories use mode `0700` (user-only access), including older directories.

TUI instances on one repository share these files: each change re-reads its
file under a [lock](architecture.md#concurrency-and-locking) and replaces it
atomically. A history, review or prompt file
that cannot be read is left as it is rather than overwritten; one that does not
parse is renamed to `<name>.corrupt-<time>` and started afresh. Files written
by a newer sbx-tui version are not changed.
