# Usage and keys

## Command line

```sh
sbx-tui [REPO]               # open the TUI; default: current directory
sbx-tui --init               # initialize config and model catalog
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
order. The cheat-sheet collapses to one line in small terminals.

The `vs main` heading names the baseline: `project.main_branch`, or the first
existing bookmark main/master/trunk. Counts omit zeros:

| Symbol | Meaning |
|---|---|
| `↑N` / `↓N` | Commits only in this revision / main; both appear for diverged histories |
| `ΔN` | Files differing between tips, as with `=` |
| `=` | Matching tips |
| `?` | Comparison unavailable, e.g. missing main or conflicted bookmark |

`@` includes edits captured on refresh; its commit counts toward `↑N` even if empty.

| Key | Action |
|---|---|
| `j` `k` · `gg` `G` · `ctrl+d` `ctrl+u` | Move · top/bottom · half page |
| `/` | Filter (`enter` keeps it, `esc` clears) |
| `:` · `\` | [Command line](#command-line) (`:!cmd` runs a shell command) · [leader key](#leader-key) |
| `i` · `r` | [Interactive session](sessions.md#interactive-sessions) · background run with prompt and optional timeout |
| `x` | [Cancel a run](sessions.md#one-sandbox-per-bookmark): repeat to cancel harder, a third time to forget it; clears a finished status at once; asks first before signalling several runs |
| `l` / `enter` | [Logs](#log-viewer), or run details while a bookmark has no runs |
| `P` | Last run details (agent, times, IDs, log, command line), history and the interactive command |
| `n` | New bookmark: name and start revision |
| `e` | Edit the full description of one bookmark or the working copy (`ctrl+s` saves, `esc` cancels) |
| `dd` · `u` · `ctrl+r` | Delete bookmark, keeping its history · [undo · redo](#bookmark-history) |
| `v` / `V` · `space` | Select a range / mark individual bookmarks; `esc` clears |
| `=` | Diff against main, or compare two selected revisions |
| `f` | Fast-forward main to highlighted bookmark or `@` (`u` undoes) |
| `@` | Start a working-copy change on highlighted bookmark |
| `o` | Open workspace in editor; asks before opening more than three |
| `W` | [Clean up workspaces](cleanup.md) |
| `R` · `ctrl+l` | Refresh list · reload configuration |
| `H` · `?` · `q` | Toggle cheat-sheet · full help · quit (background runs continue; asks first while any are running) |

Selections apply to `x`, `dd`, `o` and `P`; `l` and `e` require one bookmark.
One marked bookmark overrides the highlighted row for `f`, `@`, `r` and `i`.
With at least two selected, `r` / `i` opens [merge or rebase](merging.md).
Selected rows stay visible while a filter hides others, so an action never hits
a row you cannot see; the header counts them as outside the filter. An action
ends visual mode as in vim; its range stays marked until `esc`.

A half-typed `gg` or `dd` shows in the header (`d…`) until the second key; any
other key, or 0.8 seconds, cancels it.

The status column shows the last run's outcome and, when inactive, how long ago
it ended (or started if the end time is unavailable), such as `completed · 5m ago`.
Its node in the graph shows the outcome as a symbol
(`▶` running, `✓` completed, `✗` failed, `⚠` other).

Diffs use `jj diff --from BASE --to TARGET` with the host's formatter and pager
(including delta). Exit the viewer, then press Enter to resume the TUI.

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

## Command line

`:` opens a command line at the bottom of the list, as in vim.

| Command | Action |
|---|---|
| `:!cmd` | Run `cmd` with `sh -c` in the repository |
| `:name args` | Run the configured [`[commands]`](configuration.md#commands-and-leader-key) entry `name`, appending `args` |
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

### Leader key

The leader key (`\` by default, `ui.leader`) followed by a key sequence from
[`[leader]`](configuration.md#commands-and-leader-key) runs its command line, so
`t = "test"` makes `\t` run `:test`. The header shows the keys typed so far;
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

## Launch form

| Key | Action |
|---|---|
| `tab` | Next field (`j`/`k` on choice fields) |
| `h` `l` | Cycle agent, effort or model |
| `enter` | Launch interactive session; newline in a background prompt |
| `ctrl+s` / `ctrl+enter` | Launch background run (or click **launch**); `ctrl+enter` requires terminal support |
| `esc` | Cancel, keeping an edited background prompt as a draft |

The form remembers launch choices per repository; see
[defaults and precedence](configuration.md#settings-reference). A bookmark
name that does not exist yet is flagged under the field: launching creates it
from the highlighted bookmark.

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
