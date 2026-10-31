# Architecture

The Textual app calls `jj` and `project-sandbox` and reads session records from
disk. Project-sandbox manages containers, credentials and runtimes. Repositories
require a `.jj/` directory.

## Modules (`src/project_sandbox_tui/`)

| Module | Role |
|---|---|
| `cli.py`, `settings.py`, `doctor.py` | Entry point, config commands, dependency checks |
| `config.py` | Typed TOML loading, validation, trust, state paths and launch defaults |
| `vcs.py` | Bookmark discovery, creation, working-copy moves and fast-forwarding |
| `sandbox.py` | `LaunchSpec`, argv construction and session matching |
| `jobs.py` | Detached jobs, persistence, polling, adoption and stopping |
| `launchers.py`, `terminal.py` | External terminals and terminal restoration |
| `octopus.py`, `cleanup.py` | Host-side integration and workspace cleanup |
| `models.py`, `history.py`, `notify.py` | Suggestions, prompt and command history, notifications |
| `commands.py` | `:` command parsing, completion and leader-key mappings |
| `opgraph.py` | Bookmark history graph, undo stack and jj-style graph layout |
| `app.py`, `widgets.py`, `screens/` | Bookmark list, forms, dialogs, logs and history |

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

Inline sessions use temporary job records to mark workspaces busy across
instances. Job records keep only each bookmark's latest run; `opgraph.json`
keeps every run as a node of the [bookmark history](usage.md#bookmark-history),
finalised with the bookmark's commit on the first refresh that lists bookmarks
after the run ended. See [sessions](sessions.md) for terminal behavior and
[state storage](configuration.md#state-directory) for paths and permissions.

## Limitations

- Inline sessions, diffs and shell commands suspend the event loop;
  notifications arrive on return.
- External launches, stale records and PID reuse after crashes may need
  [manual recovery](sessions.md#interactive-sessions).
- Working-copy dirty markers, prompt files, remote bookmarks and switching
  repositories are not implemented.
