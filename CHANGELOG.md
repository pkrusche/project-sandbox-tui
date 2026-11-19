# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

### Added

- `:init` runs the same setup as `sbx-tui --init` inside the TUI, shows the
  results and reloads configuration.
- Structured per-run agent feedback: atomic JSON progress/outcome reports, a
  standalone `sbx-report` helper, and a bundled reporting skill deployed through
  existing sandbox mounts and into host workspaces. Reports survive workspace
  deletion and remain distinct from process exit status.
- Retained feedback in task details and notifications, optional success-only
  queue progression, selected state handoff, and bounded retries/action routing
  with durable successor decisions.
- Reporting guidance in `--init` and read-only feedback diagnostics in `doctor`.
- Named project workflows can start with interactive or queued planning and
  advance through configured actions on success. Blocked reports guide users
  into an interactive session or workspace editor. The project config includes
  a spec-to-implementation workflow with checks and a structured commit summary.
  Each bookmark runs one workflow at a time.
- Agent actions accept `profile = "NAME"` to run with a saved agent profile,
  including its CLI args; it cannot be combined with the action's own `agent`,
  `model` or `effort`. Actions that set neither now use the profile the `i` / `r`
  launch forms would select (the first profile before any choice), including its
  CLI args or the remembered Custom CLI args. A missing profile warns and falls
  back to that choice, and `doctor` lists actions with unknown profiles.
- `:workflow` now shows a picker with a profile for the planning action and each
  step before starting; `enter` starts, `esc` cancels. Fields start at the
  action's profile or own agent settings, else the launch forms' profile, and
  the dry-run preview shows each step's profile.
- `:config-profiles` shows whether a profile comes from the project config, the
  global config or both, and `ctrl+d` deletes it from the file that defines it
  after confirmation.
