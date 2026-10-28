# Merging and rebasing

Select at least two bookmarks (`v` for a range, `space` for individual marks).
Marking order determines merge or stacking order. Press `r` for a background
integration or `i` for an interactive one. The form adds:

- **Action:** merge or rebase.
- **Bookmark:** new result, default `merge/<a>-<b>-…`.
- **Base** (rebase only): `project.default_branch_start`, else main/master/trunk,
  else `@-`. Accepts any jj revset naming one commit.
- **Prompt:** a template that follows field changes until you edit it.

On launch, sbx-tui prepares the result on the host, then starts the agent there
to resolve conflicts, review the integration and run tests. Input bookmarks stay
unchanged.

| Action | Preparation |
|---|---|
| Merge | `jj new A B C`, with the result bookmark on the merge commit |
| Rebase | `jj duplicate` each input's own commits onto the previous tip, starting at the base; bookmark the last tip |

A current-state report of inputs and conflicts is appended to background prompts
or shown before interactive sessions. Choose `bash` for a shell; in background
mode it instead gets a command that prints changes and fails on remaining conflicts.

Override templates with `[prompts].octopus_merge` and `octopus_rebase`:

| Placeholder | Value |
|---|---|
| `{inputs}` / `{inputs_plain}` | Quoted / space-separated input list |
| `{result}` / `{base}` | Result bookmark / base revision |
| `{first}` / `{rest}` | First input / remaining inputs |
