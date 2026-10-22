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
| Config format | TOML (`tomllib` stdlib for reading, `tomli-w` for writing) | Readable, comment-friendly |
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
effort = "medium"            # low | medium | high | xhigh | max
runtime = "auto"             # auto | apple-container | docker | podman

[network]
internet_proxy = "http://proxy.internal:3128"   # -> --internet-proxy
ca_certs = ["/path/to/corp-root.pem"]           # -> --ca-cert (repeatable)

[sandbox]
# any extra args always appended to project-sandbox
extra_args = ["--memory", "16g"]
no_forward_credentials = false
api_key_env = []             # -> --api-key-env VAR (repeatable)
api_key_env_file = ""        # -> --api-key-env-file PATH

[ui]
notify_command = ""          # "" = platform default; supports {title} {message}
terminal_command = ""        # "" = suspend TUI; e.g. 'tmux new-window -n {branch} {cmd}'
editor_command = ""          # for `e` (open worktree); "" = $EDITOR
refresh_seconds = 5
```

### Per project: `<repo>/.project-sandbox-tui.toml`

Same schema, deep-merged on top of global. Holds the project-specific bits:

```toml
[project]
base_image = "python:3.12-slim"       # positional arg to project-sandbox
dockerfile = ""                       # -> --dockerfile (absolute, or relative to repo)
docker_context = ""
build_flags = ["--python-uv"]         # --node-npm / --python-uv / --rust-cargo
worktree_dir = ""                     # -> --worktree-dir
keep_workspace = true                 # -> --keep-workspace (needed for reuse)
default_branch_start = "main"         # -> --branch-start-at for new branches

[defaults]
agent = "codex"                       # overrides global default for this repo
```

### State (not config): `~/.local/state/project-sandbox-tui/`

- `<repo-hash>/last_used.json` — last agent/model/effort chosen in this repo.
  This is what "defaults that update" means: the launch dialog is pre-filled
  with the last-used values; the TOML defaults are only used when there is no
  history. The TUI never rewrites the user's TOML.
- `<repo-hash>/jobs/<branch-slug>.json` — one record per background job
  (see §4).

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
│   ├── cli.py                # argparse: sbx-tui [REPO] [--config] [--init] [--dry-run]
│   ├── config.py             # dataclasses + TOML load/merge/validate, state dir paths
│   ├── vcs.py                # Branch model; git + jj backends (list, create, current, ahead/behind, dirty)
│   ├── sandbox.py            # build project-sandbox argv from config + LaunchSpec; parse sessions list --json
│   ├── jobs.py               # JobRecord, JobManager: spawn detached, poll, adopt, read json-summary
│   ├── notify.py             # platform notification + in-app
│   ├── app.py                # Textual App, global bindings, mode handling
│   ├── screens/
│   │   ├── branches.py       # main list view
│   │   ├── launch.py         # agent/model/effort/prompt form
│   │   ├── new_branch.py     # name + start revision
│   │   ├── log.py            # tail of a job's log / transcript
│   │   └── help.py           # keybinding cheatsheet (?)
│   └── widgets/
│       ├── branch_row.py
│       └── statusbar.py
└── tests/
    ├── test_config.py
    ├── test_vcs.py           # against temp git and jj repos
    ├── test_sandbox_argv.py  # golden argv strings
    ├── test_jobs.py          # fake `project-sandbox` script on PATH
    └── test_app.py           # Textual Pilot: keybindings, one-job-per-branch guard
```

### Key data models (`config.py`, `vcs.py`, `jobs.py`)

```python
@dataclass
class Branch:
    name: str              # git branch or jj bookmark
    kind: Literal["git", "jj"]
    is_current: bool
    head: str              # short commit / change id
    subject: str           # last commit message, one line
    dirty: bool            # only meaningful for current branch
    workspace_path: Path | None   # existing worktree/workspace, if any

@dataclass
class LaunchSpec:
    branch: str
    agent: str
    model: str
    effort: str
    mode: Literal["interactive", "background"]
    prompt_text: str | None       # background only
    timeout_s: int | None

@dataclass
class JobRecord:                  # jobs/<branch-slug>.json
    branch: str
    spec: LaunchSpec
    pid: int
    started_at: str
    log_path: Path
    summary_path: Path             # --json-summary target
    status: Literal["running", "completed", "failed", "rate_limited", "timeout", "orphaned"]
    exit_code: int | None
    session_id: str | None
    container_name: str | None
    notified: bool
```

## 4. How a session runs

### Argv construction (`sandbox.py`)

