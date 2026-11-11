# Architecture

The Textual app calls `jj` and `project-sandbox` and reads session records from
disk. Project-sandbox manages containers, credentials and runtimes. Repositories
require a `.jj/` directory.

## Modules (`src/project_sandbox_tui/`)

| Module | Role |
|---|---|
| `cli.py`, `settings.py`, `doctor.py` | Entry point, config commands, dependency checks |
| `config.py` | Typed TOML loading, validation, trust, state paths and launch defaults |
| `vcs.py` | Bookmark discovery, creation, changed files, working-copy moves and fast-forwarding |
| `sandbox.py` | `LaunchSpec`, argv construction and session matching |
| `jobs.py` | Detached jobs, persistence, polling, adoption and stopping |
| `tasks.py` | `:&` / `:*` host commands: detached, output and exit status captured |
| `launchers.py`, `terminal.py` | External terminals and terminal restoration |
| `host_session.py` | External host agent runner: persistent PID and exit status |
| `octopus.py`, `cleanup.py` | Host-side integration and workspace cleanup |
| `models.py`, `history.py`, `notify.py` | Suggestions, prompt and command history, notifications |
| `commands.py` | `:` command parsing (`:!`, `:&`, `:*`, `[actions]`), completion and leader-key mappings |
| `review.py` | Diff review: repository-wide per-commit and direct-comparison file marks |
| `opgraph.py` | Bookmark history graph, op log, undo stack and jj-style graph layout |
| `app.py`, `widgets.py`, `screens/` | Bookmark list, forms, dialogs, logs, op history and prompt search |

## Launches and job lifecycle

`sandbox.py` maps [configuration](configuration.md) to `--flag=value` arguments,
preventing leading `-` characters from becoming options. Bookmark launches add
`--branch`; background launches add `--prompt-text`, `--log`, `--json-summary`
and optional `--timeout`.

1. **Guard:** reject busy workspaces. Project-sandbox records in
   `$XDG_STATE_HOME/project-sandbox/sessions/` lack branch names; match
   `workspace_path` using its slug rule
   (`/` → `-`, plus sha256[:6]).
2. **Spawn:** detach with `start_new_session=True`, capture output and write a
   job record under a repository lock.
3. **Poll:** every `refresh_seconds`, finalise dead processes using the summary's
   exit code, then the child's, else `orphaned`. A summary never closes
   a live process.
4. **Notify:** claim `notified` atomically so only one instance announces completion.
5. **Restart:** adopt live processes; finalise dead ones.

Detached helpers (notifications, editors, terminal launchers and container stop
commands) are reaped by daemon waiters. Forgotten or replaced job handles are
handed to the same waiters so their children are collected even without another
refresh. Waiters do not stop live processes or delay quitting the TUI.

Inline sessions use temporary job records to mark workspaces busy across
instances. Host agents use the same reservations. External host agents report
their runner PID and exit code in an atomic summary file, so they can be
tracked without project-sandbox session records. Job records keep only each
bookmark's latest run; `opgraph.json`
keeps every run as a node of the [bookmark history](usage.md#bookmark-history),
finalised with the bookmark's commit on the first refresh that lists bookmarks
after the run ended. See [sessions](sessions.md) for terminal behavior and
[state storage](configuration.md#state-directory) for paths and permissions.

## Concurrency and locking

Several TUI instances may run on one repository, and each does its jj, file and
process work in worker threads. These locks order what they do:

```text
  this TUI only       _history_lock (asyncio)    held for a whole action that
                              │                  changes the repository: n dd u
                              │                  ctrl+r f @ e, merge or rebase
                              │                  preparation, W removal
                              ▼
  every TUI on the    opgraph.json.lock          bookmark history and undo stack
  repository                  │
  (flock on files             ▼
  in the state        jobs.lock                  job records, and the launch
  directory)                  │                  guard: busy checks and spawns
                              │                  happen under it
                              ▼
                      tasks.lock                 background task records
```

A lock is only taken while holding the ones above it, never the reverse, so
actions cannot deadlock. The diff review, prompt history and command history
each have their own `<file>.lock`, taken alone.

Which actions take which locks. *history* is `_history_lock`, *graph*
`opgraph.json.lock`; `A → B` takes B while holding A, `A, B` one after the other:

| Action | Locks | Done under them |
|---|---|---|
| Refresh (every `ui.refresh_seconds`) | graph, jobs, tasks | Adopt new bookmarks and record finished runs; poll jobs and claim their notifications; the same for tasks |
| `r` / `i`, `:NAME` actions | jobs, graph | Recheck busy, spawn and write the job record; add the run node. A new bookmark typed into the form is recorded first, under graph |
| `I` | jobs, graph | Create a missing workspace; recheck busy and reserve the target before launching the host agent; add the run node |
| `r` / `i` with bookmarks selected | history → (jobs, graph); then as `r` / `i` | Check the result bookmark is free, merge or rebase with jj, record the result bookmark |
| `n` | history → graph | Create with jj, then record the creation and its undo entry |
| `dd` | history → graph → jobs | Busy check, delete, undo entry |
| `u` / `ctrl+r` | history → graph → jobs | Busy check, delete or restore, move the undo position |
| `f` | history → graph | Fast-forward main, undo entry |
| `@` | history → jobs | Busy check of the working copy (and of the target's workspace before starting a change there), move it |
| `e` | history | Describe, only if the target is still the commit that was edited |
| `:*` | jobs, tasks | Create a missing workspace unless a session is starting; start the task |
| `:&` | jobs, tasks | Start one task in the repository under `@`; warn if a session is running there |
| `o` | jobs | Create a missing workspace unless a session is starting |
| `x` | tasks, jobs, graph | Signal tasks; signal or forget the run; record a forgotten run's outcome |
| `W` | tasks; removal: history → graph → jobs → tasks | List without the launch guard; recheck each chosen workspace right before deleting it |
| `D` in the cleanup list | jobs → tasks | Recheck, then force-delete |
| `sbx-tui --cleanup-workspaces` | tasks; jobs → tasks | As `W`, without the bookmark history |

- Checks that guard an action are repeated under its lock with records and
  session files read again. The UI's cached state only decides what to offer.
- Inline sessions hold `jobs.lock` only while starting and finishing, not
  while the agent runs.
- Readers take no lock. Files are replaced atomically, and a reload swaps the
  in-memory records in one assignment, so readers see the old state or the new.
- Locks are `flock`s on separately opened files, so they also order the threads
  of one TUI. They are not re-entrant: code holding one uses unlocked helpers
  such as `JobManager._busy_reason`.
- Workers that change the repository are not `exclusive`. A second action waits
  for `_history_lock` instead of cancelling the first after jj has already run.
- `last_used.json` and the trusted project config are replaced atomically
  without a lock; the last write wins.

## Limitations

- Inline sessions, diffs and shell commands suspend the event loop;
  notifications arrive on return.
- External launches, stale records and PID reuse after crashes may need
  [manual recovery](sessions.md#interactive-sessions).
- Working-copy dirty markers, prompt files, remote bookmarks and switching
  repositories are not implemented.
