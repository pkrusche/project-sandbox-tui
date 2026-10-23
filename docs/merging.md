# Merging and rebasing several branches

Select two or more branches (`v` for a range, `space` for single ones; marking
order is the merge / stacking order), then:

- `r`: the form gains **action** (`merge` / `rebase`), **result** (new branch,
  default `merge/<a>-<b>-…`) and, for rebase, **base** (default
  main/master/trunk), plus a prompt from a template that follows those fields
  until you edit it.
- `i`: the same, then an interactive session on the result to resolve conflicts
  yourself or with the agent. Pick the `bash` agent for a plain shell.

For unsupervised runs, the `bash` agent fills the prompt with a command that
prints changes and conflicts and fails if conflicts remain.

On launch sbx-tui does the mechanical part on your machine, then starts the
session on the result branch; the agent resolves what is left **and reviews the
integration** (semantic conflicts, tests):

| action | what happens |
|---|---|
| merge | `jj new A B C` (conflicts are recorded in the commit) + bookmark `result` on it |
| rebase | each input's own commits `jj duplicate`d onto the previous tip, starting at `base`, + bookmark `result` on the last |

Inputs are never modified. A "Current state" section (what was merged, which
files or commits conflict) is appended to the prompt; for `i` it is shown first.

Templates can be replaced in `[prompts]` (`octopus_merge`, `octopus_rebase`;
placeholders `{inputs}`, `{result}`, `{base}`, `{first}`, `{rest}`).
