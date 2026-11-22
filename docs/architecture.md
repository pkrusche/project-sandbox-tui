# Architecture

The Textual app calls `jj` and `project-sandbox` and reads session records from
disk. Project-sandbox manages containers, credentials and runtimes. Repositories
require a `.jj/` directory.

## Modules (`src/project_sandbox_tui/`)

| Module | Role |
|---|---|
| `cli.py`, `settings.py`, `doctor.py` | Entry point, config commands, dependency checks |
| `config.py` | Typed TOML loading, validation, trust, state paths and launch defaults |
| `catalogue.py`, `capabilities.py`, `security.py` | Provider and persona definitions, launcher capability discovery and security level flags |
| `vcs.py` | Bookmark discovery, creation, changed files, working-copy moves and fast-forwarding |
| `sandbox.py` | `LaunchSpec`, argv construction and session matching |
| `jobs.py` | Detached jobs, persistence, polling, adoption and stopping |
| `queue.py`, `screens/queue.py` | Persistent per-bookmark unsupervised queues, Vim editing and dispatch |
| `tasks.py` | `:&` / `:*` host commands: detached, output and exit status captured |
| `launchers.py`, `terminal.py` | External terminals and terminal restoration |
| `host_session.py` | External host agent runner: persistent PID and exit status |
| `octopus.py`, `cleanup.py` | Host-side integration and workspace cleanup |
| `reorganise.py` | `:reorganise` result names and pre-filled prompt |
| `workspace_skills.py` | Workspace creation and skill deployment before launches |
| `models.py`, `history.py`, `notify.py` | Suggestions, prompt and command history, notifications |
| `commands.py` | `:` command parsing (`:!`, `:&`, `:*`, `[actions]`), completion and leader-key mappings |
| `review.py` | Diff review: repository-wide per-commit and direct-comparison file marks |
| `commit_summary.py` | Isolated pinned-patch staging, summary launches and assessment validation |
| `opgraph.py` | Bookmark history graph, op log, undo stack and jj-style graph layout |
| `app.py`, `widgets.py`, `screens/` | Bookmark list, forms, dialogs, logs, op history and prompt search |
| `logview.py` | Log tail widget: wrapped rows, incremental Markdown blocks, line cursor |
| `screens/agent.py`, `screens/profiles.py` | Shared agent controls and the project profile editor |
| `screens/workflow.py` | Per-step profile picker shown before a workflow starts |

## Launches and job lifecycle

`sandbox.py` maps [configuration](configuration.md) to `--flag=value` arguments,
preventing leading `-` characters from becoming options. Bookmark launches add
`--branch`; background launches add `--prompt-text`, `--log`, `--json-summary`
and optional `--timeout`.

Launch forms resolve the selected persona and provider, named profile, or Custom
controls into a `LaunchSpec`. Queue and job records store the resolved agent,
model, effort, fast mode, gateway selection, security level and extra arguments,
along with the profile or persona and provider that supplied them. Dispatch uses
the saved settings. A task without a saved level takes the current project
default. Per-run extra arguments follow `sandbox.extra_args`.

