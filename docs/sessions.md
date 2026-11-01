# Sessions

## One sandbox per bookmark

Bookmark sessions use separate jj workspaces.
A workspace is busy while any TUI instance or project-sandbox session uses it,
including sessions started from a shell.

`o` creates a missing workspace before opening it, named after the bookmark
at project-sandbox's location, on a new change on top of the bookmark; later
sessions reuse it with `keep_workspace`. Rows with a starting session wait for
the session's own workspace.

Select `@ working copy` to run directly in the TUI's checkout with `i`, `r`, or
a configured agent action. These sessions use the existing files, including
project-local skills and other ignored files that a new jj workspace would not
contain. They omit `--branch` and the workspace options, and share the same
busy-session checks as bookmark sessions.

The main bookmark is greyed out and protected from sessions, including
interactive runs, background runs and configured agent actions. Press `n` to
create a bookmark for changes, then start a session there. Main follows
`project.main_branch`, or the first existing bookmark main/master/trunk.

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

Each bookmark has one current job record. When a new run replaces it or `x`
clears it, the old record moves to the bookmark's job history in the
[state directory](configuration.md#state-directory), the last 50 per
bookmark. Those records keep the session id, container name, argv, exit code
and end time, to match a run with project-sandbox's session records and the
jj op log.

## Kept workspaces

Sessions reuse a bookmark's workspace when it is kept (`keep_workspace`, or
`--keep-workspace` on the command line). Beware:

- **Later sessions add to the same change.** project-sandbox records a
  session's edits in the workspace's current change and keeps that change's
  description. When the bookmark's work is already finished and described, a
  second session's edits join that commit rather than a new one. Start a new
  change in the workspace (`jj -R <workspace> new`) before reusing it for
  unrelated work, and check `jj evolog -r <bookmark>` if a commit grew
  unexpectedly.
- **Test sessions on real bookmarks.** Trying out launch options, e.g.
  `project-sandbox --branch=<bookmark> --keep-workspace --fast`, edits that
  bookmark like any other session. Use a throwaway bookmark for experiments.
- **Sessions started from a shell are not guarded by sbx-tui.** sbx-tui skips
  bookmarks that already have a session, including ones started from a shell
  once their project-sandbox session record exists. project-sandbox itself does
  not check sbx-tui's runs, so a session started from a shell on a bookmark that
  sbx-tui is running can share its workspace and both edit the same files.
  Check the bookmark's status column before starting one by hand.
- **Shell tasks do not wait for agents.** [`:&`](usage.md#background-commands)
  and [`:*` commands](usage.md#running-in-each-workspace) only warn when a
  session is running on the target. Tests run meanwhile may
  see half-made edits, and commands that write files change the session's work.

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
