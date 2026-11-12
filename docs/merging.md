# Merging and rebasing

Select at least two bookmarks in merge or stacking order (`v` for ranges,
`space` for individual marks). Press `r` for background integration or `i` for
interactive. The form adds:

- **Action:** merge or rebase.
- **Bookmark:** result name, default `merge/<a>-<b>-…`.
- **Base** (rebase only): `project.default_branch_start`, else main/master/trunk,
  else `@-`. Accepts any jj revset naming one commit.
- **Prompt:** follows field changes until edited.

The form also offers the same [agent profiles](configuration.md#agent-profiles)
as a single-bookmark launch. Choose a profile for the integration agent, or
**custom** to adjust agent settings and extra project-sandbox arguments. Profiles
do not store the merge action, input order, result bookmark, base or prompt.

sbx-tui prepares the result on the host, then launches the agent to resolve
conflicts, review and test. Input bookmarks stay unchanged.

| Action | Preparation |
|---|---|
| Merge | `jj new --no-edit A B C`; bookmark the merge commit |
| Rebase | `jj duplicate` each input's own commits onto the previous tip, starting at the base; bookmark the last tip |

An input and conflict report is appended to background prompts or shown before
interactive sessions. `bash` opens a shell interactively; in background mode it
prints changes and fails on remaining conflicts.

Override templates with `[prompts].octopus_merge` and `octopus_rebase`:

| Placeholder | Value |
|---|---|
| `{inputs}` / `{inputs_plain}` | Quoted / space-separated input list |
| `{result}` / `{base}` | Result bookmark / base revision |
| `{first}` / `{rest}` | First input / remaining inputs |
