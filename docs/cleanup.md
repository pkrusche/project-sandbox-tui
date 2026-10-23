# Cleaning up workspaces

```sh
sbx-tui --cleanup-workspaces --dry-run   # preview
sbx-tui --cleanup-workspaces             # remove idle clean / merged workspaces and bookmarks
```

In the TUI, `W` lists every workspace and merged bookmark found, with path and
status. Removable ones are preselected; others are greyed out with the reason
they are kept. `space` toggles, `a` selects all / none, `enter` removes, `esc`
cancels. The CLI removes everything removable and prints `removed` /
`would remove` / `deleted bookmark` / `kept` lines.

Workspaces come from `jj workspace list`, whatever their name or location. The
workspace the TUI runs in is never listed.

## Always kept

- workspaces with a running TUI job or project-sandbox session
- uncommitted changes in `@`, or files too large to snapshot
- the repository itself (the `default` workspace)

## Merged work

The main branch (`project.main_branch`; default: first of `main` / `master` /
`trunk`) relaxes the rules for fully merged work:

- A workspace whose commits are all in main (only empty revisions outside
  `::main`) may also be removed when it only has ignored files (deleted) or its
  directory is missing (just unregistered). Its bookmark is deleted too, except
  main itself.
- Bookmarks fully merged into main with no workspace are listed as their own rows.

Unmerged workspaces with ignored files or a missing directory are kept. Removal
re-checks each row, runs `jj workspace forget` and `jj bookmark delete` (a
tracked remote bookmark is deleted on the next `jj git push`). Job history is kept.

## Force delete

Highlight a kept workspace and press `D`. After confirmation this deletes its
directory with everything in it (uncommitted, untracked and ignored files
included) and unregisters it. Bookmarks are kept. Workspaces with a running
session and the `default` workspace can never be force-deleted.