1. **Guard:** defer queued background prompts while busy; reject interactive
   launches into busy workspaces. Then, still under `jobs.lock`,
   `JobManager.prepare` creates a missing workspace and deploys
   [workspace skills](configuration.md#workspace-skills); a configured skill that
   cannot be deployed stops the launch. Project-sandbox records in
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

Inline sessions reserve their workspace with a running job record and save
the final status when the terminal returns. Host agents use the same reservations. External host agents report
their runner PID and exit code in an atomic summary file, so they can be
tracked without project-sandbox session records. Job records keep only each
bookmark's latest run and archive replaced or cleared records in bounded history;
`opgraph.json` keeps bookmark runs as nodes of the [bookmark history](usage.md#bookmark-history),
finalised with the bookmark's commit on the first refresh that lists bookmarks
after the run ended. See [sessions](sessions.md) for terminal behavior and
[state storage](configuration.md#state-directory) for paths and permissions.

## Concurrency and locking

Diff review positions live in the app's memory, keyed by review type, baseline
and target. Commit reviews restore a commit ID and row; direct comparisons
restore only the row. Positions are local to the TUI instance, while seen marks
are persisted and shared through `diff_review.json`.

Commit summaries use an async worker with patch preparation off the UI thread.
The app tracks pending batches by their ordered full commit IDs and caches
assessments by full ID for the TUI session. `commit_summary.py` stages patches
using the same merged-parent baseline as file review and launches in an empty
temporary workspace, with staged inputs mounted read-only and feedback mounted
separately. Only final success feedback with a clean process exit supplies new
assessments. Temporary inputs are removed after completion, cancellation or
failure; exiting the app cancels and waits for summary workers.

Each TUI instance does its jj, file and process work in worker threads.
Instances opened from the same checkout share the file locks shown below.
See [cleanup from a second checkout](cleanup.md#cleanup-from-a-second-checkout)
for the limits of this protection across checkouts.

```text
  this TUI only       _history_lock (asyncio)    held for a whole action that
                              │                  changes the repository: n dd u
                              │                  ctrl+r f @ e, merge or rebase
                              │                  preparation, W removal
                              ▼
  every TUI in the    opgraph.json.lock          bookmark history and undo stack
  same checkout               │
  (flock on files             ▼
  in the state        jobs.lock                  job records, and the launch
  directory)                  │                  guard: busy checks and spawns
                              │                  happen under it
                              ▼
                      tasks.lock                 background task records
```

Nested locks follow this order; an action may take a lower lock alone.
The diff review, prompt history and command history
each have their own `<file>.lock`, taken alone. Queue edits take `queue.lock`
alone; dispatch takes `queue.lock` → `jobs.lock`, releasing both before
recording the run under the graph lock.

Which actions take which locks. *history* is `_history_lock`, *graph*
`opgraph.json.lock`; `A → B` takes B while holding A, `A, B` one after the other:

| Action | Locks | Done under them |
|---|---|---|
| Refresh (every `ui.refresh_seconds`) | graph, jobs, tasks, queue → jobs, then graph and dispatch when needed | Adopt bookmarks, poll jobs and tasks, capture finished runs, dispatch pending prompts and record new runs |
| `i` | jobs, graph | Recheck busy, prepare the workspace, reserve the session and add the run node |
| `r`, `:reorganise`, `:NAME` actions | queue; queue → jobs, graph | Save the prompt, launch when idle and add the run node. A new bookmark typed into the form is recorded first, under graph |
| `:workflow` | queued: as `r`; interactive: queue → jobs, graph | Refuse while another workflow is in progress, reserve the planner and its queue entry together, then add the run node |
| `L` edits / pause / resume | queue | Reload and atomically save pending tasks, history, marks and pause state |
| `I` | jobs, graph | Recheck busy, create a missing workspace and deploy skills, reserve the target before launching the host agent; add the run node |
| `r` / `i` with bookmarks selected | history → graph → jobs, then as `r` / `i` | Check the result bookmark is free, merge or rebase with jj, record the result bookmark. jj runs with `jobs.lock` held |
| `n` | history → graph | Create with jj, then record the creation and its undo entry |
| `dd` | history → graph → jobs | Busy check, delete, undo entry |
| `u` / `ctrl+r` | history → graph → jobs | Busy check, delete or restore, move the undo position |
| `f` | history → graph | Fast-forward main, undo entry |
| `@` | history → jobs | Busy check of the working copy (and of the target's workspace before starting a change there), move it |
| `e` | history | Describe, only if the target is still the commit that was edited |
| `:*` | jobs → tasks | Verify or create the workspace and register the task before cleanup can remove it |
| `:&` | tasks, jobs | Start one task in the repository under `@`; warn if a session is running there |
| `o` | jobs | Create a missing workspace unless a session is starting; deploy skills if idle |
| `x` | tasks, jobs, graph | Signal tasks; signal or forget the run; record a forgotten run's outcome |
| `W` | tasks; removal: history → graph → jobs → tasks | List without the launch guard; recheck each chosen workspace right before deleting it |
| `D` in the cleanup list | jobs → tasks | Recheck, then force-delete |
| `sbx-tui --cleanup-workspaces` | tasks; jobs → tasks | As `W`, without the bookmark history |

- Checks that guard an action are repeated under its lock with records and
  session files read again. The UI's cached state only decides what to offer.
- Inline sessions hold `jobs.lock` only while starting and finishing, not
  while the agent runs.
- Most readers take no lock. Files are replaced atomically, and a reload swaps
  the in-memory records in one assignment. Queue readers take `queue.lock` so
  they can recover interrupted multi-file transactions before reading.
- Locks are `flock`s on separately opened files, so they also order the threads
  of one TUI. They are not re-entrant: code holding one uses unlocked helpers
  such as `JobManager._busy_reason`.
- Merging or rebasing selected bookmarks holds `jobs.lock` while jj runs.
  Refreshes and launches from the same checkout wait until jj finishes.
- Workers that change the repository are not `exclusive`. A second action waits
  for `_history_lock` instead of cancelling the first after jj has already run.
- Launch defaults and host-command updates share the per-checkout `last_used.lock`.
  CLI settings edits, the TUI profile editor, initialization and trust approvals
  share `config.lock` in the application state directory. Project edits carry
  forward the trust status of the exact input they edited. An approval checks
  the trusted baseline again under the lock. A newer approval requires a fresh
  review of the diff.
- Initialization and host feedback installation share a per-workspace
  `guidance.lock`. Host installation takes it inside `jobs.lock`.

## Unsupervised queue dispatch

`queue.json` indexes pending and started task IDs, per-bookmark pause/error state,
named marks, and bounded pending-task undo/redo snapshots. Each task lives in
`queue-tasks/<id>.json`, with its launch spec, retained job record and pinned
starting/result revisions and commit IDs, including runs that start immediately
and dry-run previews. Previews are recorded directly in task history without
dispatching pending tasks or changing queue pause state. Mutations reload under
`queue.lock`.
A durable `queue-transaction.json` journal makes the per-task files and index
recoverable as one transaction; readers finish an interrupted write under the
same lock. Legacy inline task records migrate on the next mutation.
Dispatch holds that lock while
`JobManager.start_background` takes `jobs.lock`, so multiple instances cannot
claim the same task. No caller holds a graph, jobs or host-task lock when
acquiring the queue lock. Graph recording happens after releasing the queue lock.

The dispatched job stores the task's unique `queue_id`. Retrying after a crash
between saving the job and moving the queue entry to history finds that job in the
current record or retained job history instead of spawning it again. Consuming a task
also removes it from every undo/redo snapshot, preventing replay through undo.
A launch error pauses only that bookmark's queue. Corrupt queue state is reported
without overwriting the file.

The job captures its starting revision under the launch lock, after the busy
check. Queue refresh retains status and log paths and captures the finished
revision before dispatching the next run. It uses the graph's pinned outcome or,
while holding `jobs.lock`, a fresh bookmark tip if this is still the current job.
It never attributes a later job's tip to an earlier task. Completed tasks remain
until deleted; pending-task undo cannot restore or rerun them. Queue editors pause
dispatch persistently, including across TUI instances, until explicitly resumed.

A workflow's review step checks the current bookmark's per-commit marks under
`queue.lock` → `jobs.lock`. When a host command follows, dispatch keeps the
launch lock through checkout and spawn, taking `tasks.lock` to save the host
record. The review step's ID identifies that record so dispatch can adopt it if
the queue write fails. Host commands use their own temporary jj workspaces at
the reviewed commits. Termination is a queue transaction that consumes a
pending step or closes the active step's decision.

## Limitations

- Inline sessions, diffs and shell commands suspend the event loop;
  notifications arrive on return.
- External launches, stale records and PID reuse after crashes may need
  [manual recovery](sessions.md#interactive-sessions).
- Working-copy dirty markers, prompt files, remote bookmarks and switching
  repositories are not implemented.

## Agent feedback

`feedback.py` defines the strict run-report protocol and standalone helper;
`feedback_transport.py` stages per-run resources and uses existing sandbox bind
mounts. `jobs.py` collects and freezes reports separately from exit status.
Queue decisions commit successor IDs before dispatch using the existing queue
transaction journal. No queue/graph lock is acquired while collecting reports
under the jobs lock. See [agent feedback](feedback.md) for protocol and policies.
