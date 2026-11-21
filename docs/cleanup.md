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
current workspace and the directory holding the jj repository store (usually
`default`) are protected. When cleanup sees a running session or a
[`:*` task](usage.md#running-in-each-workspace), it keeps the workspace even
during force deletion. Normal cleanup also keeps files too large to snapshot and uncommitted
changes not already included in main.
Directories containing another registered workspace are protected from both
normal and force removal; remove the nested workspace first and scan again.
Running sessions also protect enclosing and nested workspace directories.

## Cleanup from a second checkout

Multiple TUI instances opened from the same checkout share job and task locks.
The edge case arises when you open sbx-tui from two different checkouts that
share a jj repository. Cleanup lists workspaces across that repository, but
each checkout keeps its own job and task records. For example, cleanup from
one checkout can delete an otherwise eligible workspace while a host task
started from the other checkout is still using it.

Run launches and cleanup from the same checkout. If you use both, wait for
work in the affected workspace to finish and hold off on new launches from
either checkout until cleanup completes.

## Merged work

Clean workspaces without ignored files can be removed even before merging;
their bookmarks stay.

The [main bookmark](configuration.md#settings-reference) defines merged work:

- Workspaces with only empty revisions outside `::main` can be removed, deleting
  ignored files or unregistering missing directories. Their bookmarks are also
  deleted if the bookmarks themselves are fully merged, except main. A
  workspace and its namesake bookmark can point at different revisions.
- Fully merged bookmarks without workspaces appear as separate rows.

Unmerged workspaces with ignored files or missing directories are kept. Removal
holds the job and host-task locks through deletion and verifies the directory
belongs to the selected workspace. Bookmark deletion requires that the original
selection included that bookmark and that it is still merged. Cleanup runs
`jj workspace forget` and deletes eligible bookmarks;
tracked remote bookmarks are deleted on the next `jj git push`. Job history and the
[bookmark history](usage.md#bookmark-history) stay. In the TUI, `u` restores the
deleted bookmarks; removed workspaces cannot be undone.

If a directory listed as missing reappears, or a workspace listed as clean
gains ignored files, cleanup skips it. Review the updated row with `W` or
`sbx-tui --cleanup-workspaces --dry-run` before selecting it again.

## Force delete

Highlight a kept workspace, press `D` and confirm to unregister it and delete
its directory, including uncommitted, untracked and ignored files. Bookmarks
stay. The same job, task and repository-directory protections apply.
The TUI rechecks the displayed deletion list under the locks and skips removal
if entries appeared, disappeared or changed their symlink targets.
