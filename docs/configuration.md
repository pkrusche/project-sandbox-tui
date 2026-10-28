# Configuration

Global settings live in `$XDG_CONFIG_HOME/project-sandbox-tui/config.toml`
(default `~/.config/…`). Trusted `<repo>/.project-sandbox-tui.toml` settings are
deep-merged on top; unknown keys are errors. `--init` writes templates without
overwriting existing files.

`ctrl+r` reloads both files, keeping current settings on error. Running sessions
keep their launch settings. The TUI reads TOML; use an editor or `config set/unset`
to change it.

## From the command line

```sh
sbx-tui config list                     # settings in effect (global + trusted project)
sbx-tui config list --project           # what one file sets (--global / --project)
sbx-tui config get defaults.agent
sbx-tui config set --global defaults.agent codex
sbx-tui config set --project project.build python-uv
sbx-tui config set --global network.extra_domains '["pypi.org", "files.pythonhosted.org"]'
sbx-tui config unset --project defaults.effort
sbx-tui config path --global
```

Keys use `section.key`; values use the setting's type (booleans, numbers, TOML
arrays). Edits preserve comments and are validated against the other file before
writing. A new or trusted project file stays trusted after editing; one with
unreviewed changes stays untrusted.

## Trusting the project config

Project config can run host commands (`sandbox.command`, `ui.*_command`) or
loosen sandbox restrictions (network, credentials, `extra_args`). Only a reviewed
copy takes effect:

- When the file is new or has changed, sbx-tui shows it as a diff against the
  trusted copy at startup and on `ctrl+r`; `y` trusts exactly what was shown.
- Until then a never-trusted file is ignored, and a changed one keeps the
  previously trusted version in effect. A file that fails to load is not offered.
- `sbx-tui --trust-config` does the same review in the terminal;
  `--print-config` and `--cleanup-workspaces` warn about an untrusted file.
- `sbx-tui --init` trusts the template it writes.

## Settings reference

Defaults resolve as project → global → built-in. Remembered launch choices
override these; models are remembered per agent.

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
cheatsheet = true       # H toggles; collapses to one help line in small terminals

[models]                # extra launch-form suggestions, listed first
claude = ["opus", "sonnet"]
codex = ["gpt-5.5"]

[prompts]               # templates and placeholders: see merging.md
# octopus_merge = "…"
# octopus_rebase = "…"
```

Command placeholders (`{cmd}`, `{branch}`, `{repo}`, `{title}`, `{message}`,
`{path}`) are substituted per argument. Whole-argument values pass unchanged;
embedded values are shell-quoted. `{cmd}` is already a command line: use
`'wezterm start -- sh -c {cmd}'`, which passes it to a shell, rather than
`alacritty -e {cmd}`, which treats it as one word.

## State directory

`$XDG_STATE_HOME/project-sandbox-tui/` (default `~/.local/state/…`) holds:

| Location | Contents |
|---|---|
| `models.json` | Downloaded model catalog |
| `<repo>-<hash>/last_used.json` | Launch defaults |
| `<repo>-<hash>/prompt_history.json` | Prompt history |
| `<repo>-<hash>/trusted-project-config.toml` | Reviewed project config |
| `<repo>-<hash>/jobs/`, `<repo>-<hash>/jobs.lock` | Job records and cross-instance lock |
| `<repo>-<hash>/logs/` | Agent logs, transcripts, summaries and captured output |

Job records include full argv and prompts. Repository state, jobs and log
directories use mode `0700`, including directories created by earlier versions,
to keep them private to your user.