```
project-sandbox <repo> <base_image | --dockerfile …>
  --runtime <runtime> [build_flags…]
  --branch <name> [--branch-start-at <rev>]  [--keep-workspace] [--worktree-dir …]
  --agent <agent> [--model <model>] [--effort <effort>]
  [--internet-proxy <url>] [--ca-cert <pem>]…
  [--no-forward-credentials] [--api-key-env …]… [--api-key-env-file …]
  <extra_args…>
  # background only:
  --prompt-text "<text>" --log <log_path> --json-summary <summary_path> [--timeout N]
```

A `--dry-run` preview (`P` key) shows the exact command before anything runs.

### Background (`jobs.py`)

1. Guard: refuse if a `JobRecord` with `status == running` exists for the
   branch, **or** `project-sandbox sessions list --json` shows a `running`
   record with that `bookmark` (covers sessions started outside the TUI).
2. `subprocess.Popen(argv, stdin=DEVNULL, stdout/stderr → log, start_new_session=True)`;
   write `JobRecord`.
3. Poll loop (Textual `set_interval`, `refresh_seconds`): for each running job
   check `os.kill(pid, 0)`; when gone, read `summary_path` → status from
   `exit_code` (0 → completed, 124 → timeout, 75 → rate_limited, else failed).
   If no summary and process gone → `orphaned`.
4. On terminal state: update row, fire `notify.send(...)` once, set `notified`.
5. On TUI start: load all `JobRecord`s, re-adopt live pids, finalize dead ones
   (and notify if `notified == False`).

### Interactive

- Default: `with app.suspend(): subprocess.run(argv)`; same one-per-branch
  guard; a lightweight `JobRecord` with `mode=interactive` is written for the
  duration so a second TUI instance also sees the branch as busy.
- With `ui.terminal_command`: render the template (`{cmd}`, `{branch}`,
  `{repo}`) and `Popen` it detached; row shows "interactive (external)" until
  the `sessions list` record for that bookmark leaves `running`.

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
| `x` | stop running job (SIGTERM to the process group; falls back to `container stop <container_name>`) |
| `R` | force refresh branches + sessions |
| `c` | clear finished status for the row |
| `?` | help overlay |
| `q` | quit (asks for confirmation only if interactive jobs are attached to this terminal) |

Launch form fields: agent (cycle with `h`/`l`), model (text, with per-agent
suggestions), effort (cycle), prompt (textarea, `r` only), timeout. `Enter`
launches, `Esc` cancels. Pre-filled from last-used.

Status glyphs: `●` current, `▶` running, `✓` completed, `✗` failed,
`⏱` timeout, `⚠` rate-limited/orphaned, blank idle.

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
`[models]` suggestions). 52 tests pass: config, git and jj VCS, argv golden tests, the job
lifecycle against `tests/fake_project_sandbox.py`, and Textual Pilot UI tests.

Changes from the plan, made after reading the project-sandbox source:

- **Image source**: `project.build_flags` became `project.build = "python-uv" | "rust-cargo" |
  "node-npm"` plus `build_version`. These flags rule out a base image and each other.
- **Session ↔ branch matching**: project-sandbox session records have no `bookmark`
  field, so the TUI matches `workspace_path` against the worktree path, using the same
  `/` → `-` + sha256[:6] slug rule as `worktree.path_for` / `jj_workspace.path_for`. It
  reads the records directly from `$XDG_STATE_HOME/project-sandbox/sessions/` and does not
  spawn `sessions list --json` on every refresh.
- **Stop**: SIGINT first, because project-sandbox finalises on KeyboardInterrupt (it commits
  work to the branch and marks the record `interrupted`). Pressing `x` again sends SIGKILL to
  the process group and runs `<runtime> stop <container>`.
- **New branches** are created directly with `git branch` / `jj bookmark create -r`, not
  with `--branch-start-at`, so the branch exists before any session starts.
- **Extra network flags**: `agent_proxy`, `agent_proxy_key_env`, `extra_domains`,
  `allow_github` and `no_firewall`.
- **Models**: `[models]` only feeds autocomplete. A blank model means the agent's own default.
- **`--dry-run`** on `sbx-tui` adds `--dry-run` to every project-sandbox call.

Known limitations / next steps:

- An interactive session suspends the TUI's event loop. Notifications for background runs
  that finish in the meantime arrive when you return.
- With `terminal_command`, the branch counts as busy only while a project-sandbox session
  record is `running`, plus a 10-minute grace period that covers the image build.
- Not yet implemented: ahead/behind vs trunk, a dirty marker, `--prompt FILE` / TODO-item
  prompts (to tie in with sbx-loop), remote bookmarks, and switching between repos.
