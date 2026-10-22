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

   move               sessions                       branches            other
   j k   down / up    i    interactive               n  new branch       c C  clear / forget
   gg G  top / bottom r    unsupervised run          o  open in editor   H    hide this sheet
   ^d ^u half page    x    stop (again: escalate)    P  show command     ?    full help
   /     filter       l ⏎  log / transcript          R  refresh          q    quit
```

The welcome screen shows a key cheat-sheet under the list; `H` hides it (the
one-line key footer takes its place), and `ui.cheatsheet = false` starts with it
hidden.

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
| `i` | interactive session on the branch: inline (the TUI hands over this terminal and comes back when the agent exits) or in tmux / Zellij / screen, see [Interactive sessions](#interactive-sessions) |
| `r` | unsupervised run: prompt + optional timeout, runs in the background |
| `x` | stop a background run: SIGINT for a clean teardown; each further press escalates to SIGTERM, then SIGKILL plus `container stop` |
| `l` / `enter` | log view: transcript (`.md`), agent log, captured output (`tab` cycles, `f` toggles follow) |
| `n` | new branch / bookmark (name + start revision) |
| `v` · `space` | select branches: `v` starts a range at the cursor (`v` again keeps it), `space` marks one and moves on; `esc` clears. With two or more selected, `r` / `i` merge or rebase them, see [Merging and rebasing several branches](#merging-and-rebasing-several-branches) |
| `o` | open the branch's worktree in your editor |
| `P` | preview the full `project-sandbox` command |
| `c` · `C` | clear a finished status · force-forget a record stuck as running (signals nothing) |
| `R` · `?` · `q` | refresh · help · quit |
| `H` | show / hide the cheat-sheet |
| `ctrl+r` | reload global and project configuration |

In the launch form: `tab` (or `j`/`k` on choice fields) moves between fields,
`h`/`l` cycles agent and effort, `enter` launches an interactive session,
`ctrl+enter` launches an unsupervised run. In that form, `enter` inserts a newline
in the prompt or moves to the next field elsewhere. `esc` cancels.

### Prompt history

Prompts of unsupervised runs that started are remembered per repository
(`~/.local/state/project-sandbox-tui/<repo>-<hash>/prompt_history.json`, newest
500, duplicates collapsed). In the prompt box:

| key | action |
|---|---|
| typing | the newest earlier prompt that starts with what you typed appears as dim ghost text |
| `→` (at the end) · `ctrl+f` | take the suggestion |
| `ctrl+p` · `ctrl+n` | step to older / newer prompts starting with what you had typed; past the newest you're back to your text |
| `ctrl+r` | search the history: type words to filter (all must match, any case), `↑`/`↓` choose, `enter` use |
| `ctrl+l` | clear the prompt (it starts with the branch's previous prompt); `ctrl+z` undoes |

## One sandbox per branch

A branch counts as busy when either

- the TUI has a running job for it (records live in
  `~/.local/state/project-sandbox-tui/<repo>-<hash>/jobs/`, so a second TUI
  instance or a restarted one sees them), or
- project-sandbox has a `running` session record whose workspace is that
  branch's worktree, or the repository itself for the branch checked out
  there. This also catches sessions you started from a shell, as long as they
  used `--branch` or ran in the main checkout.

Sessions normally run in a project-sandbox worktree (`--branch`). git cannot
check a branch out twice, so the branch checked out in your main checkout runs
there instead, without `--branch` (the launch form says "in the main
checkout"). A branch checked out in some other worktree is refused until you
switch that worktree away from it. jj workspaces have no such restriction.

In a jj repository the first row is always `@ working copy`, followed by the
bookmarks. Sessions on `@` run in the repository's own workspace, on the
working-copy commit, without `--branch`. Sessions on a bookmark get their own jj
workspace as usual, so `@` and each bookmark are separate sandboxes. With no
bookmarks yet, `n` offers `@` as the start revision; otherwise it prefers
`main`/`master`/`trunk`, falling back to `@-`.

Background runs are detached from the TUI: quitting leaves them running, and the
next start re-adopts them and notifies about any that finished meanwhile.

## Cleaning up workspaces

```sh
sbx-tui --cleanup-workspaces --dry-run   # preview
sbx-tui --cleanup-workspaces             # remove idle clean / merged workspaces and branches
```

In the TUI, `W` lists every workspace and merged branch it found with its path
and status (the reason wraps onto its own line; long lists scroll). Removable
ones are preselected; the others are shown greyed out with the reason they are
kept. Toggle with `space` (`a` selects all / none), press `enter` to remove the
selection, `esc` to cancel. The CLI removes everything removable and prints the
same list: `removed` / `would remove` / `deleted branch` lines, plus a `kept`
line with the reason for each of the rest.

Workspaces come from the VCS itself (`git worktree list` / `jj workspace list`),
so worktrees of deleted branches, detached worktrees and jj workspaces in any
location or with any name are included. The workspace the TUI runs in is never
listed. Workspaces are always kept when they have a running TUI job or
project-sandbox session, uncommitted or untracked files (for jj: changes in `@`,
or files too large to snapshot), are locked, or hold the repository itself (jj's
`default` workspace).

The main branch (`project.main_branch`, default: the first of `main` / `master`
/ `trunk`) relaxes the rules for work that is fully merged:

- A workspace whose commits are all in the main branch (git: `HEAD` is an
  ancestor; jj: only an empty, undescribed `@` is not in `::main`) may also be
  removed when it only has ignored files (they are deleted) or when its directory
  is missing (it is just unregistered). Its branch / bookmark is deleted with it,
  except the main branch itself.
- Branches / bookmarks fully merged into the main branch that have no workspace
  are listed as rows of their own.

Unmerged workspaces with ignored files or a missing directory are kept. Removal
re-checks each selected row, unregisters workspaces (`git worktree remove` /
`jj workspace forget`), and deletes branches with `git update-ref -d` (only if the
branch has not moved) / `jj bookmark delete` (which also deletes a tracked remote
bookmark on the next `jj git push`). Job history is kept.

To get rid of a kept workspace anyway (e.g. a stale jj working copy), highlight
it and press `D`. After a confirmation this deletes its directory with everything
in it (uncommitted, untracked and ignored files included) and unregisters it
(`git worktree remove --force --force`, which also handles locked worktrees /
`jj workspace forget`). Branches and bookmarks are kept. Workspaces with a
running session and the repository itself (jj's `default` workspace) can never
be force-deleted, and for jj only a directory that really is a workspace of this
repository is deleted.

## Merging and rebasing several branches

Select two or more branches (`v` for a range, `space` for single ones; the order
you marked them is the merge / stacking order, a range adds in list order), then:

- `r`: the form gains **action** (`merge` / `rebase`, `h`/`l`), **result**
  (new branch, default `merge/<a>-<b>-…`) and **base** (rebase only, default
  main/master/trunk), and a prompt from a template that follows those fields
  until you edit it.
- `i`: the same, then an interactive session on the result to resolve conflicts
  yourself (or with the agent). Pick the `bash` agent if you want a plain shell.

On launch sbx-tui does the mechanical part on your machine, then starts the
session on the result branch, with the agent asked to resolve what is left
**and review the integration** (semantic conflicts, tests):

| | jj | git |
|---|---|---|
| merge | `jj new A B C` (conflicts are recorded in the commit) + bookmark `result` on it | worktree for `result` at `A`, octopus `git merge B C`; on conflicts one branch at a time, stopping at the first conflict |
| rebase | each bookmark's own commits `jj duplicate`d onto the previous tip, starting at `base` | worktree for `result` at `base`, `git cherry-pick` of each branch's own commits, stopping at the first conflict |

The inputs are never modified. A "Current state" section (what was merged,
which files or commits conflict, what is still to apply) is appended to the
prompt; for `i` it is shown before the session starts. Git commands run on
your machine with hooks disabled, because the sandbox can write to `.git`.
The templates can be replaced per repo in `[prompts]` (`octopus_merge`,
`octopus_rebase`; placeholders `{inputs}`, `{result}`, `{base}`, `{first}`,
`{rest}`).

## Interactive sessions

`ui.terminal` decides where `i` runs the agent:

| `ui.terminal` | inside the multiplexer | outside it |
|---|---|---|
| `inline` (default) | the TUI suspends, the agent takes over this terminal, you're back in the list when it exits | same |
| `tmux` | new tmux window named after the branch | detached session `sbx-<repo>-<branch>`: `tmux attach -t …` |
| `zellij` | new pane, closed when the agent exits | not supported (start sbx-tui inside Zellij) |
| `screen` | new screen window | detached session `sbx-<repo>-<branch>`: `screen -r …` |
| `custom` | `ui.terminal_command` with `{cmd}`, `{branch}`, `{repo}`, e.g. `'wezterm start -- sh -c {cmd}'` | same |

"Inside" means sbx-tui itself runs in that multiplexer (`$TMUX`, `$ZELLIJ`, `$STY`).
Everything except `inline` returns to the list straight away and the row shows
`interactive (ext)` while project-sandbox's session record for the branch is
running. Detach and reattach with the multiplexer's own keys. If project-sandbox
fails, its window stays open with the exit code until you press Enter, so the
error is readable. A setting with only `terminal_command` keeps meaning
`custom`. `x` cannot stop these sessions; quit the agent in its window.

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
Press `ctrl+r` in the branch list to reload both files without restarting.
Invalid configuration leaves the current settings in place. Running sessions keep
their launch settings; new launches use the reloaded configuration.

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
main_branch = ""                  # cleanup removes work merged into it ("" = main/master/trunk)

[ui]
notify_command = ""
terminal = "inline"     # inline | tmux | zellij | screen | custom (see Interactive sessions)
terminal_command = ""   # only with terminal = "custom", e.g. 'wezterm start -- sh -c {cmd}'
editor_command = ""     # GUI editor, e.g. 'code {path}'; "" = $EDITOR in this terminal
refresh_seconds = 3
bell = true
cheatsheet = true       # key cheat-sheet on the welcome screen (H toggles)

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
uv run pytest                 # unit, Textual Pilot and pty tests; jj tests skip if jj is missing
uv run ruff check .           # lint
uv run ruff format --check .  # formatting (drop --check to apply)
uv run ty check               # types (src and tests; warnings are errors)
```

CI (`.github/workflows/ci.yml`) runs the same checks on pushes to `main` and on
pull requests: a lint job (ruff check, ruff format, ty) and pytest on Linux with
Python 3.14 and jj installed (macOS and Python 3.12 are commented out in the
test matrix and can be switched back on). Dependencies are installed
with `uv sync --locked`, so commit `uv.lock` whenever `pyproject.toml` changes.

Tests use `tests/fake_project_sandbox.py`, a stand-in CLI that writes session
records, logs, a transcript and a JSON summary the way project-sandbox does.

See [PLAN.md](PLAN.md) for the design and remaining phases.

Press Ctrl, Shift, or Alt to see contextual shortcut hints when your terminal
reports standalone modifier keys.
