# Workspace cleanup

```sh
sbx-tui --cleanup-workspaces --dry-run   # preview
sbx-tui --cleanup-workspaces             # remove eligible workspaces and merged bookmarks
```

Press `W` to review candidates: eligible rows are preselected; others show why
they are kept. `j` `k` move, `space` toggles a row, `a` selects all/none,
`enter` lists what goes and asks for `y`, and `esc` or `q` cancels. The CLI removes all eligible candidates, reporting removals and
reasons for keeping others.

Candidates come from `jj workspace list`, regardless of name or location. The
current workspace is excluded. Normal cleanup keeps `default` and workspaces
with running sessions, uncommitted changes or files too large to snapshot.
Directories containing another registered workspace are protected from both
normal and force removal; remove the nested workspace first and scan again.
Running sessions also protect enclosing and nested workspace directories.

## Merged work

The [main bookmark](configuration.md#settings-reference) defines merged work:

- Workspaces with only empty revisions outside `::main` can be removed, deleting
  ignored files or unregistering missing directories. Their bookmarks are also
  deleted if the bookmarks themselves are fully merged, except main. A
  workspace and its namesake bookmark can point at different revisions.
- Fully merged bookmarks without workspaces appear as separate rows.

Unmerged workspaces with ignored files or missing directories are kept. Removal
rechecks eligibility, runs `jj workspace forget` and deletes eligible bookmarks;
tracked remote bookmarks are deleted on the next `jj git push`. Job history and the
[bookmark history](usage.md#bookmark-history) stay. In the TUI, `u` restores the
deleted bookmarks; removed workspaces cannot be undone.

## Force delete

Highlight a kept workspace, press `D` and confirm to unregister it and delete
its directory, including uncommitted, untracked and ignored files. Bookmarks
stay. Running sessions and `default` cannot be force-deleted.
