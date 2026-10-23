# Sessions

## One sandbox per branch

A branch is busy when either

- the TUI has a running job for it (records in
  `~/.local/state/project-sandbox-tui/<repo>-<hash>/jobs/`, shared by all TUI
  instances), or
- project-sandbox has a `running` session record whose workspace is that
  bookmark's jj workspace (or the repository itself, for `@`). This also catches
  sessions started from a shell with `--branch`.

Sessions on `@` run in the repository's own workspace, without `--branch`.
Each bookmark gets its own jj workspace (`--branch`).

Background runs are detached: quitting leaves them running, and the next start
re-adopts them and notifies about any that finished meanwhile.

## Interactive sessions

`ui.terminal` decides where `i` runs the agent:

| `ui.terminal` | inside the multiplexer | outside it |
|---|---|---|
| `inline` (default) | TUI suspends, agent takes the terminal, you return to the list on exit | same |
| `tmux` | new window named after the branch | detached session `sbx-<repo>-<branch>` (`tmux attach -t …`) |
| `zellij` | new pane, closed on exit | not supported |
| `screen` | new window | detached session `sbx-<repo>-<branch>` (`screen -r …`) |
| `custom` | `ui.terminal_command` with `{cmd}`, `{branch}`, `{repo}`, e.g. `'wezterm start -- sh -c {cmd}'` | same |

"Inside" means sbx-tui itself runs in that multiplexer (`$TMUX`, `$ZELLIJ`,
`$STY`). Everything except `inline` returns to the list immediately; the row
shows `interactive (ext)` while project-sandbox's session record is running. If
project-sandbox fails, its window stays open until you press Enter. `x` cannot
stop these sessions; quit the agent in its window.

## Notifications

When a background run finishes the row updates, a toast appears, the terminal
bell rings (`ui.bell`) and a desktop notification is sent: `osascript` on
macOS, `notify-send` on Linux, or `ui.notify_command` (e.g.
`terminal-notifier -title {title} -message {message}`; placeholders are
substituted per argument, never through a shell).

## Exit codes

`0` completed · `124` timeout · `75` rate limited · `130` interrupted · anything
else failed. A vanished process with no summary is `orphaned`.
