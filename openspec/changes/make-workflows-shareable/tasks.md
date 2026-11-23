# Tasks

## 1. Generalised file trust

- [x] 1.1 Extract `FileTrust(path, trusted_path)` from `ProjectTrust` in `config.py` (check, needs_review, diff, compare-and-swap trust under `config_lock`, describe) and keep `ProjectTrust` as the project-config instance; verify the existing trust tests in `tests/test_config.py` and `tests/test_cli.py` pass unchanged with `scripts/test.sh tests/test_config.py tests/test_cli.py`
- [x] 1.2 Add workflow trust storage at `<repo>-<hash>/trusted-workflows/NAME.toml` (directory `0700`) and a scan of `.project-sandbox-tui/workflows/*.toml` returning each file's name and trust state, skipping invalid stems, symlinks and non-regular files with warnings; verify with `tests/test_config.py` cases for new, trusted, changed, deleted, invalid-name and symlinked files

## 2. Workflow file loading and namespacing

- [x] 2.1 Parse trusted workflow file text: require `[workflow]`, allow `[actions.*]`, `[commands]`, `[source]`, warn on unknown tables/keys, reject `profile`/`agent`/`model`/`effort` on actions; verify with `tests/test_config.py` cases for the "Workflow file contents" and "Persona-only agent selection" scenarios
- [x] 2.2 Resolve short names inside the file and namespace actions, commands, `planning_action` and `host:` steps as `NAME/SHORT`, then merge into `Config.actions`, `Config.commands` and `Config.workflows` and run the existing workflow validation; verify with tests for "Namespaced names", "Step refers to main config action" and auto-inserted `human-review` before `host:NAME/SHORT`
- [x] 2.3 Isolate failures: record per-file problems (`Config.workflow_problems`) and drop only the failing workflow, including when `feedback.enabled` is false; make an inline/file name clash a `ConfigError` naming both; verify with tests for "One broken file" and "Inline and file workflow with the same name"
- [x] 2.4 Load the trusted copy for files that exist, never the checkout text, and add workflow files to `Config.sources`; verify with tests for "Changed file keeps the trusted copy" and "Deleted file"
- [x] 2.5 Document workflow files (layout, namespacing, persona-only rule, embedded multi-line commands and how extra arguments are appended, per-file trust, portability guidance, inline vs file) in `docs/configuration.md` and `docs/feedback.md`; verify examples parse with a `tests/test_docs_examples.py` case

## 3. Namespaced names on the command line

- [x] 3.1 Accept `NAME/SHORT` in `commands.parse` for commands (`:`, `:&`, `:*`) and actions, and in completion; keep `[commands]`/`[actions]` names in the main config restricted to the current pattern; verify with `tests/test_commands.py` and `tests/test_command_line.py` cases for `:spec/check`, `:&spec/test -k x` and `:*spec/test`
- [x] 3.2 Validate `[leader]` after workflow files are merged; a target with `/` whose workflow is unavailable becomes a warning and an inactive mapping; verify with `tests/test_config.py` cases for "Leader mapping to a bundled command" and "Leader target from an unavailable workflow"
- [x] 3.3 Verify a bundled action runs like a main config action with an app test that runs `:spec/implement` and checks the persona-derived argv and prompt; update `docs/usage.md` (agent actions, command line) to mention namespaced names

## 4. Reviewing workflow files

- [x] 4.1 Extend `sbx-tui --trust-config` to review the project config and then each new or changed workflow file with its own diff, candidate validation and `[y/N]` prompt; verify with `tests/test_cli.py` for "Accept one, decline another" and "File changes during review"
- [x] 4.2 Turn the TUI start-up review in `app.py` into a sequence of per-file dialogs and report `workflow_problems` on load and `:reload`; extend headless untrusted warnings to list every file; verify with `tests/test_app.py` Pilot tests accepting and declining a workflow file
- [x] 4.3 Report workflow files, trust state, problems and locally-configured persona dependencies in `doctor.check_config`; verify with `tests/test_doctor.py` cases for "New file is not used until trusted" and "Persona configured only locally"
- [x] 4.4 Verify `sbx-tui config list` shows namespaced `actions.spec/plan.*` and `commands.spec/test` entries read-only, with a `tests/test_settings.py` case, and document the trust review in `docs/configuration.md`

## 5. Workflow import

- [x] 5.1 Generalise `models.fetch` error type and messages for non-catalog use without changing `:update-models` behavior; verify `tests/test_models.py` still passes
- [x] 5.2 Add `sbx-tui workflow import SOURCE [--name NAME]` in `cli.py`: GitHub `blob/` rewrite, HTTPS/file only, UTF-8 check, name rules, validation against the effective config before showing anything; verify with `tests/test_cli.py` cases for "GitHub blob URL", "Plain HTTP refused", "Local file", "Renamed on import" and "Import of a file with a profile" (network mocked)
- [x] 5.3 Implement conflict handling (inline: ask for another name; file: overwrite / rename / cancel; EOF cancels). Generate `[source]` (`url` or absolute `path`, `imported_at` in UTC ISO-8601, and `sha256` of the tomlkit body without `[source]`) before previewing the final text or its diff against the existing file. On confirmation, write the reviewed text atomically, refusing symlinked targets, and trust that same text. In `tests/test_cli.py`, verify that the preview includes provenance, the checkout file and trusted copy match the reviewed text, and declining leaves both untouched. Cover "Confirmed import", "Declined import", "Overwrite existing file", "Conflict with inline workflow", "Update from source" and "Provenance written".
- [x] 5.4 Add the doctor "matches source / edited since import" check using the serialized body without `[source]`; verify with a test for "Local edits detected"
- [x] 5.5 Document `sbx-tui workflow import` in `docs/configuration.md` / `docs/feedback.md` and the CLI help; verify `sbx-tui workflow import --help` matches the docs

## 6. Workflows from both sources

- [x] 6.1 Make `:workflow` list inline and file workflows and show namespaced step names in the picker and dry-run preview; verify with `tests/test_workflow.py` cases for "Listing both sources", "Picker for a file workflow" and "Unchanged inline workflow"
- [x] 6.2 Verify a started file workflow keeps its snapshot after the file is deleted and the TUI restarts, with a `tests/test_workflow.py` case for "File removed mid-workflow"

## 7. This repository's workflows and guidance

- [x] 7.1 Move `[workflows.spec]` and the four `spec-*` actions from `.project-sandbox-tui.toml` into `.project-sandbox-tui/workflows/spec.toml` as `plan`, `check`, `implement`, `chores`, keeping prompts generic; verify `sbx-tui --trust-config` accepts it and `:workflow spec` in `--dry-run` lists `spec/plan`, `spec/check`, `spec/implement`, `spec/chores`
- [x] 7.2 Add inline `[workflows.pre-release]` (`planning_action = "release-docs"`, `first_step_interactive = false`, steps `check`, `human-review`, `host:smoke`) and a read-only `[actions.release-docs]` with persona `reviewer`; verify `:workflow pre-release` in `--dry-run` shows all four stages
- [x] 7.3 Update the `spec` example in `docs/feedback.md` to the file form and add guidance for choosing inline vs file workflows to the bundled `sbx-tui-config` skill; verify the skill and docs examples with `tests/test_docs_examples.py`
- [x] 7.4 Add a CHANGELOG entry covering workflow files, per-file trust, namespaced names, import, and the renamed `spec-*` actions

## 8. Integration checks

- [x] 8.1 Run `uv sync --locked`, `scripts/test.sh`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check` and `uv build`; all pass
- [x] 8.2 Run `openspec validate make-workflows-shareable --strict`; it passes
