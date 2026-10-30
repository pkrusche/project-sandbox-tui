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

`@ working copy` appears first. The cheat-sheet collapses to one line in small
terminals.

The `vs main` heading names the baseline: `project.main_branch`, or the first
existing bookmark main/master/trunk. Counts omit zeros:

| Symbol | Meaning |
|---|---|
| `↑N` / `↓N` | Commits only in this revision / main; both appear for diverged histories |
| `ΔN` | Files differing between tips, as with `d` |
| `=` | Matching tips |
| `?` | Comparison unavailable, e.g. missing main or conflicted bookmark |

`@` includes edits captured on refresh; its commit counts toward `↑N` even if empty.

| Key | Action |
|---|---|
| `j` `k` · `gg` `G` · `ctrl+d` `ctrl+u` | Move · top/bottom · half page |
| `/` | Filter (`enter` keeps it, `esc` clears) |
| `i` · `r` | [Interactive session](sessions.md#interactive-sessions) · background run with prompt and optional timeout |
| `x` | [Stop a run](sessions.md#one-sandbox-per-bookmark); repeat to escalate |
| `l` / `enter` | Logs: `tab` cycles transcript/log/output, `f` toggles follow |
| `n` | New bookmark: name and start revision |
| `v` / `V` · `space` | Select a range / mark individual bookmarks; `esc` clears |
| `d` | Diff against main, or compare two selected revisions |
| `f` | Fast-forward main to highlighted bookmark or `@` |
| `@` | Start a working-copy change on highlighted bookmark |
| `o` · `P` | Open workspace in editor · preview sandbox command |
| `c` · `C` | Clear finished status · [forget a running record](sessions.md#interactive-sessions) without stopping it |
| `W` | [Clean up workspaces](cleanup.md) |
| `R` · `ctrl+r` | Refresh list · reload configuration |
| `H` · `?` · `q` | Toggle cheat-sheet · full help · quit (background runs continue; asks first while any are running) |

Selections apply to `c`, `C`, `x`, `o` and `P`; `l` requires one bookmark.
One marked bookmark overrides the highlighted row for `f` and `@`.
With at least two selected, `r` / `i` opens [merge or rebase](merging.md).

Diffs use `jj diff --from BASE --to TARGET` with the host's formatter and pager
(including delta). Exit the viewer, then press Enter to resume the TUI.

`f` moves only the local main bookmark, requiring main to be an ancestor of the
target and refusing bookmark or file conflicts. Equal tips do nothing; commits
and working-copy files stay unchanged.

`@` saves an unbookmarked working-copy change, including edits, as
`saved/<change-id>` for later recovery, then runs `jj new` on the chosen bookmark
and checks out its files. Running sessions in `@` block the move.

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
