# Design

## Context

See proposal.md for motivation and specs/ for the required behavior.

- `config.py` merges the global config with the *trusted copy* of
  `.project-sandbox-tui.toml` (`ProjectTrust`, `trusted_config_path`), then
  validates. `_validate_workflows` resolves step names against
  `Config.actions` and `Config.commands` and inserts `human-review` before
  `host:` steps.
- `app.py` (`_run_workflow`, `_start_workflow`) looks steps up by name in
  `cfg.actions` / `cfg.commands` and snapshots each step's `LaunchSpec`
  (including the prompt text) and host command script into the queue at start.
  In-progress workflows therefore do not depend on config after start.
- `commands.parse` resolves `:NAME` against `[commands]` and `[actions]`;
  `commands.NAME` allows `[A-Za-z][A-Za-z0-9_-]*`.
- `settings.py` addresses keys like `actions.NAME.field` by splitting on `.`.
- `models.fetch` already reads an `https://` URL (HTTPS-only redirects, size
  cap) or a local file for `:update-models`.
- `tomlkit` is already a dependency for comment-preserving TOML edits.

## Goals / Non-Goals

**Goals:**
- Keep existing step lookup, picker, queue and snapshot code unchanged by
  presenting workflow-file content to it as ordinary, namespaced
  `Config.actions`, `Config.commands` and `Config.workflows` entries.
- One trust mechanism for the project config and workflow files.
- A broken or untrusted workflow file never prevents the TUI from opening.

**Non-Goals:**
- Global (user-level) workflow files under `$XDG_CONFIG_HOME`.
- Declaring or deploying workspace skills a workflow needs (e.g. OpenSpec).
- Cloning repositories, importing several files at once, or a dedicated
  `update` command (re-import covers it).
- Editing workflow files through `sbx-tui config set` / `unset`.
- Automatic migration of inline workflows into files.

## Decisions

### D1 File location and name: `.project-sandbox-tui/workflows/NAME.toml`
Discovered by listing the directory; no registry key in the main config. The
directory sits next to `.project-sandbox-tui.toml`, so it is clearly sbx-tui's
and is tracked like the main config. The stem is the name, so a file can be
renamed (or imported under another name) without editing it.
*Alternatives:* an `include = [...]` list in the main config (every import
would edit the main config and trigger its review); a top-level `workflows/`
directory (likely clashes with other tools, e.g. `.github/workflows`).

### D2 Namespacing at load time with `/`
While loading a workflow file, short names are rewritten: action `plan` ->
`spec/plan`, command `test` -> `spec/test`, step `host:test` ->
`host:spec/test`, `planning_action = "plan"` -> `spec/plan`. The resulting
`Workflow` is stored in `Config.workflows["spec"]` with namespaced step names,
so `app.py` and the queue need no workflow-file awareness.
`/` is used because `.` breaks `settings.py` key splitting and `:` collides with
`host:` and the command line. `commands.NAME` stays as is for main config
names (which keeps the two namespaces disjoint); a second pattern
`NAME(/NAME)?` is accepted by `commands.parse` and `[leader]` validation.
*Alternative:* explicit `spec-plan` prefixes written in the file. Rejected: the
file would have to be edited on rename and could still collide.

### D3 Generalised file trust
Extract the text-snapshot logic of `ProjectTrust` into a reusable
`FileTrust(path, trusted_path)` with the same `check`, `needs_review`, `diff`,
`trust` (compare-and-swap under `config_lock`) and `describe`. The project
config is one instance; each `NAME.toml` is another, with its trusted copy in
`<repo>-<hash>/trusted-workflows/NAME.toml` (directory `0700`). Loading
always reads the **trusted copy** for files that currently exist; the checkout
file is only read for review. A `WorkflowTrust.scan(repo)` helper lists all
workflow files with their trust state for the CLI, TUI and doctor. Trusted
copies of deleted files are left in place (harmless, re-used if the file comes
back unchanged).
*Alternative:* checksum-only records. Rejected by the user: no diff on review
and no fallback to the last reviewed version.

### D4 Validation order and isolation
1. Merge and validate the main config as today (hard errors unchanged).
2. For each trusted workflow file: parse the trusted text, check allowed tables
   and keys (unknown ones -> warnings), enforce persona-only actions, resolve
   short-name references inside the file, namespace, then validate the
   resulting workflow with the existing `_validate_workflows` rules against the
   merged personas, `feedback.enabled` and catalogue.
3. A failing file is dropped with a problem record
   (`Config.workflow_problems: list[tuple[Path, str]]`) shown by the TUI on
   load/reload, printed by the CLI, and reported by doctor. Only the
   inline-vs-file name clash is a hard `ConfigError`.
