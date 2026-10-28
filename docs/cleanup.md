# Workspace cleanup

```sh
sbx-tui --cleanup-workspaces --dry-run   # preview
sbx-tui --cleanup-workspaces             # remove eligible workspaces and merged bookmarks
```

Press `W` in the TUI to review candidates: removable rows are preselected,
others show why they are kept. `space` toggles a row, `a` selects all/none,
`enter` removes, `esc` cancels. The CLI removes all eligible candidates and
reports each removal or reason for keeping it.

Candidates come from `jj workspace list`, regardless of name or location.
The current workspace is excluded; the `default` workspace, running sessions,
uncommitted changes and files too large to snapshot are always kept.

## Merged work

`project.main_branch` (default: first of main/master/trunk) defines merged work:

- A workspace with only empty revisions outside `::main` can be removed even
  with ignored files (which are deleted) or a missing directory (unregistered).
  Its bookmark is deleted too, except main itself.
- Fully merged bookmarks without workspaces appear as separate rows.

Unmerged workspaces with ignored files or missing directories are kept. Removal
re-checks eligibility, runs `jj workspace forget` and deletes eligible bookmarks;
tracked remote bookmarks are deleted on the next `jj git push`. Job history stays.

## Force delete

Highlight a kept workspace and press `D`, then confirm to delete its directory
including uncommitted, untracked and ignored files, and unregister it. Bookmarks
stay. Running sessions and the `default` workspace cannot be force-deleted.
