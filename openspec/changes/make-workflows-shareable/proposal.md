# Proposal

## Why

A workflow (`[workflows.NAME]`) is only a list of names. Its actions, host
commands and prompts live elsewhere in the single `.project-sandbox-tui.toml`,
and that file is reviewed and trusted as one text. A workflow can therefore not
be copied to another project without hand-picking its dependencies out of a
larger config, and every edit to one prompt re-opens review of the whole file.
Teams want to keep a workflow such as `spec` in one place and reuse it in other
repositories, including repositories that are themselves sbx-tui projects.

## What Changes

- New **workflow files**: `.project-sandbox-tui/workflows/NAME.toml` holds one
  self-contained workflow: a `[workflow]` table, the `[actions.*]` it uses and
  the `[commands]` its `host:` steps run, as embedded (multi-line) shell
  scripts. The file stem is the workflow name and its unique identifier.
- Actions and commands defined in a workflow file are namespaced by the
  workflow name with `/`: `spec.toml`'s `plan` action loads as `spec/plan`,
  its `test` command as `spec/test`. Inside the file, steps use short names.
- Namespaced actions and commands are callable on their own (`:spec/check`,
  `:&spec/test`, `:*spec/test`, `[leader]` mappings), for example to continue
  by hand after a workflow was ended.
- Actions in workflow files choose their agent only through `persona` (or
  fall back to the launch-form selection). `profile`, `agent`, `model` and
  `effort` remain available to actions in the main config only.
- **Per-file trust**: each workflow file is trusted on its own. sbx-tui keeps a
  trusted copy per workflow name in the repository state directory; a changed
  file keeps the trusted copy in effect until its diff is reviewed, and a
  never-trusted file is ignored with a warning. `--trust-config` and the TUI
  start-up review cover the project config and each workflow file.
- New `sbx-tui workflow import SOURCE [--name NAME]`: fetches a single workflow
  file from an HTTPS URL (GitHub `blob/` URLs are rewritten to raw) or a local
  path, validates it, and resolves name conflicts. It adds a `[source]` table
  with the URL or path, timestamp and checksum before showing the final text
  (or its diff against the existing file). On confirmation it writes and
  trusts that same text.
- Inline `[workflows.NAME]` in the main config keeps working unchanged. A name
  defined both inline and as a file is a load error.
- This repository moves its `spec` workflow into
  `.project-sandbox-tui/workflows/spec.toml` and adds an inline, project-specific
  `pre-release` workflow (docs check, test/ruff/ty review, human review, host
  e2e tests) to show both forms.
- Documentation and the `sbx-tui-config` skill explain when to use each form and
  how to keep workflow files portable.

## Capabilities

### New Capabilities

- `workflow-files`: the workflow file format, discovery and naming,
  namespacing of bundled actions and commands, persona-only agent selection,
  per-file trust with trusted copies, and running bundled actions and commands
  on their own.
- `workflow-import`: importing a single workflow file from an HTTPS URL or a
  local path, including validation, name conflicts, provenance and trust on
  import.

### Modified Capabilities

- `project-workflows`: `:workflow` offers workflows from both sources (inline
  config and trusted workflow files) and resolves their steps through the
  workflow's own namespace.

## Impact

- `src/project_sandbox_tui/config.py`: workflow file discovery, parsing and
  validation; namespacing into `Config.actions` / `Config.commands`; per-file
  trust (`ProjectTrust` generalised or a sibling `WorkflowTrust`); duplicate
  name checks; `Config.sources`.
- `src/project_sandbox_tui/commands.py`: names with one `/` namespace separator
  on the command line and in `[leader]`.
- `src/project_sandbox_tui/cli.py`: `workflow import` subcommand;
  `--trust-config` and untrusted-file warnings for workflow files.
- `src/project_sandbox_tui/app.py`: start-up review of changed workflow files,
  `:workflow` listing, reload.
- `src/project_sandbox_tui/models.py`: reuse `fetch()` for import.
- `src/project_sandbox_tui/doctor.py`, `src/project_sandbox_tui/settings.py`:
  report workflow files and their trust state; `config list` shows namespaced
  entries.
- Repository config: `.project-sandbox-tui.toml` and new
  `.project-sandbox-tui/workflows/spec.toml`.
- Docs: `docs/configuration.md`, `docs/feedback.md`, `docs/usage.md`, the
  bundled `sbx-tui-config` skill, `CHANGELOG.md`.
- State: new `<repo>-<hash>/trusted-workflows/` directory. Existing state and
  the existing trusted project config are unchanged; in-progress workflows keep
  their snapshotted steps.
- No new runtime dependencies.