4. `[leader]` targets containing `/` whose workflow is unavailable become
   warnings and inactive mappings; other unknown targets stay errors.
Main config validation must therefore run leader checks after workflow files
are merged.

### D5 Review UX
`sbx-tui --trust-config` iterates: project config first (if it needs review),
then each new or changed workflow file, each with its own diff and
`Trust <path>? It can run commands on this machine. [y/N]`. Each is validated
as a candidate before being offered. The TUI start-up review (`app.py`
`_review_project_config`) becomes a sequence of the same dialogs. Headless
warnings (`_warn_untrusted`) list every file needing review.

### D6 Bundled commands
Multi-line TOML strings are passed unchanged to `sh -c`, as `[commands]`
values already are. Extra words typed after `:spec/test` are appended to the
script text as today, so they land on its last line; the docs say so. The
review dialog's warning text covers workflow files because they can contain
host commands.

### D7 Import
`cli.py` gains `sbx-tui workflow import SOURCE [--name NAME]`:
1. Rewrite `https://github.com/O/R/blob/REF/PATH` to
   `https://raw.githubusercontent.com/O/R/REF/PATH`.
2. Fetch with `models.fetch` (generalise its error type/messages so they do not
   say "catalog"). UTF-8 decode; reject otherwise.
3. Parse with `tomlkit`, remove any `[source]` table, `dumps` -> *body*;
   `sha256(body)`. Validate the body as workflow file `NAME` (D4 step 2) against
   the current effective config.
4. Name conflicts: an inline workflow -> ask for another name; an existing file
   -> ask "overwrite / rename / cancel". Non-interactive input (EOF) cancels, as
   `_confirm` does.
5. Append the generated `[source]` table (`url` or absolute `path`,
   `imported_at` in UTC ISO-8601, and the body's `sha256`) and serialize the
   final text once. Show the complete text for a new file or a `difflib` diff
   from the current file to that text for an overwrite, then ask for
   confirmation.
6. On confirmation, write the reviewed text atomically to
   `.project-sandbox-tui/workflows/NAME.toml` (refuse symlinked directories or
   target) and pass that same text to `FileTrust` under `config_lock`.
Doctor recomputes the body checksum the same way (tomlkit parse, drop
`[source]`, dumps) and reports "matches source" or "edited since import".
*Alternative:* textual stripping of the `[source]` block. Rejected: fragile
with comments and table ordering; tomlkit round-trips untouched content.

### D8 This repository
- Move `[workflows.spec]` and `spec-plan`, `spec-check`, `spec-implement`,
  `spec-chores` into `.project-sandbox-tui/workflows/spec.toml` as `plan`,
  `check`, `implement`, `chores` (persona-only already). The workflow's
  user-facing name `spec` is unchanged; action names become `spec/plan` etc.
  Prompts stay free of repo-specific paths where practical (they refer to
  "AGENTS.md and the project's development docs").
- Add an inline, project-specific workflow:
  ```toml
  [workflows.pre-release]
  planning_action = "release-docs"
  first_step_interactive = false
  steps = ["check", "human-review", "host:smoke"]
  ```
  with a new `[actions.release-docs]` (persona `reviewer`, read-only: checks
  README, `docs/` and `CHANGELOG.md` against changes since the last release
  tag and reports gaps). It reuses the existing `check` action (tests, ruff,
  ty, build) and `smoke` command. A planning action is required by workflow
  validation, so the docs check is the queued first step.

## Risks / Trade-offs

- [A shared file still assumes project layout (e.g. `scripts/test.sh`)] ->
  docs and the `sbx-tui-config` skill tell authors to keep shared files
  generic and keep project-specific workflows inline; review shows the
  commands before trust.
- [Imported host commands run on the host] -> import and review require an
  explicit confirmation of the exact text; untrusted or changed files never
  run their new content.
- [Renaming `spec-*` actions breaks muscle memory or leader keys] -> this repo
  has no leader entries for them; CHANGELOG notes the new names.
- [Per-checkout trust state] -> unchanged from today: each checkout reviews
  its files once; bookmark workspaces load from the checkout that opened the
  TUI.
- [tomlkit dump differs across versions, changing the body checksum] -> only
  affects the "edited since import" doctor hint, never trust; the dependency
  range is already pinned.
- [Several review dialogs at start-up] -> only for files that changed; each is
  small.

## Migration Plan

No migration is required: inline workflows, the trusted project config and
in-progress workflows continue to work. Moving this repository's `spec`
workflow is a config change shipped with the feature; rollback is restoring the
inline tables. Old sbx-tui versions ignore `.project-sandbox-tui/workflows/`.
