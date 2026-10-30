# Sessions

## One sandbox per bookmark

Bookmark sessions use separate jj workspaces; `@` uses the repository itself.
A workspace is busy while any TUI instance or project-sandbox session uses it,
including sessions started from a shell.

Background jobs survive quitting; restarting re-adopts them and announces any
that finished. Quitting (`q` or `ctrl+q`) while jobs or sessions are running
asks for confirmation; `y` quits, and `ctrl+q` again over the dialog also quits. `x` sends SIGINT so project-sandbox can commit work; repeated
presses escalate to SIGTERM, then SIGKILL plus runtime `stop`.

## Interactive sessions

`ui.terminal` controls where `i` opens the agent. Inline sessions suspend the TUI
until the agent exits; multiplexer modes fall back to inline outside that multiplexer.

| Mode | Agent location |
|---|---|
| `inline` (default) | Current terminal |
| `tmux` | New window named after the bookmark |
| `zellij` | New pane, closed on exit |
| `screen` | New window |
| `custom` | `ui.terminal_command`; see [command placeholders](configuration.md#settings-reference) |

Multiplexers are detected through `$TMUX`, `$ZELLIJ` and `$STY`. External sessions
show `interactive (ext)` and return immediately; stop them in the agent window
(`x` cannot). Failed launches leave the window open until Enter.

If an exited agent's row stays busy, press `C` (`Shift+c`) to forget its TUI record,
then `i` to restart. `C` sends no signal; `c` clears only finished records. Launches
that fail before writing a session record may stay busy for 10 minutes unless cleared.

## Notifications and exit codes

Completion updates the row, shows a toast, rings the bell (`ui.bell`) and sends
a desktop notification via `osascript` (macOS), `notify-send` (Linux) or
`ui.notify_command`.

| Exit code | Status |
|---|---|
| `0` | Completed |
| `124` | Timeout |
| `75` | Rate limited |
| `130` | Interrupted |
| Other | Failed |
| Vanished process, no summary or exit code | Orphaned |
