# Architecture

The TUI never talks to containers. Everything goes through the
`project-sandbox` CLI (`--branch`, `--prompt-text`, `--json-summary`) and `jj`,
so project-sandbox stays the source of truth for isolation, credentials and
runtimes. Its session records, read from disk, are the only other input.

## Decisions

| Topic | Decision |
|---|---|
| Names | package `project-sandbox-tui`, module `project_sandbox_tui`, command `sbx-tui` |
| Tooling | uv, hatchling, Python ≥ 3.12, Textual ≥ 6 (async loop, `App.suspend()`, Pilot tests) |
| Config | TOML via `tomllib`; never written back |
| VCS | jj only; a `.jj/` directory is required |
| Background jobs | detached child processes (`start_new_session=True`) with an on-disk job record; re-adopted on restart |
| Interactive | suspend the TUI and run in the same terminal, or hand off to tmux / zellij / screen / custom |

## Modules (`src/project_sandbox_tui/`)

| module | role |
|---|---|
| `cli.py` | argparse entry point |
| `config.py` | dataclasses, TOML load / merge / validate, state paths, last-used defaults |
| `vcs.py` | `Branch` model, jj queries, bookmark creation, diff |
| `sandbox.py` | `LaunchSpec`, argv builder, session-record matching |
| `jobs.py` | `JobRecord`, `JobManager`: spawn, poll, adopt, stop, summary parsing |
| `launchers.py`, `terminal.py` | external terminals; terminal state restore after inline sessions |
| `octopus.py` | host-side merge / rebase preparation |
| `cleanup.py` | workspace and merged-bookmark cleanup |
| `models.py`, `history.py`, `notify.py` | model suggestions, prompt history, notifications |
| `app.py`, `widgets.py`, `screens/` | Textual app, branch list, launch / dialog / log / diff / history screens |

## Argv construction

```
project-sandbox <repo> <base_image | --dockerfile … | --python-uv|--rust-cargo|--node-npm>
  [--runtime …] [--memory …] [--cpus …]
  --branch <name> [--worktree-dir …] [--keep-workspace]    # omitted for @
  --agent <agent> [--model …] [--effort …]
  [--internet-proxy …] [--agent-proxy …] [--ca-cert …]… [--extra-domain …]…
  [--no-forward-credentials] [--api-key-env …]… [--api-key-env-file …]
  <extra_args…>
  # background only:
  --prompt-text "<text>" --log <path> --json-summary <path> [--timeout N]
```

## Job lifecycle

1. **Guard**: refuse if a running `JobRecord` exists for the branch or a running
   project-sandbox session maps to its workspace. Session records are read from
   `$XDG_STATE_HOME/project-sandbox/sessions/`; they carry no branch name, so
   `workspace_path` is matched using project-sandbox's slug rule (`/` → `-` +
   sha256[:6]).
2. **Spawn** detached, output to the log, write the record under a repository lock.
3. **Poll** every `refresh_seconds`: when the pid is gone, status comes from the
   JSON summary's exit code (or the child's, else `orphaned`). A summary never
   closes a job whose process is alive.
4. **Notify** once across TUI instances (atomic `notified` claim).
5. **Restart**: load records, re-adopt live pids, finalise dead ones.

Interactive inline sessions write a lightweight record for their duration so
other TUI instances see the branch as busy. `x` sends SIGINT first so
project-sandbox can commit work and mark the session interrupted, then SIGTERM,
then SIGKILL plus `<runtime> stop <container>`.

## Known limitations

- An inline interactive session suspends the event loop; notifications arrive on return.
- With a non-inline terminal the TUI only sees project-sandbox's session record. A launch that fails before writing one keeps the branch busy up to 10 minutes (or until `C`).
- PID reuse after a crash can make a record look running forever; `C` forgets it.
- Not implemented: ahead/behind vs trunk, dirty marker, prompt files, remote bookmarks, switching repos.
- No licence chosen yet.
