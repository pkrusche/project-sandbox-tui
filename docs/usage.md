# Usage and keys

## Command line

```sh
sbx-tui [REPO]               # open the TUI (default: current directory)
sbx-tui --init               # write global and project config templates, fetch model list if missing
sbx-tui --print-config       # merged config + the command an interactive session would run
sbx-tui --trust-config       # review and trust a new or changed .project-sandbox-tui.toml
sbx-tui --dry-run            # add --dry-run to every project-sandbox call
sbx-tui --update-models      # refresh the model list, see models.md
sbx-tui --cleanup-workspaces # remove idle merged workspaces, see cleanup.md
sbx-tui doctor               # check jj, project-sandbox and the container runtime
sbx-tui config …             # read or change settings, see configuration.md
```

Subcommands take `-R/--repository PATH` (default: the current directory) and
`--config PATH`. To open a repository directory named `config` or `doctor`,
write `sbx-tui ./config`.

`sbx-tui doctor` checks that `jj` is installed (and not older than the release
sbx-tui is tested with, 0.46), that `sandbox.command --version` reports
project-sandbox 0.2.2 or newer, and that the container runtime project-sandbox
would pick (`sandbox.runtime`, or Apple `container` → `docker` → `podman`)
answers `container system status` / `docker info` / `podman info`. It also
warns about an untrusted project config, a missing image source, a missing
multiplexer for `ui.terminal` and a missing notifier. It exits 1 if anything
failed.

## Branch list

The first row is always `@ working copy`, followed by the bookmarks.

| key | action |
|---|---|
| `j` `k` · `gg` `G` · `ctrl+d` `ctrl+u` | move · top/bottom · half page |
| `/` | filter (`enter` keeps it, `esc` clears) |
| `i` | interactive session on the branch ([details](sessions.md#interactive-sessions)) |
| `r` | unsupervised run: prompt + optional timeout, runs in the background |
| `x` | stop a background run: SIGINT; each further press escalates to SIGTERM, then SIGKILL plus `container stop` |
| `l` / `enter` | log view: transcript, agent log, captured output (`tab` cycles, `f` toggles follow) |
| `n` | new bookmark (name + start revision) |
| `v` / `V` · `space` | select branches: range / single marks; `esc` clears |
| `d` | diff the branch (or two selected tips) against the main branch |
| `o` | open the bookmark's workspace in your editor |
| `P` | preview the full `project-sandbox` command |
| `c` · `C` | clear a finished status · force-forget a record stuck as running |
| `W` | clean up workspaces ([details](cleanup.md)) |
| `R` · `?` · `q` | refresh · help · quit (background runs keep going) |
| `H` | show / hide the cheat-sheet |
| `ctrl+r` | reload global and project configuration |

With a selection, `c`, `C`, `x`, `o` and `P` apply to every selected branch;
`l` needs a single one. Two or more selected branches turn `r` / `i` into a
[merge or rebase](merging.md).

## Launch form

| key | action |
|---|---|
| `tab` | next field (`j`/`k` on choice fields) |
| `h` `l` | cycle agent, effort, model |
| `enter` | launch an interactive session (in the prompt: newline) |
| `ctrl+enter` | launch an unsupervised run |
| `esc` | cancel |

The form is pre-filled with the agent, effort, timeout and per-agent model last
used in this repository (kept in the state directory, never written to your
TOML). Config defaults apply until you have launched something.

## Prompt history

Prompts of started unsupervised runs are remembered per repository (newest 500,
duplicates collapsed). In the prompt box:

| key | action |
|---|---|
| typing | newest earlier prompt with that prefix appears as ghost text |
| `→` (at end) · `ctrl+f` | accept the suggestion |
| `ctrl+p` · `ctrl+n` | older / newer prompts with the typed prefix |
| `ctrl+r` | search history: type words to filter, `↑`/`↓` choose, `enter` use |
| `ctrl+l` · `ctrl+z` | clear the prompt · undo |
