# project-sandbox-tui — implementation plan

A minimal vim/ranger-style terminal UI that wraps the `project-sandbox` CLI
(https://github.com/pkrusche/project-sandbox). It tracks the branches/bookmarks
of one repository, runs one sandbox per branch — either unsupervised in the
background (`--prompt-text`) or as an interactive terminal session — and
notifies you when background work finishes.

The TUI never talks to containers directly. Everything goes through the
`project-sandbox` CLI (`--branch`, `--prompt-text`, `--json-summary`,
`sessions list --json`), so the sandbox tool stays the single source of truth
for isolation, credentials and runtime handling.

---

## 1. Decisions

| Topic | Decision | Why |
|---|---|---|
| Package / module / command | `project-sandbox-tui` / `project_sandbox_tui` / `sbx-tui` | Mirrors `project-sandbox` naming; short command for daily use |
| Build tooling | `uv`, `pyproject.toml` (hatchling), Python ≥ 3.12 | Same as project-sandbox; `uv tool install project-sandbox-tui` |
| TUI framework | **Textual** | Async event loop (needed for watching background jobs), `App.suspend()` for handing the terminal to an interactive agent, easy custom keybindings, testable with `Pilot` |
| CLI parsing | `argparse` | House style |
| Config format | TOML, read with stdlib `tomllib`; never written back (templates via `--init`) | Readable, comment-friendly |
| VCS | git branches **and** jj bookmarks, auto-detected (`.jj/` → jj) | project-sandbox already does this for `--branch` |
| Process model | Background sessions are detached child processes (`start_new_session=True`) with a job record on disk | Jobs survive a TUI restart; TUI re-adopts them on launch |
| Notifications | Pluggable command; defaults: `osascript -e 'display notification …'` on macOS, `notify-send` on Linux; plus in-TUI status + terminal bell | No extra dependencies |
| Interactive sessions | Default: suspend the TUI and run `project-sandbox` in the same terminal; optional `terminal_command` template to launch in tmux / a new Terminal window | Simplest possible default, power-user escape hatch |

## 2. Config files

### Global: `~/.config/project-sandbox-tui/config.toml`

```toml
[defaults]
agent  = "claude"            # claude | codex | opencode | pi | bash
model  = ""                  # "" = agent default
effort = ""                  # "" = agent default, or low | medium | high | xhigh | max
timeout = 0                  # seconds for unsupervised runs; 0 = none

[network]
internet_proxy = "http://proxy.internal:3128"   # -> --internet-proxy
agent_proxy = ""                                # -> --agent-proxy (+ agent_proxy_key_env)
ca_certs = ["/path/to/corp-root.pem"]           # -> --ca-cert (repeatable)
extra_domains = []                              # -> --extra-domain (repeatable)
allow_github = false                            # -> --allow-github
no_firewall = false                             # -> --no-firewall

[sandbox]
command = "project-sandbox"  # or "uvx project-sandbox"
runtime = "auto"             # auto | apple-container | docker | podman
memory = ""                  # -> --memory
cpus = 0                     # -> --cpus
no_forward_credentials = false
api_key_env = []             # -> --api-key-env VAR (repeatable)
api_key_env_file = []        # -> --api-key-env-file PATH (repeatable)
extra_args = []              # appended verbatim

[ui]
notify_command = ""          # "" = platform default; supports {title} {message}
terminal = "inline"          # inline | tmux | zellij | screen | custom
terminal_command = ""        # with terminal = "custom"; supports {cmd} {branch} {repo}
editor_command = ""          # for `o` (open worktree), GUI editors; "" = $EDITOR in this terminal
refresh_seconds = 3
bell = true

[models]                     # autocomplete suggestions only
claude = ["opus", "sonnet", "haiku"]
```

### Per project: `<repo>/.project-sandbox-tui.toml`

Same schema, deep-merged on top of global. Holds the project-specific bits:

```toml
[project]
base_image = "python:3.12-slim"       # positional arg to project-sandbox
dockerfile = ""                       # -> --dockerfile (absolute, or relative to repo)
docker_context = ""
build = ""                            # python-uv | rust-cargo | node-npm (excludes base_image/dockerfile)
build_version = ""                    # -> --python / --rust for the generated build
worktree_dir = ""                     # -> --worktree-dir
keep_workspace = true                 # -> --keep-workspace (needed for reuse)
default_branch_start = ""             # start revision for `n` ("" = main/master/trunk, else HEAD / @-)

[defaults]
agent = "codex"                       # overrides global default for this repo
```

### State (not config): `~/.local/state/project-sandbox-tui/`

- `<repo-hash>/last_used.json` — last agent, effort, timeout and per-agent model
  of a launch that actually started in this repo.
  This is what "defaults that update" means: the launch dialog is pre-filled
  with the last-used values; the TOML defaults are only used when there is no
  history. The TUI never rewrites the user's TOML.
- `<repo-hash>/jobs/<branch-slug>.json` — one record per job (see §4).
- `<repo-hash>/jobs.lock` — advisory lock shared by TUI instances; launch checks
  and job-record changes are serialized, and refresh reloads records from disk.
- `<repo-hash>/logs/` — `--log`, transcript (`.md`), `--json-summary` and
  captured stdout of unsupervised runs.

Resolution order for the launch form: last_used → project config → global config → hard-coded.

## 3. Package layout

```
project-sandbox-tui/
├── pyproject.toml
├── README.md
├── PLAN.md
├── src/project_sandbox_tui/
│   ├── __init__.py
│   ├── __main__.py           # python -m project_sandbox_tui
│   ├── cli.py                # argparse: sbx-tui [REPO] [--config] [--init] [--print-config] [--dry-run]
│   ├── config.py             # dataclasses + TOML load/merge/validate, state dirs, last-used defaults
│   ├── vcs.py                # Branch model; git + jj backends (list, create, current, checkout path)
│   ├── sandbox.py            # LaunchSpec, argv builder, main-checkout rule, session records
│   ├── jobs.py               # JobRecord, JobManager: spawn detached, poll, adopt, stop, json-summary
│   ├── notify.py             # platform notification
│   ├── app.py                # Textual App + MainScreen (branch list, bindings, actions)
│   ├── widgets.py            # CycleField (h/l choice field)
│   └── screens/
│       ├── launch.py         # agent/model/effort (+ prompt, timeout) form with live command preview
│       ├── dialogs.py        # new branch, help, message/preview
│       └── log.py            # tail of a job's transcript / log / captured output
└── tests/
    ├── fake_project_sandbox.py  # stand-in CLI: session records, log, transcript, summary
    ├── test_cli.py
    ├── test_config.py
    ├── test_vcs.py           # against temp git and jj repos
    ├── test_sandbox.py       # golden argv, main-checkout rule, session matching
    ├── test_jobs.py          # lifecycle, adoption, stop escalation, external sessions
    └── test_app.py           # Textual Pilot: keybindings, launch forms, one-job-per-branch guard
```

### Key data models (`config.py`, `vcs.py`, `jobs.py`)

The authoritative definitions are `vcs.Branch`, `sandbox.LaunchSpec` and
`jobs.JobRecord`. In short:

- `Branch`: name, kind (git/jj), head, subject, timestamp, `is_current`
  (git: checked out in the repo; jj: the working-copy row, or a bookmark on `@` or `@-`),
  `working_copy` (jj's `@` row), and for git the
  `checkout_path` where it is checked out, if anywhere.
- `LaunchSpec`: branch, agent, model, effort, mode (interactive/background),
  prompt text, timeout, and `main_checkout` (run without `--branch`, see §4).
- `JobRecord`: the spec plus pid, argv, log/summary/stdout paths, status
  (running, completed, failed, rate_limited, timeout, interrupted, orphaned),
  exit code, session id, container name, observed runtime, `notified`, and for sessions opened
  via `terminal_command`: `external` and `session_seen`.

## 4. How a session runs

### Argv construction (`sandbox.py`)

```
project-sandbox <repo> <base_image | --dockerfile … | --python-uv|--rust-cargo|--node-npm>
  [--runtime <runtime>] [--memory …] [--cpus …]
  --branch <name> [--worktree-dir …] [--keep-workspace]   # omitted in the main checkout
  --agent <agent> [--model <model>] [--effort <effort>]
  [--internet-proxy <url>] [--agent-proxy <url>] [--ca-cert <pem>]… [--extra-domain …]…
  [--no-forward-credentials] [--api-key-env …]… [--api-key-env-file …]
  <extra_args…>
  # background only:
  --prompt-text "<text>" --log <log_path> --json-summary <summary_path> [--timeout N]
```

The launch form previews the command live, and `P` shows the full command for
the selected branch. `sbx-tui --dry-run` appends `--dry-run` to every call.

**Main checkout.** git cannot check a branch out twice, so `--branch` fails for
the branch checked out in the repository itself. Sessions on that branch run in
the main checkout, without `--branch` and the worktree flags (which
project-sandbox rejects without `--branch`). A branch checked out in
project-sandbox's own worktree is reused via `--branch`. A branch checked out in
any other worktree is refused with a message. jj workspaces are unaffected.

### Background (`jobs.py`)

1. Guard: refuse if a `JobRecord` with `status == running` exists for the
   branch, **or** a project-sandbox session record is `running` with that
   branch's worktree as its workspace (or the repo itself, for the branch
   checked out there). Records are read directly from
   `$XDG_STATE_HOME/project-sandbox/sessions/`; they carry no branch name.
2. `subprocess.Popen(argv, stdin=DEVNULL, stdout/stderr → log, start_new_session=True)`;
   write `JobRecord`.
3. Poll loop (Textual `set_interval`, `refresh_seconds`): for each running job
   check `os.kill(pid, 0)`; when gone, read `summary_path` → status from
   `exit_code` (0 → completed, 124 → timeout, 75 → rate_limited,
   130/-SIGINT → interrupted, else failed).
   A known child exit code is used if there is no summary; if neither is
   available and the process is gone → `orphaned`. A summary alone never closes
   a job whose process is still alive.
4. On terminal state: update row, atomically claim `notified`, fire
   `notify.send(...)` once across TUI instances.
5. On TUI start: load all `JobRecord`s, re-adopt live pids, finalize dead ones
   (and notify if `notified == False`).

### Interactive

- Default: `with app.suspend(): subprocess.run(argv)`; same one-per-branch
  guard; a lightweight `JobRecord` with `mode=interactive` is written for the
  duration so a second TUI instance also sees the branch as busy.
- With `ui.terminal` = tmux / zellij / screen / custom (`launchers.py`): tmux and
  screen open a window when sbx-tui runs inside them (`$TMUX`, `$STY`) and a
  detached `sbx-<repo>-<branch>` session otherwise; zellij opens a pane and needs
  sbx-tui inside Zellij (outside, `zellij run` exits 0 without doing anything).
  Multiplexer commands are run and their exit code checked; a custom
  `terminal_command` (`{cmd}`, `{branch}`, `{repo}`) is started detached. The
  agent runs via `sh -c` that waits for Enter after a failure. The row shows
  "interactive (ext)" until the session that appeared for the branch has ended,
  or for at most 10 minutes if none ever appears.

## 5. UI

Single-pane list, ranger-like, no mouse needed. Modal screens for forms.

```
 project-sandbox-tui   ~/workspace/foo  (jj)              3 branches · 1 running
 ────────────────────────────────────────────────────────────────────────────────
 ●  main            a1b2c3  Merge feature-x                     idle
 ▶  feature-x       d4e5f6  Add parser                          running  claude/opus/high  12m
 ✓  fix-tests       0f9e8d  Fix flaky test                      completed 2m ago  exit 0
 ✗  spike-ui        77aa11  Try textual                         failed    exit 1
 ────────────────────────────────────────────────────────────────────────────────
 j/k move  i interactive  r run prompt  n new  l log  o open  x stop  ? help
```

Keybindings (vim/ranger):

| Key | Action |
|---|---|
| `j` / `k`, `gg` / `G`, `ctrl-d` / `ctrl-u` | move / top / bottom / page |
| `/` then text | filter branches (fuzzy substring) |
| `Enter` or `l` | open log/transcript view for selected branch (`q` back) |
| `i` | **interactive** session on branch → launch form (agent/model/effort) |
| `r` | **run** unsupervised → launch form + multi-line prompt box |
| `n` | **new** branch: name, start revision (default `default_branch_start`), then optionally launch |
| `o` | open the branch's worktree in `editor_command` |
| `P` | preview the exact `project-sandbox` argv (dry run) |
| `x` | stop running job; each press escalates SIGINT → SIGTERM → SIGKILL (+ `container stop`) on the process group |
| `R` | force refresh branches + sessions |
| `c` / `C` | clear finished status / force-forget a record stuck as running |
| `?` | help overlay |
| `q` | quit; background runs keep going and are re-adopted next start |

Launch form fields: agent (cycle with `h`/`l`), model (text, with per-agent
suggestions), effort (cycle), prompt (textarea, `r` only), timeout. `Enter`
launches an interactive session; in the unsupervised form `Enter` moves to the
next field and `ctrl+enter` launches. `Esc` cancels. Pre-filled from last-used.

Status glyphs: `●` (after the name) current, `▶` running, `✓` completed,
`✗` failed, `⏱` timeout, `⚠` rate-limited/interrupted/orphaned, `◆` busy with a
session the TUI did not start, blank idle.

## 6. Implementation phases

Each phase ends with tests passing and a usable increment.

**Phase 0 — scaffold (½ day)**
- `pyproject.toml` (uv, hatchling, deps: `textual`, `tomli-w`; dev: `pytest`, `pytest-asyncio`, `ruff`, `mypy`).
- `cli.py`: `sbx-tui [REPO]` (default cwd), `--init` writes a commented per-project config, `--config PATH`.
- `config.py`: dataclasses, TOML load, deep merge, validation with clear errors, state dir helpers.
- Tests: config merge/precedence.

**Phase 1 — VCS + read-only list (1 day)**
- `vcs.py`: detect git vs jj; `list_branches()` via `git for-each-ref --format` / `jj bookmark list -T`; `current()`, `create(name, start)`, `workspace_path(name)` (matches project-sandbox's slug rule: `/` → `-` + 6 hex).
- `app.py` + `screens/branches.py`: list, navigation, filter, `?`, `q`, `R`.
- Tests against temporary git and jj repos.

**Phase 2 — launch interactive (1 day)**
- `sandbox.py` argv builder with golden tests.
- `screens/launch.py`, last-used persistence, `P` dry-run preview.
- `i` → suspend → run → resume, with the one-per-branch guard.
- `screens/new_branch.py` + `n`.

**Phase 3 — background jobs + notifications (1–2 days)**
- `jobs.py`: spawn detached, records, polling, adoption on restart, `x` stop.
- `--json-summary` parsing, exit-code → status mapping, cross-check with `sessions list --json`.
- `notify.py` (osascript / notify-send / custom template), in-app toast + bell.
- `screens/log.py`: tail of `.log` and the Markdown transcript, auto-follow.
- Tests with a fake `project-sandbox` shell script on `PATH` that sleeps, writes a summary and exits with a chosen code.

**Phase 4 — polish (½–1 day)**
- `o` open worktree, `terminal_command` external launch mode.
- Per-agent model suggestions list in config (`[models] claude = [...]`).
- Ahead/behind vs default branch in the row; `dirty` marker on the current branch.
- README with screenshots (Textual `textual run --screenshot`), `uv tool install` instructions.

## 7. Open questions / assumptions to confirm

1. **One-per-branch scope** — enforced per repo (a branch name in two repos is fine). Assumed yes.
2. **Interactive default** — suspend-in-place vs. always a new tmux window. Plan defaults to suspend; tmux via config.
3. **Prompt source for `r`** — inline textarea only, 
4. **jj bookmarks without a branch-like workflow** — list all bookmarks, or only local, non-remote ones? Plan: local only, remote-tracking hidden behind a toggle.
5. **Stopping sessions** — project-sandbox documents only `--timeout`; `x` will SIGTERM the process group and then `container stop <container_name>`/`docker stop` as a fallback. Worth adding a `sessions stop` subcommand upstream instead.
6. **Multiple repos** — out of scope for v1 (one TUI instance per repo).

---

## 8. Implementation status (2026-10-22)

Phases 0–3 are implemented, plus parts of phase 4 (`o` open worktree, `terminal_command`,
`[models]` suggestions). 127 tests pass: CLI, config, prompt history, git and jj VCS, argv golden tests, the
job lifecycle against `tests/fake_project_sandbox.py`, and Textual Pilot UI tests.
Sections 2–5 above describe the code as built, plus the additions below.

Additions (2026-10-22):

- **Cheat-sheet** on the welcome screen: a four-column key panel under the branch
  list that replaces the footer line. `H` toggles it; `ui.cheatsheet` sets the default.
- **jj working copy**: jj repos always list an `@ working copy` row first
  (`Branch.working_copy`), above the bookmarks. Sessions on it run in the repository's
  workspace without `--branch`, like the git main checkout, and project-sandbox sessions
  there are attributed to it; bookmarks keep their own workspaces. In a repo without
  bookmarks, `n` offers `@` as the start revision.
- **Prompt history** (`history.py`): per-repo JSON, newest last, deduplicated, capped at 500,
  written under a lock and re-read on every write so parallel TUIs merge. Recorded when
  an unsupervised run starts. The prompt box (`widgets.PromptArea`) offers fish-style ghost
  completion via Textual's `TextArea.suggestion` (hence `textual>=6.0`), `ctrl+p`/`ctrl+n`
  prefix-filtered browsing, and `ctrl+r` opens `screens/history.py`, an fzf-style search.

Review fixes (2026-10-22), verified with pytest, Ruff and strict mypy:

- Job records are synchronized across open TUI instances, with a repository lock
  around reservations and updates. Duplicate launches and completion notifications
  are guarded across instances, including external-terminal launches.
- Launch submission rechecks branch existence, git checkout location and external
  sessions. Failed external launches release their reservation; background launches
  are stopped if their job record cannot be persisted.
- Run output paths preserve dots in branch names and are unique for rapid reruns.
  Adopted jobs remain running until the process exits, even if a summary exists.
- Session ages accept ISO timestamps with fractional seconds and time-zone offsets.
  Forced container stops use the observed runtime, run detached and report spawn errors.
- Invalid command quoting, empty commands, negative CPU counts, non-finite refresh
  intervals and missing explicit config files produce configuration errors. Malformed
  job records are ignored, and CLI VCS failures are reported without a traceback.
- Git branch discovery handles matching tag names; branch validation rejects invalid
  path components. Log following handles split UTF-8, truncation and file replacement,
  bounds reads and retained lines, and half-page navigation follows the viewport size.

Changes from the original plan, made after reading the project-sandbox source:

- **Image source**: `project.build_flags` became `project.build = "python-uv" | "rust-cargo" |
  "node-npm"` plus `build_version`. These flags rule out a base image and each other.
- **Session ↔ branch matching**: project-sandbox session records have no `bookmark`
  field, so the TUI matches `workspace_path` against the worktree path, using the same
  `/` → `-` + sha256[:6] slug rule as `worktree.path_for` / `jj_workspace.path_for`. It
  reads the records directly from `$XDG_STATE_HOME/project-sandbox/sessions/` and does not
  spawn `sessions list --json` on every refresh.
- **Stop**: SIGINT first, because project-sandbox finalises on KeyboardInterrupt (it commits
  work to the branch and marks the record `interrupted`). Further presses of `x` escalate to
  SIGTERM, then SIGKILL plus `<runtime> stop <container>`.
- **Main checkout**: the branch checked out in a git repo runs without `--branch` (§4).
- **New branches** are created directly with `git branch` / `jj bookmark create -r`, not
  with `--branch-start-at`, so the branch exists before any session starts.
- **Extra network flags**: `agent_proxy`, `agent_proxy_key_env`, `extra_domains`,
  `allow_github` and `no_firewall`.
- **Models**: `[models]` only feeds autocomplete. A blank model means the agent's own default.
- **`--dry-run`** on `sbx-tui` adds `--dry-run` to every project-sandbox call.

Interactive-session fix (2026-10-22): after an interactive session the TUI ignored keys.
The Enter that submitted the launch form bubbled up to the App after the form was popped
and fired the branch list's Enter binding, so the session ended on an (empty) log screen.
The form now consumes that key, launches start via `call_after_refresh` once the form is
gone, `App.suspend()` is wrapped so an exception (e.g. project-sandbox missing) cannot skip
the resume, and `terminal.py` restores termios and O_NONBLOCK if the agent's
`container run -it` leaves the terminal raw or non-blocking. `tests/test_pty_session.py`
drives the real TUI in a pseudo-terminal to cover this.

Interactive terminals (2026-10-22): `ui.terminal` = inline | tmux | zellij | screen | custom
(`launchers.py`, see §4); tmux is covered by a test against a private tmux server.

Tooling (2026-10-22): ruff (lint + format) and ty replace mypy; ty is configured in
`pyproject.toml` (`[tool.ty]`, Python 3.12, `src` and `tests`, warnings are errors).
`.github/workflows/ci.yml` runs ruff check, ruff format --check and ty, plus pytest on
Linux with Python 3.14 and jj installed (macOS / 3.12 commented out in the matrix), all from `uv sync --locked`.

Known limitations / next steps:

- An interactive session suspends the TUI's event loop. Notifications for background runs
  that finish in the meantime arrive when you return.
- With `ui.terminal` other than inline, the TUI cannot see the launched process, only project-sandbox's
  session record. A launch that fails before writing one keeps the branch busy for up to
  10 minutes (or until `C`).
- PID reuse after a crash can make a record look running forever; `C` forgets it.
- Not yet implemented: ahead/behind vs trunk, a dirty marker, `--prompt FILE` / TODO-item
  prompts (to tie in with sbx-loop), remote bookmarks, and switching between repos.
- No licence has been chosen yet (no `LICENSE` file, no `license` field in `pyproject.toml`).
