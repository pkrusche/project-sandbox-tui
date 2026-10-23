# Sessions

## One sandbox per bookmark

A bookmark or the working copy is busy when either

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
| `tmux` | new window named after the bookmark | detached session `sbx-<repo>-<branch>` (`tmux attach -t …`) |
| `zellij` | new pane, closed on exit | not supported |
| `screen` | new window | detached session `sbx-<repo>-<branch>` (`screen -r …`) |
| `custom` | `ui.terminal_command` with `{cmd}`, `{branch}`, `{repo}`, e.g. `'wezterm start -- sh -c {cmd}'` | same |

"Inside" means sbx-tui itself runs in that multiplexer (`$TMUX`, `$ZELLIJ`,
`$STY`). Everything except `inline` returns to the list immediately; the row
shows `interactive (ext)` while project-sandbox's session record is running. If
project-sandbox fails, its window stays open until you press Enter. `x` cannot
stop these sessions; quit the agent in its window.

If a session in a separate Zellij / tmux window dies but the bookmark still shows
`interactive (ext)` and blocks another launch, confirm the old session has
exited, select the bookmark and press `C` (`Shift+c`) to forget its TUI job record.
Then press `i` to restart. `C` sends no signal and does not stop any process;
lowercase `c` only clears finished records.

The TUI polls project-sandbox's session records on each refresh. If a launch
fails before the TUI sees a running session, the bookmark can stay busy for up to
10 minutes; `C` clears the TUI job record immediately.

## Notifications

When a background run finishes the row updates, a toast appears, the terminal
bell rings (`ui.bell`) and a desktop notification is sent: `osascript` on
macOS, `notify-send` on Linux, or `ui.notify_command` (e.g.
`terminal-notifier -title {title} -message {message}`; placeholders are
substituted per argument, never through a shell).

## Exit codes

`0` completed · `124` timeout · `75` rate limited · `130` interrupted · anything
else failed. A vanished process with no summary is `orphaned`.
