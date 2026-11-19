# Design

## Context

See proposal.md for motivation; requirements are in `specs/agent-actions` and
`specs/project-workflows`.

Current state:

- `SandboxTUI._action_spec` (app.py) builds a `LaunchSpec` for both `:NAME`
  action runs and every workflow stage. It takes `action.agent/model/effort`
  over `resolve_launch_defaults(cfg, last)` and `resolve_fast`, and never sets
  `profile` or `extra_args`.
- `_run_workflow` builds the planning spec and all step specs immediately and
  calls `_start_launch(..., remember_defaults=False, workflow=WorkflowRun(...))`.
  `WorkflowRun.steps` stores `(action, LaunchSpec)` pairs and is persisted with
  the queue, so step settings are already snapshotted.
- The launch form resolves its initial profile inline (screens/launch.py,
  `LaunchScreen.__init__`): `last.profile is None` selects the first configured
  profile, otherwise `last.profile`, and a name that is no longer configured
  becomes Custom (`""`). Custom uses `AgentControls`, seeded from
  `resolve_launch_defaults` and `last.extra_args`. A named profile's spec takes
  `agent/model/effort/fast/extra_args` from the profile and blanks the model for
  `bash`.
- `LaunchSpec` already has `profile` and `extra_args` and serializes both, so no
  state format changes are needed.
- Config errors are raised at load; the TUI has no load-time warning channel.
  Warnings are `notify(..., severity="warning")` at the point of use, and
  `sbx-tui doctor` reports `warn` checks.

## Goals / Non-Goals

**Goals:**

- One resolution path from "action plus choice" to `LaunchSpec`, shared by
  `:NAME` runs, the workflow picker, and the dry-run preview.
- Reuse the launch form's remembered-profile resolution instead of duplicating
  it.

**Non-Goals:**

- Remembering picker choices per workflow or step.
- Profiles on workflow tables (`[workflows.X]`) or per-workflow overrides of an
  action's profile.
- A picker for plain `:NAME` action runs.
- Changing how the `i`/`r` forms remember or select profiles.

## Decisions

### A step "choice" type and one resolver

Introduce a small choice value in config.py:

```
Choice = profile name (str) | ACTION_SETTINGS | CUSTOM
```

Two pure functions, testable without the TUI:

- `remembered_profile(cfg, last) -> str` returns a profile name or `""` for
  Custom. This is extracted from `LaunchScreen.__init__`, which then calls it.
- `initial_choice(cfg, last, action) -> (Choice, warning | None)` applies the
  spec order. If the action has a profile that is configured, use it. If it has
  a profile that is missing, return a warning and fall through. If it has its
  own agent fields, use action settings. Otherwise use `remembered_profile`.

`_action_spec` gains a `choice` parameter and resolves it as follows:

| Choice | agent/model/effort/fast | extra_args | `spec.profile` |
|---|---|---|---|
| named profile | from the profile (model blank for `bash`) | profile's | name |
| action settings | today's `_action_spec` logic | `[]` | `""` |
| Custom | `resolve_launch_defaults` + `resolve_fast` | `last.extra_args` | `""` |

The timeout is always `action.timeout`, falling back to the remembered or
default timeout, matching today and the profile docs, where timeouts aren't part
of a profile.

Alternative considered: resolving profiles inside `LaunchSpec` construction or
`build_argv`. Rejected because the spec must be a self-contained snapshot, and
argv building already appends `spec.extra_args` after `sandbox.extra_args`.

### `:NAME` runs resolve without UI

`_run_action` calls `initial_choice` once and notifies any warning once rather
than once per target. It then builds specs with that choice. The confirmation
body already shows `specs[0].label()`. It will also include the profile name
when one is used, so a multi-target confirm shows which profile applies.

### Picker screen

Add `screens/workflow.py` with a `WorkflowProfilesScreen(ModalScreen[list[StepChoice] | None])`.
The title shows the workflow and bookmark. There is one row per stage (planning
action first, with its mode: interactive or queued) holding a `CycleField` of
`[*cfg.profiles, ("action settings" if action has own fields), "custom"]`. A
one-line hint under each row shows the resolved `agent/model/effort/fast`, as
the launch form's profile hint does. `enter` (from any field) or a **start**
button dismisses with one choice per stage, in order; `esc` dismisses with `None`.

Rows are keyed by index, not action name, because a workflow may repeat an
action in its steps.

`_run_workflow` is split into two parts:

1. Validate (workflow exists, one bookmark selected) and compute initial
   choices and warnings, then push the picker.
2. In the callback, build the planning spec and step specs from the chosen
   values, append the state-key prompt lines as today, then either show the
   dry-run `MessageScreen` (now including a `profile: X · agent/model/effort`
   line per stage) or call `_start_launch(..., remember_defaults=False, ...)`.

The "workflow already in progress" refusal currently happens inside
`_start_launch`, through `queue.start_workflow` or `queue.add` raising
`BusyError`. To refuse before showing the picker, part 1 checks
`self.queue.get(branch).workflow_in_progress()` and notifies with the same
message. The later check stays in place to cover races.

Alternative considered: a picker only when some step lacks a configured
profile. The user chose to always show it, which makes it the review point
before start.

### Validation

- `ACTION_KEYS` gains `"profile": ""`, so `_build_action`,
  `sbx-tui config set actions.X.profile`, and `Action.to_dict` (which emits
  `profile` when set) pick it up.
- `Config.validate` rejects `profile` combined with any non-empty `agent`,
  `model` or `effort`. It also applies `validate_profile_name` to a non-empty
  profile, which rejects blanks, surrounding whitespace and `custom`. The error
  message is prefixed with `actions.NAME.profile`.
- Existence is **not** validated at load, because profiles can be deleted at
  runtime through `:config-profiles`, and the user chose warn-and-fall-back.

### Doctor

`check_config` appends one `Check("action profiles", "warn", ...)` listing
`actions.X -> 'name'` for every action whose profile is not in `cfg.profiles`,
with the hint "define [profiles.NAME] or change actions.X.profile". No check is
emitted when there are no problems, matching the other optional checks in
`check_config`.

## Risks / Trade-offs

- **Every `:workflow` start now takes one more keypress.**
  → `enter` accepts the shown choices from any field.
- **Existing workflow tests start workflows synchronously through
  `run_command`.**
  → Add a test helper that waits for `WorkflowProfilesScreen` and presses
  `enter`, and update `tests/test_workflow.py` callers.
- **Plain actions without own fields change behaviour slightly.** They now pick
  up the remembered profile's `extra_args`. Before anything is remembered, they
  use the first configured profile instead of `[defaults]`.
  → This is consistent with what the `r` form would pre-select. Note it in the
  CHANGELOG.
- **A typo in `profile` is only a warning,** so a run can quietly use the
  fallback. → The warning appears on every resolution, and `doctor` lists the
  typo.
- **An interactive planning step launched with a profile** takes the profile's
  `extra_args` too. That matches `i` with a profile, so no special case is
  needed.

## Migration Plan

No data migration: `LaunchSpec` and `LastUsed` formats are unchanged, and
existing queued workflow steps keep their snapshotted specs. Existing configs
without `profile` keep working. Actions with their own agent fields behave as
before, and workflows show the picker with today's resolution pre-selected for
those steps. Rollback is a plain revert. An action config containing `profile`
would then fail to load with an "unknown key" error until the field is removed.
