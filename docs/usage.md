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
| `i` · `r` | [Interactive session](sessions.md#interactive-sessions) · background run with prompt and optional timeout |
| `x` | [Cancel a run](sessions.md#one-sandbox-per-bookmark): repeat to cancel harder, a third time to forget it; clears a finished status at once |
| `l` / `enter` | Logs: `tab` cycles transcript/log/output, `f` toggles follow |
| `P` | Last run details (agent, times, IDs, log, command line), history and the interactive command |
| `n` | New bookmark: name and start revision |
| `dd` · `u` · `ctrl+r` | Delete bookmark, keeping its history · undo · redo |
| `v` / `V` · `space` | Select a range / mark individual bookmarks; `esc` clears |
| `=` | Diff against main, or compare two selected revisions |
| `f` | Fast-forward main to highlighted bookmark or `@` |
| `@` | Start a working-copy change on highlighted bookmark |
| `o` | Open workspace in editor |
| `W` | [Clean up workspaces](cleanup.md) |
| `R` · `ctrl+l` | Refresh list · reload configuration |
| `H` · `?` · `q` | Toggle cheat-sheet · full help · quit (background runs continue) |

Selections apply to `x`, `dd`, `o` and `P`; `l` requires one bookmark.
One marked bookmark overrides the highlighted row for `f` and `@`.
With at least two selected, `r` / `i` opens [merge or rebase](merging.md).

The status column shows the last run's outcome; its node in the graph shows
the same as a symbol (`▶` running, `✓` completed, `✗` failed, `⚠` other).

Diffs use `jj diff --from BASE --to TARGET` with the host's formatter and pager
(including delta). Exit the viewer, then press Enter to resume the TUI.

`f` moves only the local main bookmark, requiring main to be an ancestor of the
target and refusing bookmark or file conflicts. Equal tips do nothing; commits
and working-copy files stay unchanged.

`@` saves an unbookmarked working-copy change, including edits, as
`saved/<change-id>` for later recovery, then runs `jj new` on the chosen bookmark
and checks out its files. Running sessions in `@` block the move.

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
results) and deletions, restoring the commit the bookmark last pointed to.
Bookmarks with running sessions are not deleted, and the main bookmark is never
deleted by `dd`. Cleanup, `f` and other jj commands are not undone; use
`jj undo` for those. The history is kept in the
[state directory](configuration.md#state-directory) and shared by TUI instances.

## Launch form

| Key | Action |
|---|---|
| `tab` | Next field (`j`/`k` on choice fields) |
| `h` `l` | Cycle agent, effort or model |
| `enter` | Launch interactive session; newline in a background prompt |
| `ctrl+s` / `ctrl+enter` | Launch background run (or click **launch**); `ctrl+enter` requires terminal support |
| `esc` | Cancel |

The form remembers launch choices per repository; see
[defaults and precedence](configuration.md#settings-reference).

## Prompt history

Each repository remembers the latest 500 distinct launched background prompts.

| Key | Action in the prompt box |
|---|---|
| Typing | Show newest matching prompt as ghost text |
| `→` at end / `ctrl+f` | Accept suggestion |
| `ctrl+p` / `ctrl+n` | Older / newer prompt matching the typed prefix |
| `ctrl+r` | Search: type words, `↑`/`↓` choose, `enter` inserts |
| `ctrl+l` · `ctrl+z` | Clear · undo |
