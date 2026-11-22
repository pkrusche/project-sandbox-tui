# Tasks

## 1. Action profile config

- [x] 1.1 Add `profile` to `Action` and `ACTION_KEYS` in `config.py` and emit it from `Action.to_dict` when set; verify with a `tests/test_config.py` case that loads `[actions.review] profile = "deep"` and one that checks `sbx-tui config list` output includes it
- [x] 1.2 In `Config.validate`, reject `profile` combined with non-empty `agent`/`model`/`effort` and apply `validate_profile_name` to a non-empty `profile`, while still allowing `profile` with `timeout` and allowing unknown profile names; verify with `tests/test_config.py` cases for each spec scenario under "Profile excludes per-action agent settings" and "Config still loads"
- [x] 1.3 Verify `sbx-tui config set --project actions.review.profile deep` writes the key, with a `tests/test_settings.py` case
- [x] 1.4 Document `profile` on actions in `docs/configuration.md` (agent actions and agent profiles sections, replacing "configured agent actions do not select profiles"), `docs/usage.md` agent actions, the bundled `sbx-tui-config` skill, and the commented config templates in `config.py`; verify each example parses with `sbx-tui config list` against a scratch project config

## 2. Shared launch resolution

- [x] 2.1 Extract `remembered_profile(cfg, last)` from `LaunchScreen.__init__` and have the launch form call it; verify existing `tests/test_profiles.py` and launch-form tests still pass with `scripts/test.sh tests/test_profiles.py tests/test_app.py`
- [x] 2.2 Add the step choice type (profile name, action settings, Custom) and `initial_choice(cfg, last, action)` returning the choice and an optional unknown-profile warning; verify with unit tests covering every scenario under "Picker initial values" and "Remembered profile fallback for actions"
- [x] 2.3 Extend `_action_spec` to take a choice and resolve it per the design table (profile fields and `extra_args`; action settings as today; Custom from remembered defaults and `last.extra_args`; timeout always from the action or remembered/default), setting `spec.profile`; verify with unit tests on the resulting `LaunchSpec`, including a `bash` profile blanking the model

## 3. Plain action runs

- [x] 3.1 Make `_run_action` use `initial_choice`, notify an unknown-profile warning once per command, and show the profile name in the multi-target confirmation; verify with an app test that `:review` with `profile = "deep"` produces argv containing the profile's `extra_args` after `sandbox.extra_args`, and one with `profile = "gone"` shows the warning and uses the remembered profile
- [x] 3.2 Verify action runs leave remembered launch choices unchanged with an app test that runs a profiled action and then checks the `r` form's initial profile
- [x] 3.3 Add a CHANGELOG entry for action profiles, noting that prompt-only actions now use the remembered launch profile and its extra arguments

## 4. Doctor check

- [x] 4.1 Add the `action profiles` warn check to `doctor.check_config` listing each `actions.X -> 'name'` with an unknown profile; verify with `tests/test_doctor.py` cases for an unknown profile (warn, exit status unaffected) and all-valid profiles (no check emitted)
- [x] 4.2 Mention the check in the agent profiles/actions docs; verify by reading the rendered section against the doctor output from 4.1

## 5. Workflow profile picker

- [x] 5.1 Add `screens/workflow.py` with `WorkflowProfilesScreen`: one indexed row per stage (planning first, with its mode), a `CycleField` of profiles, action settings (only when the action has own fields), and Custom, a resolved-settings hint per row, `enter`/start to accept, and `esc` to cancel; verify with a Pilot test for the choices offered in "Picker choices" and for initial values including a repeated action
- [x] 5.2 Split `_run_workflow`: refuse an in-progress workflow before the picker, notify unknown-profile warnings, push the picker, and build planning and step specs from the returned choices in the callback, keeping `remember_defaults=False`; verify with a `tests/test_workflow.py` test that accepts with an override and checks the later step's argv and `spec.profile`, and a cancel test that leaves the queue empty and allows a new `:workflow`
- [x] 5.3 Add a test helper that accepts the picker and update existing `tests/test_workflow.py` callers of `:workflow`; verify with `scripts/test.sh tests/test_workflow.py`
- [x] 5.4 Include each stage's profile (or action settings/Custom) and resolved agent settings in the dry-run preview; verify with a dry-run app test asserting the preview text and that nothing is launched or queued
- [x] 5.5 Verify the snapshot and no-remember requirements: a test that edits a profile after start and checks the pending step's spec is unchanged after reloading the queue, and one that checks the `r` form and the next picker still start at the previously remembered profile
- [x] 5.6 Update `docs/feedback.md` (project workflows) and the `sbx-tui-config` skill's workflow section to describe the picker, initial values and per-action profiles; update the CHANGELOG entry to note that `:workflow` now always shows the picker

## 6. Integration checks

- [x] 6.1 Run the full development checks from `docs/development.md` (`uv sync --locked`, `scripts/test.sh`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, `uv build`) and verify all pass
- [x] 6.2 Manually start `:workflow spec` with the project's `.project-sandbox-tui.toml` in dry-run mode, set `spec-implement` to a saved profile, and verify the preview matches the chosen profiles
