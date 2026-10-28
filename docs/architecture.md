# Architecture

The Textual app calls `jj` and the `project-sandbox` CLI; project-sandbox owns
container isolation, credentials and runtimes. Session records on disk are the
only other input. A `.jj/` directory is required.

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
| `models.py`, `history.py`, `notify.py` | Suggestions, prompt history and notifications |
| `app.py`, `widgets.py`, `screens/` | Bookmark list, forms, dialogs, logs and history |

## Launches and job lifecycle

`sandbox.py` maps [configuration](configuration.md) to project-sandbox flags,
passing values as `--flag=value` so leading `-` characters cannot become options.
Bookmark launches use `--branch`; working-copy launches omit it. Background
launches add `--prompt-text`, `--log`, `--json-summary` and an optional `--timeout`.

1. **Guard:** reject launches with an existing running job or session for the
   workspace. Project-sandbox records in `$XDG_STATE_HOME/project-sandbox/sessions/`
   have no branch name; match `workspace_path` using its slug rule
   (`/` → `-`, plus sha256[:6]).
2. **Spawn:** detach with `start_new_session=True`, capture output, and write a
   job record under a repository lock.
3. **Poll:** every `refresh_seconds`, finalise dead processes using the JSON
   summary's exit code, then the child's, else `orphaned`. A summary never closes
   a live process.
4. **Notify:** claim `notified` atomically so only one TUI instance announces completion.
5. **Restart:** re-adopt live processes and finalise dead ones.

Inline interactive sessions write temporary job records so other TUI instances
see the workspace as busy. See [sessions](sessions.md) for terminal modes and
[configuration](configuration.md#state-directory) for stored data and permissions.

## Limitations

- Inline sessions and the terminal diff viewer suspend the event loop;
  notifications arrive on return.
- External launches and stale records may need [manual recovery](sessions.md#interactive-sessions).
- PID reuse after a crash can make a record appear running; `C` forgets it.
- Ahead/behind counts, dirty markers, prompt files, remote bookmarks and switching
  repositories are not implemented.

For tooling and CI, see [development](development.md).