- The bundled `sbx-tui-config` skill is copied into each bookmark workspace
  before `I`, `i`, `r`, agent actions and an idle `o` use it, and kept out of jj
  snapshots with in-tree ignore rules or the repository's local Git excludes.
  `project.deploy_config_skill = false` turns this off. Top-level
  `[[workspace_skills]]` entries deploy additional skill directories into
  `.agents/skills/` or `.claude/skills/` once the bookmark ignores them. Existing
  copies are kept; previews and dry runs list the destinations. See
  [workspace skills](docs/configuration.md#workspace-skills).

### Changed

- New workspaces are created before sandbox launches with
  `jj workspace add --no-colocate` and registered under their directory name, as
  project-sandbox does, including for slash-named bookmarks. A sandbox start
  reports a workspace registered under another name instead of reusing it.
- Workspace cleanup protects the directory holding the jj repository store,
  verifies that a directory is still the selected workspace before deleting it,
  holds the job and task locks through deletion, and deletes a bookmark only
  when it was selected and is still merged.
- `--init` installs guidance under a per-workspace lock and stops at symlinked
  guidance paths instead of writing through them.
- `scripts/make-release.sh` runs one release per checkout at a time and requires
  a jj working copy with a single parent.

### Fixed

- Concurrent TUIs and `sbx-tui config` edits no longer overwrite each other's
  launch defaults, host commands, or config and profile changes.
- Durable state files also sync their directory after replacement, so a completed
  write survives a crash.
- `scripts/e2e-test.sh` honors quoted `--command` values and failing version
  probes, and the source distribution includes the smoke-test prerequisites.

## [0.2.0] - 2026-11-16

### Added

- `:doctor` explains unavailable comparisons to main, including missing or
  conflicted bookmarks and the failing jj command's output. The CLI doctor also
  checks comparisons. Queue dispatch refreshes retain comparison values.
- Persistent per-bookmark queues for unsupervised prompts and configured agent
  actions. Busy bookmarks accept more prompts, executed in order when free.
  `L` opens the queue with Vim navigation, selection, editing, reordering,
  undo/redo and named marks; `p` pauses or resumes dispatch for that bookmark.
  Pending tasks resume when sbx-tui reopens; interactive sessions remain manual.
- Individual JSON records for queued tasks, retained after completion with status,
  logs and pinned commit links. In `L`, Enter opens a started task's log, `P` shows
  details, `c` reviews its commits and `dd` deletes a finished task.

### Changed

- Operation history opens with `O`; `L` opens the prompt queue.
- Editing the prompt queue automatically pauses dispatch for that bookmark;
  press `p` to resume after editing or cancelling the form.
- Every background prompt run appears in the task list, including immediate
  launches and dry-run previews.

## [0.1.3] - 2026-11-12

### Added

- Named agent profiles for interactive and background launches, with agent,
  model, effort, fast mode and extra project-sandbox arguments. Use
  `:config-profiles` to create or edit profiles in the project config; launch
  forms remember the last successfully used profile per repository and offer
  **custom** for individual settings. See
  [agent profiles](docs/configuration.md#agent-profiles).
- Diff review remembers the current commit and selected file row when returning
  to the bookmark list during the current TUI session. Direct comparisons also
  remember their selected row; reviews fall back to the first commit if the
  saved commit is no longer present.

### Changed

- Moving the working copy with `@` skips creating a `saved/…` bookmark when the
  previous change is already an ancestor of main. This respects the configured
  main bookmark or detects `main`, `master` or `trunk`; unbookmarked work is
  still saved when main is missing or conflicted.
- The release script keeps verified artifacts in `dist/` and removes completed
  release state after PyPI publication, while retaining unfinished releases.

### Fixed

- The bookmark table clamps horizontal scrolling when its contents shrink,
  keeping columns visible after refreshes.
- The CLI reports the installed package version instead of a hard-coded version;
  release verification now checks that it matches the release version.
- Moving the working copy correctly handles bookmark names in jj's template
  output.

## [0.1.2] - 2026-11-12

### Changed

- Seen marks are shared across bookmarks by commit ID and file change, including
  bookmarks created later. Existing saved marks are shared automatically; marking
  a file seen or unseen updates every bookmark containing that commit.
- Direct tip-to-tip comparisons share seen marks by file change across revisions,
  independently of per-commit marks and the bookmark list's reviewed count.

### Fixed

- Faster startup and bookmark refreshes: review counts read commit metadata in
  one query per distinct tip instead of generating full patches for every commit.
- Short diffs stay open in the pager, and closing the viewer returns directly
  to the file table without an Enter prompt. Diff viewers run in their own
  foreground process group; interrupting or stopping a viewer cleans up its
  processes and restores the terminal before resuming the TUI.

## [0.1.1] - 2026-11-03

### Added

- `I` launches an interactive agent directly on the host in each selected
  workspace, or in the current checkout for `@`, creating missing workspaces as
  `o` does. Host sessions reserve their workspace like sandbox sessions, appear
  in job history and notify on completion when run in an external terminal.
  See [host agent sessions](docs/sessions.md#host-agent-sessions).
- `:host AGENT [ARGS...]` runs a chosen host agent CLI with arguments in each
  target workspace; a successful launch becomes the command for later `I`
  launches, across restarts.
- `ui.host_agent_command` customizes the default host agent command, with
  `{agent}`, `{path}` and `{branch}` placeholders.
- Diff review (`=`) on a single bookmark or `@` now reviews each commit in
  `main..bookmark` separately, oldest first; `[` and `]` move between commits.
  Selecting two revisions keeps the direct tip-to-tip comparison.

### Changed

- `@ working copy` is no longer protected: `i`, `r` and configured agent actions
  run directly in the current checkout, including its local skills and other
  ignored files.
- Seen marks are stored per bookmark, commit and file change. The `reviewed`
  column counts file changes in commits outside main, so a file changed in two
  commits counts twice. Rewriting a commit makes its files unseen again.
- `host` is a reserved command name and cannot be used for a configured command.
- The README installs from PyPI by default.

## [0.1.0] - 2026-11-01

Initial release.

### Added

- vim/ranger-style terminal UI for running coding agents in
  [project-sandbox](https://github.com/pkrusche/project-sandbox) containers,
  with one sandbox and jj workspace per bookmark.
- Interactive sessions (`i`) and unsupervised background runs (`r`) with
  prompts, optional timeouts, model and effort selection, and completion
  notifications.
- Configurable terminals for interactive sessions: inline, tmux, zellij,
  screen or a custom command.
- Agent-assisted octopus merge and rebase of selected bookmarks.
- Bookmark history graph, op log, delete (`dd`) and undo (`u`).
- Diff review against main with per-file seen marks and a `reviewed` column.
- Command line with shell commands (`:!cmd`), configured commands, background
  tasks in the repository (`:&`) or in each selected workspace (`:*`),
  `[actions]` agent prompts and leader-key mappings.
- Workspace cleanup and a default `:abandon-empty` command.
- `sbx-tui --init` project detection and a local configuration skill,
  `sbx-tui config`, `sbx-tui doctor` and `--update-models`.
- Project config is only loaded after review.

[Unreleased]: https://github.com/pkrusche/project-sandbox-tui/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/pkrusche/project-sandbox-tui/compare/v0.1.3...v0.2.0
[0.1.3]: https://github.com/pkrusche/project-sandbox-tui/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/pkrusche/project-sandbox-tui/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/pkrusche/project-sandbox-tui/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/pkrusche/project-sandbox-tui/releases/tag/v0.1.0
