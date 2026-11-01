# Usage and keys

## Command line

```sh
sbx-tui [REPO]               # open the TUI; default: current directory
sbx-tui --init               # detect project type; initialize config, setup skill and model catalog
sbx-tui --print-config       # show effective config and an interactive command
sbx-tui --trust-config       # review changed project config
sbx-tui --dry-run            # preview sandbox calls or workspace cleanup
sbx-tui --update-models      # refresh model suggestions
sbx-tui --cleanup-workspaces # remove eligible workspaces and bookmarks
sbx-tui doctor               # check dependencies and runtime
sbx-tui config …             # read or edit settings
```

Subcommands accept `-R/--repository PATH` and `--config PATH`. Open repositories
named `config` or `doctor` as `./config` or `./doctor`.

`doctor` checks jj (tested minimum 0.46), project-sandbox ≥ 0.2.2 and runtime
health; failed checks exit 1. It also warns about untrusted config and missing
image sources, multiplexers or notifiers. Automatic runtime selection tries
Apple `container`, Docker, then Podman.

See [configuration](configuration.md), [models](models.md) and
[cleanup](cleanup.md) for the corresponding commands.

## Bookmark list

`@ working copy` appears first, then bookmarks in [history](#bookmark-history)
order. The cheat-sheet collapses to one line in small terminals. In narrow ones
the header shows the repository's name instead of its path, then abbreviated
counts, and the subject column narrows to keep the status in view.

The `vs main` heading names the baseline: `project.main_branch`, or the first
existing bookmark main/master/trunk; below 80 columns it is just `main`. Counts
omit zeros:

| Symbol | Meaning |
|---|---|
| `↑N` / `↓N` | Commits only in this revision / main; both appear for diverged histories |
| `ΔN` | Files differing between tips, as with `=` |
| `=` | Matching tips |
| `?` | Comparison unavailable, e.g. missing main or conflicted bookmark |

`@` includes edits captured on refresh; its commit counts toward `↑N` even if empty.

The `reviewed` column (`seen` in narrow terminals) shows reviewed files out of
the total differing from main, for example `2/5`. It is blank when there are no
diffs or the comparison is unavailable. Counts follow the saved marks from
[diff review](#diff-review) and update when a file's diff changes.

| Key | Action |
|---|---|
| `j` `k` · `gg` `G` · `ctrl+d` `ctrl+u` | Move · top/bottom · half page |
| `/` | Filter (`enter` keeps it, `esc` clears) |
| `:` · `\` | [Command line](#command-line) (`:!cmd` runs a shell command) · [leader key](#leader-key) |
| `i` · `r` | [Interactive session](sessions.md#interactive-sessions) · background run with prompt and optional timeout; asks first on the main bookmark |
| `x` | [Cancel a run](sessions.md#one-sandbox-per-bookmark) or running [`:*` tasks](#running-in-each-workspace): repeat to cancel harder, a third time to forget a run; clears a finished status at once; asks first before signalling several |
| `l` / `enter` | [Logs](#log-viewer) and the latest `:*` output, or run details while a bookmark has neither |
| `P` | Last run details (agent, times, IDs, log, command line), history, recent `:*` tasks and the interactive command |
| `L` | [Op history](#op-history): every prompt, session, merge and rebase as a graph |
| `T` | [Tasks](#tasks): every `:*` task in all workspaces |
| `n` | New bookmark: name and start revision |
| `e` | Edit the full description of one bookmark or the working copy (`ctrl+s` saves, `esc` cancels) |
| `dd` · `u` · `ctrl+r` | Delete bookmark, keeping its history · [undo · redo](#bookmark-history) |
| `v` / `V` · `space` | Select a range / mark individual rows, including `@`; `esc` clears |
| `=` | [Review the diff](#diff-review) against main, or between two selected revisions |
| `f` | Fast-forward main to highlighted bookmark or `@` (`u` undoes) |
| `@` | Start a working-copy change on highlighted bookmark |
| `o` | Open workspace in editor, first creating a missing one on the bookmark (`jj workspace add`); asks before opening more than three |
| `W` | [Clean up workspaces](cleanup.md) |
| `R` · `ctrl+l` | Refresh list · reload configuration |
| `H` · `?` · `q` | Toggle cheat-sheet · full help · quit (background runs continue; asks first while any are running) |

Selections apply to `x`, `dd`, `o`, `P`, [`:*`](#running-in-each-workspace) and
[agent actions](#agent-actions); `l` and `e` require one bookmark.
One marked bookmark overrides the highlighted row for `f`, `@`, `r` and `i`.
With at least two selected, `r` / `i` opens [merge or rebase](merging.md).
The working copy `@` can be marked too: merges and rebases leave it out, and
`dd` skips it.
Selected rows stay visible while a filter hides others, so an action never hits
a row you cannot see; the header counts them as outside the filter. An action
ends visual mode as in vim; its range stays marked until `esc`.

A half-typed `gg` or `dd` shows in the header (`d…`) until the second key; any
other key, or 0.8 seconds, cancels it.

The status column shows the last run's outcome and, when inactive, how long ago
it ended (or started if the end time is unavailable), such as `completed · 5m ago`.
Its node in the graph shows the outcome as a symbol
(`▶` running, `✓` completed, `✗` failed, `⚠` other).
The latest [`:*` task](#running-in-each-workspace) follows while it runs or until
a newer run starts: `:pytest ▶` (`+N` more running), `:pytest ✓`, `:pytest ✗1`
with its exit status.


`e` edits the tip commit's description with `jj describe`. The editor loads the
full description, including multiple paragraphs; `enter` inserts a newline.
Saving refreshes the list. If the target changes while the editor is open,
saving is refused and the draft stays visible. Description edits can be undone
with `jj undo`.

`f` moves only the local main bookmark, requiring main to be an ancestor of the
target and refusing bookmark or file conflicts. Equal tips do nothing; commits
and working-copy files stay unchanged. `u` moves main back unless it has moved
again since.

`@` saves an unbookmarked working-copy change, including edits, as
`saved/<change-id>` for later recovery, then runs `jj new` on the chosen bookmark
and checks out its files. Running sessions in `@` block the move.

When the bookmark's commit is still the working copy of another workspace,
typically one [kept from a session](sessions.md#kept-workspaces), `@` asks
first. Any later edit in that workspace would rewrite the commit and leave the
repository's working copy stale. Confirming starts a new empty change in that
workspace so its next edits land on top of the bookmark, then moves `@`; a
session running there blocks this. Cancelling leaves everything as it was; `W`
removes the workspace instead.

## Command line

`:` opens a command line at the bottom of the list, as in vim.

| Command | Action |
|---|---|
| `:!cmd` | Run `cmd` with `sh -c` in the repository |
| `:name args` | Run the configured [`[commands]`](configuration.md#commands-and-leader-key) entry `name`, appending `args` |
| `:*!cmd` · `:*name args` | The same, in [each target workspace](#running-in-each-workspace) at once |
| `:action args` | Start the [`[actions]`](configuration.md#agent-actions) prompt on each target, with `args` appended |
| `:help` · `:refresh` · `:reload` · `:quit` (`:q`) | Same as `?` · `R` · `ctrl+l` · `q` |

| Key | Action in the command line |
|---|---|
| `tab` / `shift+tab` | Complete: command names, programs after `!`, file names for arguments; repeat to cycle |
| `↑` / `↓` · `ctrl+p` / `ctrl+n` | Older / newer command starting with the typed text |
| `→` at end | Accept the newest matching command shown as ghost text |
| `enter` · `esc` | Run · cancel (`backspace` on an empty line also cancels) |

Shell commands take over the terminal like a diff: the TUI is suspended and
returns after `enter`, and a non-zero exit status is reported. They run in the
repository root and receive `SBX_REPO`, `SBX_BOOKMARK` (the highlighted
bookmark, `@` for the working copy) and `SBX_WORKSPACE` (its workspace
directory, empty if it has none yet), e.g.
`cd "$SBX_WORKSPACE" && uv run pytest`. Each repository remembers the latest
500 distinct command lines.

### Running in each workspace

`:*` runs a shell command once per target: the marked rows, or the highlighted
one, as for `x` and `o`. Use it to run tests in several bookmarks at once:
`:*!uv run pytest -x`, or `:*test` for a `[commands]` entry. Each runs in the
background in its bookmark's workspace (the repository for `@`); a missing
workspace is created first, as `o` does. `SBX_BOOKMARK` and `SBX_WORKSPACE`
name that target. More than three targets ask first. A sandbox session on a
target does not block the command; a warning says it may still be changing
the files. Tasks don't block sandbox runs either. `W` keeps a workspace while a
task runs in it, and `dd` and `@` list the running tasks and ask first.

Tasks keep running after sbx-tui exits. The status column, header and a
notification show how each ended. `l` shows the output (`tab` switches to the
run's logs), `P` lists recent tasks and [`T`](#tasks) shows those of every
workspace. `x` sends SIGINT to the target's
running tasks, then SIGTERM and SIGKILL on further presses. Output is kept in
the [state directory](configuration.md#state-directory), the last ten tasks per
bookmark.

### Agent actions

An [`[actions]`](configuration.md#agent-actions) entry names a prompt: `:review`
starts it as an unsupervised sandbox run on each target, like `r` with the
prompt filled in. Words after the name are appended to the prompt, e.g.
`:review focus on error handling`. Targets already running a session are
skipped. sbx-tui asks first for several targets or the main bookmark. Action
runs use the launch defaults unless the action sets its own agent, model,
effort or timeout. They don't change the remembered defaults or the prompt
history.

### Leader key

The leader key (`\` by default, `ui.leader`) followed by a key sequence from
[`[leader]`](configuration.md#commands-and-leader-key) runs its command line, so
`t = "test"` makes `\t` run `:test`, `T = "*test"` runs it in each selected
workspace and `rc = "chores"` starts the `chores` action. The header shows the keys typed so far;
`esc`, an unmapped key or 1.5 seconds without a key cancels. `?` lists the
configured commands and mappings.

## Bookmark history

sbx-tui records how bookmarks came about, a coarse map of the jj history that
keeps only bookmark revisions. New bookmarks start from the bookmark they were
created on; merges and rebases from all their inputs. Each prompt or session on
a bookmark adds a node with its agent, outcome and resulting commit; `P` lists
them. Bookmarks created elsewhere join the history from their nearest
bookmarked ancestors when sbx-tui first sees them.

The list is drawn like `jj log`: children above their parents, the most
recently active first, lines from each bookmark down to those it started from.
`○─╮` starts a line to a further parent; `○─╯` ends lines at a common parent.

```text
▶─╮     merge/parser-tui
✓ │     feature/parser
│ ✗     spike/tui
○─╯     main ●
```

`dd` deletes the local bookmark but keeps its history, so its descendants stay
connected to its parents. Commits stay in jj. `u` and `ctrl+r` undo and redo
bookmark creations (`n`, new bookmarks from the launch form, merge and rebase
results) and deletions (`dd`, bookmarks deleted by [cleanup](cleanup.md)),
restoring the commit the bookmark last pointed to, and `f`, refusing when main
has moved since. Bookmarks with running sessions are not deleted, and the main
bookmark is never deleted by `dd`. Workspace removal, `@` and other jj commands
are not undone; `@` keeps the previous change as `saved/…`, and `jj undo`
covers the rest. The history is kept in the
[state directory](configuration.md#state-directory) and shared by TUI instances.

## Op history

`L` opens the operations history: the [bookmark history](#bookmark-history)
with a line for every event rather than every bookmark. Each prompt or session
continues its bookmark's line, so repeated runs stack up in order; merges and
rebases join the lines of their inputs. Newest first, starting at the
highlighted bookmark's latest event.

```text
      age  bookmark          commit    event
✓     2m   merge/parser-tui   a1b2c3d4  run · completed
○─╮   5m   merge/parser-tui   b2c3d4e5  merge
✓ │   10m  feature/parser    c3d4e5f6  run · completed
✗ │   15m  feature/parser    d4e5f6a7  run · failed
○ │   20m  feature/parser    e5f6a7b8  create
│ ⚠   25m  spike/tui         f6a7b8c9  run · interrupted
│ ○   30m  spike/tui         a7b8c9d0  create
○─╯   1h   main              b8c9d0e1  existing
```

Each line shows its age and the commit the bookmark pointed to when it
was created or the run ended. Runs show their outcome as in the bookmark list,
also after `x` forgot them (`interrupted` when killed), or `?` if it was never
seen; bookmarks deleted since are marked `(deleted)`.
The bottom pane follows the highlighted row and shows the agent, full prompt,
command line, exact time, parents, commit and log path. Long details wrap;
focus the pane with `tab` to scroll with arrow keys, Page Up/Down or Home/End.

| Key | Action |
|---|---|
| `j` `k` · `ctrl+d` `ctrl+u` · `gg` `G` | Move · half page · top/bottom |
| `tab` / `shift+tab` | Switch focus between the table and the details pane |
| `enter` / `P` | Expand the selected operation's details |
| `l` | [Log viewer](#log-viewer) for a background run, including earlier runs |
| `q` / `esc` / `h` / `L` | Back to the list |

New runs retain the full prompt and command line. Older entries may have only
the prompt's first line and no recorded command. Logs of earlier runs stay in the
[state directory](configuration.md#state-directory); interactive sessions have
none.

## Tasks

`T` lists the [`:*` tasks](#running-in-each-workspace) of every workspace:
running ones first, then finished ones, newest first. Each row shows the
outcome, its age (since it ended, or started while running), the bookmark,
the status with any exit code and the command line. The title counts running
tasks and the workspaces they run in. The view opens on the highlighted
bookmark's latest task.

| Key | Action |
|---|---|
| `j` `k` · `ctrl+d` `ctrl+u` · `gg` `G` | Move · half page · top/bottom |
| `l` / `enter` | The task's output in the [log viewer](#log-viewer) |
| `P` | Details: command, shell script, directory, times, PID and output file |
| `x` | Cancel a running task, harder on each press; forget a finished one and its output |
| `q` / `esc` / `h` / `T` | Back to the list |

## Diff review

`=` lists the files that differ between the two revisions: status
(`A`dded, `M`odified, `D`eleted, `R`enamed, `C`opied), lines added and removed,
and the file's size in the target revision. The header totals them.

| Key | Action |
|---|---|
| `enter` / `l` | View the file's diff |
| `space` | Mark the file seen (`✓`) or unseen (`●`), then move down |
| `=` | View the diff of all unseen files, or of every file once all are seen |
| `A` | Mark all files seen, or all unseen once all are seen |
| `j` `k` · `gg` `G` · `ctrl+d` `ctrl+u` | Move · top/bottom · half page |
| `q` / `esc` / `h` | Back to the list |

Diffs use `jj diff --from BASE --to TARGET` with the host's formatter and pager
(including delta). Each viewing starts with a cleared terminal. Exit the viewer,
then press Enter to return to the file list.
The list pins both revisions when it opens, so the viewer shows the change
that was listed even if a run moves the bookmark meanwhile; press `=` on the
bookmark list again to see later changes.

Seen marks are remembered per bookmark (the second revision, or `@`) and per
change to the file: both sides' contents, type and path. A file marked seen
stays seen while later runs change only other files, and becomes unseen again
when its own diff changes, including when main changes it. Marks are kept in
the [state directory](configuration.md#state-directory) and shared by TUI
instances.

## Log viewer

| Key | Action |
|---|---|
| `tab` / `shift+tab` | Next / previous file: transcript, agent log, captured output |
| `j` `k` · `ctrl+d` `ctrl+u` · `ctrl+f` `ctrl+b` | Scroll a line · half page · page |
| `gg` `G` | Top · bottom |
| `f` | Toggle follow |
| `q` / `esc` / `h` | Back to the list |

Like `less +F`, the view follows new output while it is at the bottom; scrolling
up (keys or mouse) pauses, scrolling back to the bottom resumes.

## Pop-ups

Details (`P`), help (`?`) and confirmations scroll with `j` `k`, `ctrl+d`
`ctrl+u`, `ctrl+f` `ctrl+b`, `gg` `G` and the arrow, page and home/end keys.
`q`, `esc` or `enter` close details and help (`?` also closes help);
other keys do nothing. Confirmations take `y`; any key other than a scroll key
cancels.

When another workspace or operation rewrites this workspace's working-copy
commit, jj reports the working copy as stale and refuses to read it. sbx-tui
then asks once: `u` runs `jj workspace update-stale` and refreshes (edits jj had
not snapshotted are recorded first and can be recovered via `jj op log`), and
`q` exits. Other keys leave the question open.

## Launch form

| Key | Action |
|---|---|
| `tab` | Next field (`j`/`k` on choice fields) |
| `h` `l` | Cycle agent, effort, model or fast mode |
| `enter` | Launch interactive session; newline in a background prompt |
| `ctrl+s` / `ctrl+enter` | Launch background run (or click **launch**); `ctrl+enter` requires terminal support |
| `esc` | Cancel, keeping an edited background prompt as a draft |

The form remembers launch choices per repository; see
[defaults and precedence](configuration.md#settings-reference). A bookmark
name that does not exist yet is flagged under the field: launching creates it
from the highlighted bookmark.

For Codex and Claude, the **fast** field enables fast mode in interactive and
background runs. It requires project-sandbox 0.3.1 or later and adds `--fast`
to the command preview. It starts off unless `defaults.fast = true` is configured;
successful launches remember the choice separately for each agent. Other agents
hide the field and do not receive the flag. Agent actions also use this preference.

An edited background prompt that was cancelled, or whose launch failed, comes
back the next time `r` opens on the same bookmark (or the same merge inputs),
until a launch starts. Drafts last until sbx-tui exits; launched prompts go to
the [prompt history](#prompt-history).

## Prompt history

Each repository remembers the latest 500 distinct launched background prompts.

| Key | Action in the prompt box |
|---|---|
| Typing | Show newest matching prompt as ghost text |
| `→` at end / `ctrl+f` | Accept suggestion |
| `ctrl+p` / `ctrl+n` | Older / newer prompt matching the typed prefix |
| `ctrl+r` | Search: type words, `↑`/`↓` choose, `enter` inserts |
| `ctrl+l` · `ctrl+z` | Clear · undo |
