# Workspace cleanup

```sh
sbx-tui --cleanup-workspaces --dry-run   # preview
sbx-tui --cleanup-workspaces             # remove eligible workspaces and merged bookmarks
```

Press `W` to review candidates. Eligible rows are preselected; other rows show
why they are kept. The CLI reports removals and reasons for keeping others.

## Cleanup keys

| Key | Action |
|---|---|
| `j` / `k` | Move down / up |
| `space` / `a` | Toggle this row / all eligible rows |
| `enter` | Review the deletion list and confirm with `y` |
| `D` | Request force deletion of a kept workspace |
| `esc` / `q` | Cancel |

Candidates come from `jj workspace list`, regardless of name or location. The
current workspace and the directory holding the jj repository store (usually
`default`) are protected. A running session or a
[`:*` task](usage.md#running-in-each-workspace) protects its workspace, even
from force deletion. Normal cleanup also keeps workspaces with files too large
to snapshot or uncommitted changes not included in main.

Directories containing another registered workspace are protected from normal
and force removal. Remove the nested workspace first, then scan again. Running
sessions also protect enclosing and nested workspace directories.

## Cleanup from a second checkout

TUI instances opened from the same checkout share job and task locks. The risk
is opening sbx-tui from different checkouts of the same jj repository: cleanup
sees every workspace, but each checkout has its own job and task records. One
checkout could therefore remove a workspace while a host task from the other
checkout is using it.

Run launches and cleanup from the same checkout. If you use both, wait for
work in the affected workspace to finish and hold off on new launches from
either checkout until cleanup completes.

## Merged work

A clean workspace without ignored files can be removed before its work is
merged; its bookmark remains.

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
