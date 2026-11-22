# Proposal

## Why

Agent profiles (`[profiles.NAME]`) can only be selected in the `i`/`r` launch
forms. Configured agent actions and the workflow steps built from them ignore
profiles: each step takes the action's own `agent`/`model`/`effort` fields and
otherwise the remembered per-field launch choices, and profile `extra_args`
(such as `--memory=16g`) never reach a step. Users cannot run, for example, the
implementation step of a workflow with a "Deep review" profile and the check
step with a cheaper one.

## What Changes

- Actions gain an optional `profile` field: `[actions.NAME] profile = "..."`.
  It applies to workflow steps and to plain `:NAME` action runs, supplying the
  profile's agent, model, effort, fast mode and `extra_args`. The action's
  `timeout` still applies.
- Setting `profile` together with `agent`, `model` or `effort` on the same action
  is a config error.
- `:workflow NAME [TOPIC]` opens a picker before anything starts, with one
  profile field per step, including the planning step. `enter` starts the
  workflow with the chosen profiles; `esc` cancels without starting or queueing
  anything.
- Each picker field starts at the action's profile. An action with its own
  `agent`/`model`/`effort` fields starts at a new **action settings** choice,
  which keeps today's behavior for that action. Any other step starts at the
  repository's remembered launch profile (`last.profile`), resolved the same
  way as the `i`/`r` launch forms.
- Plain `:NAME` action runs without an action profile or own agent fields use
  the same remembered launch profile resolution.
- If an action names a profile that is not configured, the TUI shows a warning
  when it resolves the profile (opening the picker, or running `:NAME`) and
  falls back to the remembered launch profile resolution. `sbx-tui doctor`
  reports a warning listing such actions.
- Picker choices are snapshotted into the workflow's launch specs, as step
  settings already are, and are not written back to the remembered launch
  profile.
- The workflow dry-run preview shows the resolved profile of each step.
- **BREAKING (interaction):** `:workflow` no longer starts immediately; it always
  shows the picker first.

## Capabilities

### New Capabilities

- `agent-actions`: launch settings of configured agent actions, including
  profile assignment, resolution and fallback, validation, and unknown-profile
  warnings and diagnostics.
- `project-workflows`: starting a configured workflow, including the per-step
  profile picker, snapshotting of step settings, and the dry-run preview.

### Modified Capabilities

None. The project has no existing specs.

## Impact

- `src/project_sandbox_tui/config.py`: `Action.profile`, `ACTION_KEYS`,
  `Action.to_dict`, action validation; shared helper for remembered profile
  resolution.
- `src/project_sandbox_tui/app.py`: `_action_spec`, `_run_action`,
  `_run_workflow`, dry-run preview.
- New picker screen under `src/project_sandbox_tui/screens/`;
  `screens/launch.py` reuses the extracted profile-resolution helper.
- `src/project_sandbox_tui/doctor.py`: new action-profile check.
- `src/project_sandbox_tui/settings.py`: `actions.<name>.profile` via
  `sbx-tui config set` (follows from `ACTION_KEYS`).
- Docs: `docs/configuration.md` (agent profiles, agent actions), `docs/feedback.md`
  (project workflows), `docs/usage.md` (agent actions), the bundled
  `sbx-tui-config` skill, the config templates, and `CHANGELOG.md`.
- Tests: `tests/test_workflow.py` (existing tests must accept the picker),
  `tests/test_config.py`, `tests/test_settings.py`, `tests/test_doctor.py`, and
  action-run tests.
- No changes to persisted state formats: `LaunchSpec` already carries `profile`
  and `extra_args`; `LastUsed` is unchanged.
