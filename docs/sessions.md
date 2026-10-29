# Sessions

## One sandbox per bookmark

Bookmark sessions use a separate jj workspace (`--branch`); `@` sessions use the
repository itself. A workspace is busy if any TUI instance has a running job for
it, or project-sandbox has a matching running session, including one started
from a shell.

Background jobs survive quitting the TUI. On restart it re-adopts them and
announces any that finished meanwhile. `x` sends SIGINT so project-sandbox can
commit work; further presses escalate to SIGTERM, then SIGKILL plus runtime
`stop`.

## Interactive sessions

`ui.terminal` controls where `i` opens the agent:

| Mode | Inside that multiplexer | Outside it |
|---|---|---|
| `inline` (default) | TUI suspends until the agent exits | Same |
| `tmux` | New window named after the bookmark | TUI suspends until the agent exits |
| `zellij` | New pane, closed on exit | TUI suspends until the agent exits |
| `screen` | New window | TUI suspends until the agent exits |
| `custom` | `ui.terminal_command`, e.g. `'wezterm start -- sh -c {cmd}'` | Same |

Multiplexers are detected through `$TMUX`, `$ZELLIJ` and `$STY`. External modes
return immediately and show `interactive (ext)` while the session runs. A failed
launch leaves its window open until Enter. Stop external sessions in their agent
window; `x` cannot stop them.

If the agent has exited but the row remains busy, press `C` (`Shift+c`) to forget
its TUI record, then `i` to restart. `C` sends no signal; lowercase `c` clears
only finished records. A launch that fails before writing a session record may
stay busy for up to 10 minutes unless cleared with `C`.

## Notifications and exit codes

Completion updates the row, shows a toast, rings the bell (`ui.bell`) and sends
a desktop notification via `osascript` (macOS), `notify-send` (Linux) or
`ui.notify_command`. See [configuration](configuration.md) for custom commands.

| Exit code | Status |
|---|---|
| `0` | Completed |
| `124` | Timeout |
| `75` | Rate limited |
| `130` | Interrupted |
| Other | Failed |
| Vanished process, no summary or exit code | Orphaned |
