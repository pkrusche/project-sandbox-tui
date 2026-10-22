# project-sandbox-tui

A minimal vim/ranger-style terminal UI for
[project-sandbox](https://github.com/pkrusche/project-sandbox). It lists the
branches (git) or bookmarks (jj) of a repository and runs **one sandbox per
branch**, either interactively in your terminal or unsupervised in the
background with a prompt, and notifies you when background work is done.

```
 sbx-tui   ~/workspace/myproject  (git)   4 branches · 1 running
   branch            head      subject                       status       agent              when
   main ●            5cf8bea   Initial import
 ▶ feature/parser    c692d0c   Add streaming parser          running      claude/opus/high   12m
 ✓ fix/flaky-tests   8643f47   Stabilise integration tests   completed    codex/gpt-5.4      2m ago
 ✗ spike/tui         a1e2e23   Try a ranger-style layout     failed (1)   claude/sonnet      1h ago
 j/k move · i interactive · r run · n new · l log · x stop · o open · ? help · q quit
```

The TUI only ever shells out to the `project-sandbox` CLI (`--branch`,
`--agent/--model/--effort`, `--prompt-text --log --json-summary`). Isolation,
credentials and worktree teardown stay in project-sandbox.

## Install

```sh
uv tool install project-sandbox          # the sandbox itself (macOS: Apple container)
uv tool install /path/to/project-sandbox-tui
# or, from this checkout:
uv sync && uv run sbx-tui --help
```

## Quick start

```sh
cd ~/workspace/myproject
sbx-tui --init           # writes ~/.config/project-sandbox-tui/config.toml (if missing)
                         # and ./.project-sandbox-tui.toml — set the image source there
sbx-tui --print-config   # merged config + the command an interactive session would run
sbx-tui --dry-run        # try the UI; every project-sandbox call gets --dry-run
sbx-tui                  # the real thing
```

## Keys

| key | action |
|---|---|
| `j` `k` · `gg` `G` · `ctrl+d` `ctrl+u` | move · top/bottom · half page |
| `/` | filter branches (`enter` keeps it, `esc` clears) |
| `i` | interactive session on the branch (the TUI suspends; you're back when the agent exits) |
| `r` | unsupervised run: prompt + optional timeout, runs in the background |
| `x` | stop a background run (SIGINT for a clean teardown; press again to force-kill and stop the container) |
| `l` / `enter` | log view: transcript (`.md`), agent log, captured output (`tab` cycles, `f` toggles follow) |
| `n` | new branch / bookmark (name + start revision) |
| `o` | open the branch's worktree in your editor |
| `P` | preview the full `project-sandbox` command |
| `c` | clear a finished status · `R` refresh · `?` help · `q` quit |

In the launch form: `tab` (or `j`/`k` on choice fields) moves between fields,
`h`/`l` cycles agent and effort, `enter` launches an interactive session,
`ctrl+s` launches an unsupervised run, `esc` cancels.

## One sandbox per branch

A branch counts as busy when either

- the TUI has a running job for it (records live in
  `~/.local/state/project-sandbox-tui/<repo>-<hash>/jobs/`, so a second TUI
  instance or a restarted one sees them), or
- project-sandbox has a `running` session record whose workspace is that
  branch's worktree — this also catches sessions you started from a shell.

Background runs are detached from the TUI: quitting leaves them running, and the
next start re-adopts them and notifies about any that finished meanwhile.

## Notifications

When a background run finishes the row updates, a toast appears, the terminal
bell rings (`ui.bell`) and a desktop notification is sent: `osascript` on macOS,
`notify-send` on Linux, or your own `ui.notify_command`
(e.g. `terminal-notifier -title {title} -message {message}`; placeholders are
substituted per argument, never through a shell).

Exit codes map to status: `0` completed, `124` timeout, `75` rate limited,
`130` interrupted, anything else failed.

## Configuration

Global `~/.config/project-sandbox-tui/config.toml`, overridden per repo by
`.project-sandbox-tui.toml` (deep merge). Unknown keys are errors.

```toml
[defaults]
agent = "claude"        # claude | codex | opencode | pi | bash
model = ""              # "" = the agent's own default
effort = ""             # "" or low | medium | high | xhigh | max
timeout = 0             # seconds for unsupervised runs

[network]
internet_proxy = "http://proxy.example:3128"   # --internet-proxy
agent_proxy = ""                               # --agent-proxy
ca_certs = ["/path/to/corp-root.pem"]          # --ca-cert …
extra_domains = []                             # --extra-domain …
allow_github = false

[sandbox]
command = "project-sandbox"   # or "uvx project-sandbox"
runtime = "auto"              # auto | apple-container | docker | podman
memory = ""                   # e.g. "16g"
cpus = 0
no_forward_credentials = false
api_key_env = []
api_key_env_file = []
extra_args = []

[project]                     # usually in .project-sandbox-tui.toml
base_image = "python:3.12-slim"   # or: dockerfile = "Dockerfile" / build = "python-uv"
build_version = ""                # --python / --rust for a generated build
worktree_dir = ""
keep_workspace = true             # reuse the worktree across runs
default_branch_start = ""         # "" = main/master/trunk, else HEAD (git) / @- (jj)

[ui]
notify_command = ""
terminal_command = ""   # run interactive sessions elsewhere, e.g. 'tmux new-window -n {branch} {cmd}'
editor_command = ""     # GUI editor, e.g. 'code {path}'; "" = $EDITOR in this terminal
refresh_seconds = 3
bell = true

[models]                # autocomplete suggestions in the launch form
claude = ["opus", "sonnet", "haiku"]
```

**Defaults that update:** the launch form is pre-filled with the agent, effort,
timeout and per-agent model you used last in this repository (stored in the
state directory, never written back to your TOML). Config defaults apply until
you've launched something.

## Development

```sh
uv sync
uv run pytest            # unit + Textual Pilot tests; jj tests skip if jj is missing
uv run ruff check . && uv run mypy src
```

Tests use `tests/fake_project_sandbox.py`, a stand-in CLI that writes session
records, logs, a transcript and a JSON summary the way project-sandbox does.

See [PLAN.md](PLAN.md) for the design and remaining phases.
