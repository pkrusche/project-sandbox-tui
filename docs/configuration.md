# Configuration

Global: `~/.config/project-sandbox-tui/config.toml`. Per repo:
`<repo>/.project-sandbox-tui.toml`, deep-merged on top. Unknown keys are errors.
`sbx-tui --init` writes templates; the TUI never writes your TOML.
`ctrl+r` reloads both files; invalid config leaves the current settings in
place, and running sessions keep their launch settings.

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

## State directory

`~/.local/state/project-sandbox-tui/` holds, per repository
(`<repo>-<hash>/`): `last_used.json`, `prompt_history.json`, `jobs/` (one record
per job), `jobs.lock` (shared by TUI instances) and `logs/` (agent log,
transcript, JSON summary and captured output). `models.json` sits at the top.
