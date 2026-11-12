# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

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

[Unreleased]: https://github.com/pkrusche/project-sandbox-tui/compare/v0.1.3...HEAD
[0.1.3]: https://github.com/pkrusche/project-sandbox-tui/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/pkrusche/project-sandbox-tui/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/pkrusche/project-sandbox-tui/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/pkrusche/project-sandbox-tui/releases/tag/v0.1.0
