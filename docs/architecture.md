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
| `queue.py`, `screens/queue.py` | Persistent per-bookmark unsupervised queues, Vim editing and dispatch |
| `tasks.py` | `:&` / `:*` host commands: detached, output and exit status captured |
| `launchers.py`, `terminal.py` | External terminals and terminal restoration |
| `host_session.py` | External host agent runner: persistent PID and exit status |
| `octopus.py`, `cleanup.py` | Host-side integration and workspace cleanup |
| `models.py`, `history.py`, `notify.py` | Suggestions, prompt and command history, notifications |
| `commands.py` | `:` command parsing (`:!`, `:&`, `:*`, `[actions]`), completion and leader-key mappings |
| `review.py` | Diff review: repository-wide per-commit and direct-comparison file marks |
| `opgraph.py` | Bookmark history graph, op log, undo stack and jj-style graph layout |
| `app.py`, `widgets.py`, `screens/` | Bookmark list, forms, dialogs, logs, op history and prompt search |
| `screens/agent.py`, `screens/profiles.py` | Shared agent controls and the project profile editor |

## Launches and job lifecycle

`sandbox.py` maps [configuration](configuration.md) to `--flag=value` arguments,
preventing leading `-` characters from becoming options. Bookmark launches add
`--branch`; background launches add `--prompt-text`, `--log`, `--json-summary`
and optional `--timeout`.

Launch forms resolve a named profile or Custom controls into a `LaunchSpec`
containing the profile name, agent, model, effort, fast mode and extra arguments.
These values are stored in the job record as a snapshot. Argument construction
uses that snapshot, without looking up the profile again; per-run extra arguments
follow `sandbox.extra_args`.

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

Detached jobs, host tasks and helpers (notifications, editors, terminal launchers
and container stop commands) get daemon waiters at launch, so their children are
collected even without another refresh or after their manager is discarded.
Waiters do not stop live processes or delay quitting the TUI.

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

Diff review positions live in the app's memory, keyed by review type, baseline
and target. Commit reviews restore a commit ID and row; direct comparisons
restore only the row. Positions are local to the TUI instance, while seen marks
are persisted and shared through `diff_review.json`.

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
each have their own `<file>.lock`, taken alone. Queue edits take `queue.lock`
alone; dispatch takes `queue.lock` → `jobs.lock`, releasing both before
recording the run under the graph lock.

Which actions take which locks. *history* is `_history_lock`, *graph*
`opgraph.json.lock`; `A → B` takes B while holding A, `A, B` one after the other:

| Action | Locks | Done under them |
|---|---|---|
| Refresh (every `ui.refresh_seconds`) | graph, jobs, tasks, graph, queue → jobs, graph | Adopt bookmarks, poll jobs and tasks, capture finished runs, dispatch pending prompts and record new runs |
| `i` | jobs, graph | Recheck busy, reserve the session and add the run node |
| `r`, `:NAME` actions | queue; queue → jobs, graph | Save the prompt, launch when idle and add the run node. A new bookmark typed into the form is recorded first, under graph |
| `L` edits / pause / resume | queue | Reload and atomically save pending tasks, history, marks and pause state |
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

## Unsupervised queue dispatch

`queue.json` holds pending launch snapshots, per-bookmark pause/error state,
named marks, and bounded undo/redo snapshots. Mutations reload under
`queue.lock` and replace the file atomically. Dispatch holds that lock while
`JobManager.start_background` takes `jobs.lock`, so multiple instances cannot
claim the same task. No caller holds a graph, jobs or host-task lock when
acquiring the queue lock. Graph recording happens after releasing the queue lock.

The dispatched job stores the task's unique `queue_id`. Retrying after a crash
between saving the job and removing the queue entry finds that job in the current
record or retained job history instead of spawning it again. Consuming a task
also removes it from every undo/redo snapshot, preventing replay through undo.
A launch error pauses only that bookmark's queue. Corrupt queue state is reported
without overwriting the file.

Refresh polls jobs, captures finished runs in the bookmark history, then starts
at most one queued task for each idle bookmark. Pending prompts and pause state
survive TUI restarts; dispatch needs a live, refreshing TUI instance.

## Limitations

- Inline sessions, diffs and shell commands suspend the event loop;
  notifications arrive on return.
- External launches, stale records and PID reuse after crashes may need
  [manual recovery](sessions.md#interactive-sessions).
- Working-copy dirty markers, prompt files, remote bookmarks and switching
  repositories are not implemented.
