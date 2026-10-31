# Sessions

## One sandbox per bookmark

Bookmark sessions use separate jj workspaces; `@` uses the repository itself.
A workspace is busy while any TUI instance or project-sandbox session uses it,
including sessions started from a shell.

Background jobs survive quitting; restarting re-adopts them and announces any
that finished. Quitting (`q` or `ctrl+q`) while jobs or sessions are running
asks for confirmation; `y` quits, and `ctrl+q` again over the dialog also quits.

`x` cancels a run in three steps, one per press:

1. SIGINT, so project-sandbox can commit work and close its session.
2. SIGTERM, if the run is still going.
3. SIGKILL plus runtime `stop`; the TUI forgets the record.

On a finished run, `x` clears the status at once. Presses within a second count
once. When the first press would signal several selected runs, sbx-tui lists
them and asks first; later presses escalate without asking. The [bookmark history](usage.md#bookmark-history) keeps cleared runs.

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
(`x` cannot signal them). Failed launches leave the window open until Enter.

If an exited agent's row stays busy, press `x` three times to forget its TUI
record, then `i` to restart. Interactive records are forgotten without sending a
signal. Launches that fail before writing a session record may stay busy for 10
minutes unless cleared.

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
