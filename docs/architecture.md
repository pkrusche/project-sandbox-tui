# Architecture

The Textual app calls `jj` and `project-sandbox` and reads session records from
disk. Project-sandbox manages containers, credentials and runtimes. Repositories
require a `.jj/` directory.

## Modules (`src/project_sandbox_tui/`)

| Module | Role |
|---|---|
| `cli.py`, `settings.py`, `doctor.py` | Entry point, config commands, dependency checks |
| `initialization.py`, `setup_wizard.py` | Repository initialization and interactive configuration interview |
| `config.py` | Typed TOML loading, validation, trust, state paths and launch defaults |
| `catalogue.py`, `capabilities.py`, `security.py` | Provider and persona definitions, launcher capability discovery and security level flags |
| `vcs.py` | Bookmark discovery, creation, changed files, working-copy moves and fast-forwarding |
| `sandbox.py` | `LaunchSpec`, argv construction and session matching |
| `jobs.py` | Detached jobs, persistence, polling, adoption and stopping |
| `queue.py` | Persistent per-bookmark unsupervised queues, Vim editing and dispatch |
| `tasks.py` | `:&` / `:*` host commands: detached, output and exit status captured |
| `launchers.py`, `terminal.py`, `processes.py` | External terminals, terminal restoration and child-process reaping |
| `feedback.py`, `feedback_host.py`, `feedback_transport.py` | Report protocol, host handoff and per-run resources |
| `host_session.py` | External host agent runner: persistent PID and exit status |
| `octopus.py`, `cleanup.py` | Host-side integration and workspace cleanup |
| `reorganise.py` | `:reorganise` result names and pre-filled prompt |
| `workspace_skills.py` | Workspace creation and skill deployment before launches |
| `models.py`, `history.py`, `notify.py` | Suggestions, prompt and command history, notifications |
| `commands.py` | `:` command parsing (`:!`, `:&`, `:*`, `[actions]`), completion and leader-key mappings |
| `review.py` | Diff review: repository-wide per-commit and direct-comparison file marks |
| `commit_summary.py` | Isolated pinned-patch staging, summary launches and assessment validation |
| `opgraph.py` | Bookmark history graph, op log, undo stack and jj-style graph layout |
| `app.py`, `widgets.py`, `screens/` | Bookmark list and screens, including shared provider and level fields in `screens/selection.py` |
| `logview.py` | Log tail widget: wrapped rows, incremental Markdown blocks, line cursor |

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

Repository-changing UI actions serialize within each instance. Across instances
in the same checkout, nested locks follow graph → jobs → tasks. Queue dispatch
takes queue → jobs and releases both before recording the run in the graph.
Independent history and review-mark updates use their own locks.
See [cross-checkout cleanup](cleanup.md#cleanup-from-a-second-checkout).

A launch checks availability and reserves the workspace while holding the jobs
lock. Queue decisions persist the successor ID before dispatch. Review and host
command launch share a lock, so a command runs at the approved revision.
[State storage](configuration.md#state-directory) and [workflow progression](feedback.md#project-workflows)
describe the persistent records and restart behavior.

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
