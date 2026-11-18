# Usage and keys

## CLI

```sh
sbx-tui [REPO]               # open the TUI; default: current directory
sbx-tui --version            # show the installed package version
sbx-tui --init               # detect project type; initialize config, setup skill and model catalog
sbx-tui --print-config       # show effective config and a working-copy sandbox command
sbx-tui --trust-config       # review changed project config
sbx-tui --dry-run            # preview sandbox calls or workspace cleanup
sbx-tui --update-models      # refresh model suggestions
sbx-tui --cleanup-workspaces # remove eligible workspaces and bookmarks
sbx-tui doctor               # check dependencies, runtime, config and comparisons to main
sbx-tui config …             # read or edit settings
```

Subcommands accept `-R/--repository PATH` and `--config PATH`. Open repositories
named `config` or `doctor` as `./config` or `./doctor`.

`doctor` checks jj (tested minimum 0.46), project-sandbox ≥ 0.2.2 and runtime
health; failed checks exit 1, while warnings alone exit 0. It also warns about
untrusted config and missing image sources, multiplexers or notifiers.
Automatic runtime selection tries
Apple `container` on macOS, then Docker and Podman, choosing the first installed
CLI and checking its health. It also calculates comparisons to main
and explains unavailable results. Inside the TUI, `:doctor` shows diagnostics
for the current bookmark list and appends the full CLI report once the checks
finish.

See [configuration](configuration.md), [models](models.md) and
[cleanup](cleanup.md) for the corresponding commands.

## Bookmark list

