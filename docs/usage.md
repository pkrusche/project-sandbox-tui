# Usage and keys

## Command line

```sh
sbx-tui [REPO]               # open the TUI; default: current directory
sbx-tui --init               # write config templates; fetch model catalog if missing
sbx-tui --print-config       # show effective config and an interactive command
sbx-tui --trust-config       # review changed project config
sbx-tui --dry-run            # preview sandbox calls or workspace cleanup
sbx-tui --update-models      # refresh model suggestions
sbx-tui --cleanup-workspaces # remove idle clean/merged workspaces and merged bookmarks
sbx-tui doctor               # check dependencies and runtime
sbx-tui config …             # read or edit settings
```

Subcommands accept `-R/--repository PATH` and `--config PATH`. For a repository
named `config` or `doctor`, use `./config` or `./doctor`.

`doctor` checks jj (tested minimum 0.46), project-sandbox ≥ 0.2.2 and runtime
health. Automatic runtime selection tries Apple `container`, Docker, then
Podman. It warns about untrusted config, missing image sources, multiplexers
and notifiers; failed checks exit 1.

See [configuration](configuration.md), [models](models.md) and
[cleanup](cleanup.md) for the corresponding commands.

## Bookmark list

`@ working copy` appears first. Press `?` for the scrollable key reference.
`H` toggles the cheat-sheet, which collapses to a one-line help hint when the
terminal is too small to leave room for the list.

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
| `f` | Fast-forward main to highlighted bookmark or `@` (one marked bookmark takes precedence) |
| `@` | Start a working-copy change on highlighted bookmark (one marked bookmark takes precedence) |
| `o` · `P` | Open workspace in editor · preview sandbox command |
| `c` · `C` | Clear finished status · [forget a stale running record](sessions.md#interactive-sessions) without stopping it |
| `W` | [Clean up workspaces](cleanup.md) |
| `R` · `ctrl+r` | Refresh list · reload configuration |
| `H` · `?` · `q` | Toggle cheat-sheet · full help · quit (background runs continue) |

With a selection, `c`, `C`, `x`, `o` and `P` apply to all marked bookmarks;
`l` requires one. Selecting at least two turns `r` / `i` into
[merge or rebase](merging.md).

Diffs use `jj diff --from BASE --to TARGET` with the host's formatter and pager
(including delta). The TUI pauses; exit the viewer, then press Enter to return.

`f` moves only the local main bookmark (`project.main_branch`, else
main/master/trunk). It requires main to be an ancestor of the target and refuses
conflicted bookmarks or unresolved file conflicts. Equal tips do nothing;
commits and working-copy files stay unchanged.

`@` saves an unbookmarked working-copy change, including uncommitted edits, as
`saved/<change-id>`, then runs `jj new` on the chosen bookmark and checks out its
files. The saved bookmark remains available to return to. Running sessions in
`@` block the move.

## Launch form

| Key | Action |
|---|---|
| `tab` | Next field (`j`/`k` on choice fields) |
| `h` `l` | Cycle agent, effort or model |
| `enter` | Launch interactive session; newline in a background prompt |
| `ctrl+s` / `ctrl+enter` | Launch background run (or click **launch**); `ctrl+enter` requires terminal support |
| `esc` | Cancel |

The form remembers the last agent, effort, timeout and per-agent model in this
repository. Until then, [config defaults](configuration.md) apply.

## Prompt history

Started background prompts are remembered per repository: newest 500, duplicates
collapsed.

| Key | Action in the prompt box |
|---|---|
| Typing | Show newest matching prompt as ghost text |
| `→` at end / `ctrl+f` | Accept suggestion |
| `ctrl+p` / `ctrl+n` | Older / newer prompt matching the typed prefix |
| `ctrl+r` | Search: type words, `↑`/`↓` choose, `enter` inserts |
| `ctrl+l` · `ctrl+z` | Clear · undo |
