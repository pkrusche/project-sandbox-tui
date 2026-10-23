# Configuration

Global: `~/.config/project-sandbox-tui/config.toml`. Per repo:
`<repo>/.project-sandbox-tui.toml`, deep-merged on top. Unknown keys are errors.
`sbx-tui --init` writes templates; the TUI never writes your TOML.
`ctrl+r` reloads both files; invalid config leaves the current settings in
place, and running sessions keep their launch settings.

## From the command line

```sh
sbx-tui config list                     # settings in effect (global + trusted project)
sbx-tui config list --project           # what one file sets (--global / --project)
sbx-tui config get defaults.agent
sbx-tui config set --global defaults.agent codex
sbx-tui config set --project project.build python-uv
sbx-tui config set --global network.extra_domains '["pypi.org", "files.pythonhosted.org"]'
sbx-tui config set --global models.claude '["opus", "sonnet"]'
sbx-tui config unset --project defaults.effort
sbx-tui config path --global
```

Keys are `section.key` as in the TOML below. Values take the key's type
(`true`/`false`, numbers, lists as TOML arrays). Edits keep comments and are
validated together with the other file first; an invalid change is not
written. Editing a trusted (or new) project config keeps it trusted; editing
one with unreviewed changes leaves it untrusted.

## Trusting the project config

The project config comes with the repository, so whoever wrote it can set
commands that run on your machine (`sandbox.command`, `ui.*_command`) or loosen
the sandbox (network, credentials, `extra_args`). sbx-tui therefore only loads
a copy you have reviewed, kept in the state directory
(`~/.local/state/project-sandbox-tui/<repo>-<hash>/trusted-project-config.toml`):

- When the file is new or has changed, sbx-tui shows it as a diff against the
  trusted copy at start and on `ctrl+r`; `y` trusts exactly what was shown.
- Until then a never-trusted file is ignored, and a changed one keeps the
  previously trusted version in effect. A file that fails to load is not offered.
- `sbx-tui --trust-config` does the same review in the terminal;
  `--print-config` and `--cleanup-workspaces` warn about an untrusted file.
- `sbx-tui --init` trusts the template it writes.

Launch form precedence: last used → project config → global config → built-in.

```toml
[defaults]
agent = "claude"        # claude | codex | opencode | pi | bash
model = ""              # "" = the agent's own default
effort = ""             # "" or low | medium | high | xhigh | max
timeout = 0             # seconds for unsupervised runs, 0 = none

[network]
internet_proxy = "http://proxy.example:3128"   # --internet-proxy
agent_proxy = ""                               # --agent-proxy
agent_proxy_key_env = ""
ca_certs = ["/path/to/corp-root.pem"]          # --ca-cert …
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
# exactly one image source:
base_image = "python:3.12-slim"   # or dockerfile = "Dockerfile" (+ docker_context), or build = "python-uv" | "rust-cargo" | "node-npm"
build_version = ""                # --python / --rust for a generated build
worktree_dir = ""                 # default: sibling <repo>-workspaces
keep_workspace = true             # reuse the workspace across runs
default_branch_start = ""         # start revision for new bookmarks: "" = main/master/trunk, else @-
main_branch = ""                  # what cleanup treats as merged ("" = main/master/trunk)

[ui]
notify_command = ""     # "" = platform default; {title} {message}
terminal = "inline"     # inline | tmux | zellij | screen | custom
terminal_command = ""   # with terminal = "custom"; {cmd} {branch} {repo}
editor_command = ""     # GUI editor, e.g. 'code {path}'; "" = $EDITOR in this terminal
refresh_seconds = 3
bell = true
cheatsheet = true       # key cheat-sheet on the welcome screen (H toggles)

[models]                # extra launch-form suggestions, listed first
claude = ["opus", "sonnet"]
codex = ["gpt-5.5"]

[prompts]               # merge/rebase templates, see merging.md
# octopus_merge = "…"
# octopus_rebase = "…"
```

Command placeholders (`{cmd}`, `{branch}`, `{repo}`, `{title}`, `{message}`,
`{path}`): a placeholder that is a whole argument is passed as that argument,
unchanged. Inside a longer argument, such as the script of
`sh -c "cd {repo} && {cmd}"`, it is shell-quoted, because branch names can
contain `$`, `;` or backticks. `{cmd}` is already a shell command line, so
`'wezterm start -- sh -c {cmd}'` works, while `alacritty -e {cmd}` would pass
it as a single word.

## State directory

`~/.local/state/project-sandbox-tui/` holds, per repository
(`<repo>-<hash>/`): `last_used.json`, `prompt_history.json`, `jobs/` (one record
per job), `jobs.lock` (shared by TUI instances) and `logs/` (agent log,
transcript, JSON summary and captured output). `models.json` sits at the top.
