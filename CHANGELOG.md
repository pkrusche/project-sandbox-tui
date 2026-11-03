# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

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

[Unreleased]: https://github.com/pkrusche/project-sandbox-tui/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/pkrusche/project-sandbox-tui/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/pkrusche/project-sandbox-tui/releases/tag/v0.1.0
