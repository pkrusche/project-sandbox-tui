# Configuration

Settings are deep-merged from global
`$XDG_CONFIG_HOME/project-sandbox-tui/config.toml` (default `~/.config/…`) and
trusted `<repo>/.project-sandbox-tui.toml`. Unknown keys are errors. `--init`
writes missing templates.

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

Project config can run host commands (`sandbox.command`, `ui.*_command`, `[commands]`) and
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
The last agent, effort, timeout and per-agent model are remembered per repository.

```toml
[defaults]
agent = "claude"        # claude | codex | opencode | pi | bash
model = ""              # "" = the agent's own default
effort = ""             # "" or low | medium | high | xhigh | max
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

`[leader]` maps key sequences typed after `ui.leader` (one character, `\` by
default) to command lines as typed after `:`, e.g. `t = "test"`,
`s = "!jj st"` or `r = "reload"`. Sequences may be longer than one key
(`tf = "test -x"`), but none may be a prefix of another, so a mapping runs as
soon as it is complete. Unknown command names are configuration errors. A
leader that is also a list key (e.g. `" "`) replaces that key's action.

```sh
sbx-tui config set --project commands.test 'uv run pytest'
sbx-tui config set --global leader.s '!jj st'
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
| `<repo>-<hash>/trusted-project-config.toml` | Reviewed project config |
| `<repo>-<hash>/jobs/`, `<repo>-<hash>/jobs.lock` | Job records and cross-instance lock |
| `<repo>-<hash>/logs/` | Agent logs, transcripts, summaries and captured output |

Job records include full argv and prompts. Repository state, job and log
directories use mode `0700` (user-only access), including older directories.