`@ working copy` appears first, then bookmarks in [history](#bookmark-history)
order. Sessions on `@` run directly in the current checkout, including its local
skills. The main bookmark is greyed out: create a bookmark with `n` before
starting a session there. The cheat-sheet collapses to one line in small
terminals. In narrow ones the header shows the repository's name instead of its
path, then abbreviated counts, and the subject column narrows to keep the status
in view.

The `vs main` heading names the baseline: `project.main_branch`, or the first
existing bookmark main/master/trunk; below 80 columns it is just `main`. Counts
omit zeros:

| Symbol | Meaning |
|---|---|
| `↑N` / `↓N` | Commits only in this revision / main; both appear for diverged histories |
| `ΔN` | Files differing between tips |
| `=` | Matching tips |
| `?` | Comparison unavailable, e.g. missing main or conflicted bookmark |

`@` includes edits captured on refresh; its commit counts toward `↑N` even if empty.
Use `:doctor` to see why a row shows `?`: it reports the baseline, affected
revisions, and the failing jj command and output when available. The report
captures the current list so a later successful refresh does not erase the error
you are inspecting. `R` or `:refresh` retries unavailable comparisons.

The `reviewed` column (`seen` in narrow terminals) shows reviewed file changes
out of all file changes in commits outside main, for example `2/5`. A file
changed in two commits counts twice. It is blank when there are no file changes
to review or the comparison is unavailable. Counts follow the saved marks from
[diff review](#diff-review); empty commits contribute no files.

| Key | Action |
|---|---|
| `j` `k` · `gg` `G` · `ctrl+d` `ctrl+u` | Move · top/bottom · half page |
| `/` | Filter (`enter` keeps it, `esc` clears) |
| `:` · `\` | [Command line](#command-line) (`:!cmd` runs a shell command) · [leader key](#leader-key) |
| `i` · `r` | [Interactive session](sessions.md#interactive-sessions) · run or queue a background prompt with optional timeout; `@` uses the current checkout, main requires a new bookmark |
| `I` | [Host agent](sessions.md#host-agent-sessions) in each selected workspace, creating missing ones; uses `ui.terminal` |
| `x` | [Cancel a run](sessions.md#one-sandbox-per-bookmark) or running [tasks](#tasks): repeat to cancel harder, a third time to forget a run; clears a finished status at once; asks first before signalling several |
| `l` / `enter` | [Logs](#log-viewer) and the latest task output, or run details while a bookmark has neither |
| `P` | Last run details (agent, times, IDs, log, command line), history, recent tasks and the interactive command |
| `O` | [Op history](#op-history): every prompt, session, merge and rebase as a graph |
| `T` | [Tasks](#tasks): every background task in all workspaces |
| `L` | [Prompt queue](#prompt-queue) for the highlighted bookmark |
| `n` | New bookmark: name and start revision |
| `e` | Edit the full description of one bookmark or the working copy (`ctrl+s` saves, `esc` cancels) |
| `dd` · `u` · `ctrl+r` | Delete bookmark, keeping its history · [undo · redo](#bookmark-history) |
| `v` / `V` · `space` | Select a range / mark individual rows, including `@`; `esc` clears |
| `=` | [Review the diff](#diff-review) against main, or between two selected revisions |
| `f` | Fast-forward main to highlighted bookmark or `@` (`u` undoes) |
| `@` | Start a working-copy change on highlighted bookmark |
| `o` | Open workspace in editor, first creating a missing one on the bookmark (`jj workspace add`) and, while idle, deploying [workspace skills](configuration.md#workspace-skills); asks before opening more than three |
| `W` | [Clean up workspaces](cleanup.md) |
| `R` · `ctrl+l` | Refresh list · reload configuration |
| `H` · `?` · `q` | Toggle cheat-sheet · full help · quit (background runs and shell tasks continue; asks first while agent sessions are running) |

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
The latest [background task](#tasks) follows while it runs or until
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

### Switching the working copy

`@` runs `jj new` on the chosen bookmark and checks out its files, preserving
the previous working copy as follows. Main is `project.main_branch`, or the
first existing bookmark main/master/trunk. Running sessions in `@` block the move.

| Previous working-copy state | Recovery bookmark |
|---|---|
| A local bookmark already points at it | Uses that existing bookmark |
| Unbookmarked, but already included in main | No `saved/…` bookmark needed |
| Unbookmarked and outside main, even if empty | Creates `saved/<change-id>` |
| Unbookmarked and main is missing or conflicted | Creates `saved/<change-id>` |

Edits are snapshotted before moving. To return, highlight the saved bookmark
and press `@` again, or run `jj new 'saved/<change-id>'` with the actual name.
Saved names use the first eight characters of the change ID and add a numeric
suffix if the name already exists.

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
| `:!cmd` | Run `cmd` with `sh -c` in the repository, in the foreground |
| `:name args` | Run the configured [`[commands]`](configuration.md#commands-and-leader-key) entry `name` in the foreground, appending `args` |
| `:&!cmd` · `:&name args` | Run once in the repository as a [background task](#background-commands) |
| `:*!cmd` · `:*name args` | Run a background task in [each target workspace](#running-in-each-workspace) at once |
| `:action args` | Start the [`[actions]`](configuration.md#agent-actions) prompt on each target, with `args` appended |
| `:host agent args` | Run the named [host agent](sessions.md#host-agent-sessions) CLI in each target workspace, passing `args` |
| `:help` · `:refresh` · `:reload` · `:quit` (`:q`) | Same as `?` · `R` · `ctrl+l` · `q` |
| `:config-profiles` | Create or edit saved agent profiles in the project config |
| `:doctor` | Explain unavailable comparisons to main and append the full `sbx-tui doctor` report |
| `:update-models [SOURCE]` | Refresh [model suggestions](models.md) from models.dev, an HTTPS URL or a local file |

| Key | Action in the command line |
|---|---|
| `tab` / `shift+tab` | Complete: command names, programs after `!`, file names for arguments; repeat to cycle |
| `↑` / `↓` · `ctrl+p` / `ctrl+n` | Older / newer command starting with the typed text |
| `→` at end | Accept the newest matching command shown as ghost text |
| `enter` · `esc` | Run · cancel (`backspace` on an empty line also cancels) |

### Choosing how to run a shell command

For a walkthrough with terminal illustrations, see
[Workflow: run tests while keeping sbx-tui open](command-workflow.md).

These forms run host shell commands. With `test = "uv run pytest"` in
`[commands]`, the named forms in the table run the same script as the literal
examples. [Agent actions](#agent-actions) start sandbox runs and already run
in the background on each target.

| Behavior | Foreground: `:!uv run pytest` / `:test` | Repository background: `:&!uv run pytest` / `:&test` | Workspace background: `:*!uv run pytest` / `:*test` |
|---|---|---|---|
| Where it starts | Repository root, once | Repository root, once | Each target's workspace; repository root for `@` |
| Which rows it uses | Ignores marked rows | Ignores marked rows | Marked rows, or the highlighted row if none are marked |
| Missing workspace | Does not create one | Does not create one | Creates one for each target that needs it |
| Terminal and output | Takes over the terminal; accepts interactive input | TUI stays usable; output captured; no interactive input | TUI stays usable; output captured per target; no interactive input |
| Task record | No background task record | One task under `@` | One task under each target |
| Stop or return | `Ctrl+C` interrupts; `Enter` at the exit prompt returns to the TUI | `T`, select task, `x` to cancel; `Enter` to view output | Same task controls, separately for each target |
| After quitting sbx-tui | Return from the command before quitting the TUI | Task keeps running | Tasks keep running |

For example, with `feature/x` highlighted and several rows marked, `:&test`
still runs just once in the repository and appears under `@`. `:*test` runs
in every marked workspace. Clear the marks to use `:*test` only in the
highlighted workspace. Merely highlighting a bookmark never changes the
starting directory of `:test` or `:&test`.

All three forms receive `SBX_REPO` as the repository root. For `:!` / `:name`
and `:&`, `SBX_BOOKMARK` identifies the highlighted row and `SBX_WORKSPACE`
is its existing workspace directory, or empty if it has none. For `:*`, those
two variables identify each task's target and workspace. A script can change
directory explicitly, e.g. `cd "$SBX_WORKSPACE" && uv run pytest`; it then
runs there, while `:&` still records its task under `@`.

Foreground commands report their exit status before returning to the TUI.
After `Ctrl+C`, `-2` or `130` means the command was interrupted. Each repository
remembers the latest 500 distinct command lines; all three forms support
completion and leader mappings.

### Background commands

Put `&` before `!` or the configured command name. A trailing shell `&`, as
in `:!uv run pytest &`, does not create a tracked sbx-tui background task.

Repository background tasks appear under `@`; on the bookmark list, `l` opens
their output and `x` cancels them. [`T`](#tasks) lets you inspect or cancel one
task at a time.

### Running in each workspace

Use `:*test` or `:*!uv run pytest -x` to run tests in the target workspaces
as described in the comparison above. More than three targets ask first.
An agent session on a target does not block the command; a warning says it may
still be changing the files. Tasks don't block agent runs either. `W` keeps a
workspace while a task runs in it, and `dd` and `@` list the running tasks and
ask first.

On the bookmark list, `l` shows the target's latest task output (`tab` switches
to the agent run's logs), and `P` lists recent tasks. `x` signals all running
tasks on the target; use [`T`](#tasks) to cancel a single task.

### Agent actions

An [`[actions]`](configuration.md#agent-actions) entry names a prompt: `:review`
starts it as an unsupervised sandbox run on each target, like `r` with the
prompt filled in. Words after the name are appended to the prompt, e.g.
`:review focus on error handling`. Busy targets and targets with pending or paused
queues receive a queued prompt; the protected main
bookmark is skipped. `@` runs in the current checkout.
sbx-tui asks first for several eligible targets. Action runs use the launch
defaults unless the action sets its own agent, model, effort or timeout. They
don't change the remembered defaults or the prompt history.

### Leader key

The leader key (`\` by default, `ui.leader`) followed by a key sequence from
[`[leader]`](configuration.md#commands-and-leader-key) runs its command line, so
`t = "test"` makes `\t` run `:test`, `b = "&test"` runs it once in the
repository background, `T = "*test"` runs it in each selected workspace and
`rc = "chores"` starts the `chores` action. The header shows the keys typed so far;
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
are not undone; `@` keeps unbookmarked work outside main as `saved/…`, and `jj undo`
covers the rest. The history is kept in the
[state directory](configuration.md#state-directory) and shared by TUI instances.

## Op history

`O` opens the operations history: the [bookmark history](#bookmark-history)
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
| `q` / `esc` / `h` / `O` | Back to the list |

New runs retain the full prompt and command line. Older entries may have only
the prompt's first line and no recorded command. Logs of earlier runs stay in the
[state directory](configuration.md#state-directory); interactive sessions have
none.

## Tasks

`T` lists all [`:&` background commands](#background-commands) and
[`:*` tasks](#running-in-each-workspace):
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

Cancellation sends SIGINT, then SIGTERM and SIGKILL on further presses. The status
column, header and a notification report completion. Tasks keep running after
sbx-tui exits; shell tasks alone do not trigger a quit confirmation. Completion
notifications wait until a TUI instance refreshes the repository.

Records and output are kept in the
[state directory](configuration.md#state-directory), the last ten tasks per
bookmark. Running tasks and completions awaiting notification are retained until
they finish and are announced, then older records and their output are pruned.

## Diff review

For a single bookmark (or `@`), `=` opens a file review for each commit in
`main..bookmark`, oldest first. Use `[` and `]` to move between commits; the
header shows the current position, commit ID and description. Each commit's
table lists its changed files with status (`A`dded, `M`odified, `D`eleted,
`R`enamed, `C`opied), line counts and target sizes. This includes changes that
a later commit reverts, and every side of a merge. Empty commits show an empty
file table and can be passed with `]`.

Returning to the bookmark list with `q`, `esc` or `h` remembers the commit ID and
selected file row for that bookmark during the current TUI session. Press `=`
again to resume there. If the saved commit is no longer in the review, it opens
at the first commit instead. Direct comparisons also remember their file row.
Positions are separate for each bookmark and baseline, or each ordered pair of
revisions in a direct comparison. They reset when sbx-tui exits and are not
shared with other TUI instances. The saved row is a position in the table;
if the file list changes, that row may identify a different file.

| Key | Action |
|---|---|
| `[` / `]` | Previous / next commit |
| `enter` / `l` | View the selected file's diff in this commit |
| `space` | Mark the file seen (`✓`) or unseen (`●`), then move down |
| `=` | View unseen files in this commit, or all its files once all are seen |
| `A` | Mark all files in this commit seen, or all unseen once all are seen |
| `j` `k` · `gg` `G` · `ctrl+d` `ctrl+u` | Move · top/bottom · half page |
| `q` / `esc` / `h` | Back to the bookmark list |

Diffs use `jj diff -r COMMIT` restricted to the selected files, with the host's
formatter and pager (including delta). Merge diffs show changes relative to the
automatic merge of their parents. Each viewing starts with a cleared terminal.
Short diffs stay in the pager too; `q` returns directly to the file table, with
no Enter prompt. `Ctrl+C` interrupts the viewer and returns to the TUI. A viewer
stopped by `Ctrl+Z` is closed so the TUI can reclaim the terminal. The review pins
commit IDs when it opens, so files and diffs stay the same even if a run moves
the bookmark meanwhile; reopen the review to see later changes.

Seen marks are remembered across bookmarks by commit ID and file change. Marking
a file seen or unseen applies to every bookmark containing that commit, including
bookmarks created later. Existing saved marks are shared automatically. Reviewing
a file in one commit leaves it unseen in other commits that change it. Adding
commits keeps earlier marks; rewriting a commit makes its files unseen again.
Advancing main removes commits it now includes from the review and counts.
Marks are kept in the [state directory](configuration.md#state-directory) and
shared by TUI instances.

Selecting two revisions keeps the direct comparison of their tips, using
`jj diff --from BASE --to TARGET`. This uses the same file table and controls,
without commit navigation. These marks are shared by file change (contents, type
and paths) across revisions, independently of per-commit file marks and the
bookmark list's reviewed count.

## Log viewer

| Key | Action |
|---|---|
| `tab` / `shift+tab` | Cycle through the transcript, agent log, run feedback, and captured stdout when available |
| `v` | Switch between captured stdout and the agent log |
| `j` / `k` | Select the next or previous line in the agent event log, or scroll in other views |
| `enter` | Inspect the selected JSON record |
| `ctrl+d` `ctrl+u` · `ctrl+f` `ctrl+b` | Scroll a half page · page |
| `gg` `G` | Top · bottom |
| `f` | Toggle follow |
| `q` / `esc` / `h` | Back to the list |

Running coding agents open on captured stdout when available. This shows the
Markdown translation as project-sandbox emits it. In the agent event log,
click a line to select it or double-click a JSON record to open the scrollable
dialog. `q`, `esc`, or `enter` closes the dialog.

The view follows new output at the bottom. Scrolling up or selecting an earlier
record pauses following. Scroll to the bottom or press `G` to resume. Use `f`
to toggle following at any time.

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
| `h` `l` | Cycle profile, or agent, effort, model and fast mode under Custom |
| `enter` | Launch interactive session; newline in a background prompt |
| `ctrl+s` / `ctrl+enter` | Submit a background prompt (or click the submit button): run or queue from `r`, save or add from `L`; `ctrl+enter` requires terminal support |
| `esc` | Cancel; `r` keeps an edited background prompt as a draft |

Interactive launches and accepted `r` submissions remember launch choices per
repository; see [defaults and precedence](configuration.md#settings-reference).
A bookmark name that does not exist yet is flagged under the field: submitting creates it
from the highlighted bookmark.

Choose a saved [agent profile](configuration.md#agent-profiles), or **custom**
to show the agent, model, effort, fast mode and extra CLI arguments.
Use `:config-profiles` to name and save profiles for this project.
Named profiles show a settings summary; the command preview includes their extra
arguments. To edit the profile, close the launch form and use `:config-profiles`.
Profiles are also available in merge and rebase forms.

Under Custom, **CLI args** accepts project-sandbox arguments with shell quoting,
for example `--memory=16g --extra-domain example.org`. These follow the configured
`sandbox.extra_args`. The preview updates as fields change; invalid quoting shows
an error and prevents submitting.

For Codex and Claude, the Custom **fast** field enables fast mode in interactive
and background runs. It requires project-sandbox 0.3.1 or later and adds `--fast`
to the command preview. Other agents hide the field and do not receive the flag.
Named profiles use their own `fast` setting. Agent actions use the remembered
preference or defaults.

An edited background prompt cancelled from `r`, or whose submission failed, comes
back the next time `r` opens on the same bookmark (or the same merge inputs),
until a submission is accepted. Drafts last until sbx-tui exits; submitted prompts
go to the [prompt history](#prompt-history).

## Prompt history

Each repository remembers the latest 500 distinct background prompts submitted
from `r` or saved in `L`, including queued prompts. Configured agent actions do
not add to this history.

| Key | Action in the prompt box |
|---|---|
| Typing | Show newest matching prompt as ghost text |
| `→` at end / `ctrl+f` | Accept suggestion |
| `ctrl+p` / `ctrl+n` | Older / newer prompt matching the typed prefix |
| `ctrl+r` | Search: type words, `↑`/`↓` choose, `enter` inserts |
| `ctrl+l` · `ctrl+z` | Clear · undo |

## Prompt queue

Press `L` on a bookmark to manage its persistent [unsupervised queue](sessions.md#unsupervised-task-queues).
The list shows pending tasks in execution order, followed by running and finished
tasks, newest first. Each row shows its status and the commits it produced.
Adding or editing a task, deleting, reordering, and undo/redo automatically pause
this bookmark's queue. Press `p` to resume when you are done, even if you cancelled
the editor. A running session continues, and other bookmarks' queues keep running.

| Key | Action |
|---|---|
| `j` `k` · `gg` `G` · `ctrl+d` `ctrl+u` | Move · top/bottom · half page |
| `/` | Filter prompts; `enter` keeps the filter, `esc` clears it |
| `n` / `r` | Add a prompt with the launch form; `ctrl+s` queues it |
| `e` / `l` / `enter` | Edit a pending task (`ctrl+s` saves); open the log of a running or finished task |
| `P` | Task details, full prompt, run times, log path and full commit IDs |
| `c` | Review the commits in the task's recorded revision range |
| `dd` | Delete selected tasks, or the highlighted task; running tasks must finish first |
| `J` / `K` | Move selected tasks down/up one position |
| `v` / `V` · `space` | Select a range / mark individual tasks; `esc` clears selection |
| `u` · `ctrl+r` | Undo / redo pending-task edits, including additions, deletions and reordering; deleting finished tasks is permanent |
| `m` then a letter | Save a named mark on the highlighted task |
| `'` or `backtick`, then a letter | Jump to a named mark, clearing the filter |
| `p` | Pause / resume this bookmark's queue without stopping a running session |
| `q` / `h` / `L` | Return to the bookmark list |
| `esc` | Clear filter or selection; otherwise return to the bookmark list |
