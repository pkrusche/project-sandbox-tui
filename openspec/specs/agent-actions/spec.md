# agent-actions Specification

## Purpose
Defines how configured agent actions (`[actions.NAME]`) obtain their launch
settings, including assigning a saved agent profile to an action, falling back
when none applies, and reporting misconfigured profile references.

## Requirements

### Requirement: Action profile assignment
An action SHALL accept an optional `profile` field naming a saved agent profile.
When the named profile is configured, a launch of that action SHALL use the
profile's agent, model, effort, fast mode and extra arguments, and the action's
own timeout or the remembered launch timeout.

#### Scenario: Plain action run uses its profile
- **GIVEN** `[profiles.deep]` sets agent `codex`, effort `high` and extra arguments `--memory=16g`
- **AND** `[actions.review]` sets `profile = "deep"` and `timeout = 600`
- **WHEN** the user runs `:review` on a bookmark
- **THEN** the background run uses agent `codex`, effort `high`, the profile's model and fast mode, and timeout 600
- **AND** the project-sandbox command includes `--memory=16g` after the shared sandbox arguments

#### Scenario: Profile set from the command line
- **WHEN** the user runs `sbx-tui config set --project actions.review.profile deep`
- **THEN** the project config contains `profile = "deep"` in `[actions.review]`
- **AND** `sbx-tui config list` shows the action's `profile`

### Requirement: Profile excludes per-action agent settings
An action that sets `profile` together with any of `agent`, `model`, `effort`
or `persona`, or sets `persona` together with any of `agent`, `model` or
`effort`, SHALL be rejected as a config error. A `profile` value that is blank,
has surrounding whitespace, or is the reserved name `custom` in any letter case
SHALL also be rejected.

#### Scenario: Profile combined with agent
- **WHEN** the config defines `[actions.review]` with `profile = "deep"` and `agent = "claude"`
- **THEN** loading the config fails with an error naming `actions.review` and the conflicting fields

#### Scenario: Profile combined with timeout
- **WHEN** the config defines `[actions.review]` with `profile = "deep"` and `timeout = 600`
- **THEN** the config loads successfully

#### Scenario: Reserved profile name
- **WHEN** the config defines `[actions.review]` with `profile = "Custom"`
- **THEN** loading the config fails with an error naming `actions.review.profile`

#### Scenario: Persona combined with profile
- **WHEN** the config defines `[actions.review]` with `persona = "reviewer"` and `profile = "deep"`
- **THEN** loading the config fails with an error naming `actions.review` and the conflicting fields

#### Scenario: Persona combined with level
- **WHEN** the config defines `[actions.review]` with `persona = "reviewer"` and `level = 5`
- **THEN** the config loads successfully

### Requirement: Action settings without a profile
An action without `profile` that sets any of `agent`, `model` or `effort` SHALL
launch with those fields, taking unset fields from the remembered launch
choices and no profile extra arguments, as before this change.

#### Scenario: Action with its own effort
- **GIVEN** `[actions.review]` sets only `effort = "high"` and a prompt
- **WHEN** the user runs `:review`
- **THEN** the run uses the remembered launch agent and its remembered model, with effort `high`

### Requirement: Remembered profile fallback for actions
An action with neither `profile` nor its own agent fields SHALL launch with the
repository's remembered launch profile, resolved as the `i`/`r` launch forms
resolve their initial selection. When that resolves to Custom, the run SHALL use
the remembered custom agent settings and custom extra arguments.

#### Scenario: Remembered named profile
- **GIVEN** the last accepted `i` or `r` launch used profile `deep`
- **AND** `[actions.check]` sets only a prompt
- **WHEN** the user runs `:check`
- **THEN** the run uses profile `deep`, including its extra arguments

#### Scenario: Nothing remembered yet
- **GIVEN** no launch profile has been remembered for the repository
- **AND** at least one profile is configured
- **WHEN** the user runs an action that sets only a prompt
- **THEN** the run uses the first configured profile

#### Scenario: Remembered Custom
- **GIVEN** the last accepted launch used Custom with extra arguments `--memory=12g`
- **WHEN** the user runs an action that sets only a prompt
- **THEN** the run uses the remembered agent, model, effort and fast mode and includes `--memory=12g`

#### Scenario: Remembered profile was removed
- **GIVEN** the remembered launch profile is no longer configured
- **WHEN** the user runs an action that sets only a prompt
- **THEN** the run uses the remembered custom agent settings

### Requirement: Unknown action profile warns and falls back
When an action names a profile that is not configured at the time the TUI
resolves its launch settings, the TUI SHALL show a warning naming the action
and the missing profile and SHALL use the remembered profile fallback.

#### Scenario: Action run with a deleted profile
- **GIVEN** `[actions.review]` sets `profile = "gone"` and no profile `gone` is configured
- **WHEN** the user runs `:review`
- **THEN** the TUI shows a warning that `actions.review` refers to unknown profile `gone`
- **AND** the run starts with the remembered profile fallback

#### Scenario: Config still loads
- **WHEN** the config defines an action whose `profile` names no configured profile
- **THEN** the config loads successfully

### Requirement: Doctor reports unknown action profiles
`sbx-tui doctor` SHALL report a warning, not a failure, when any action names a
profile that is not configured, listing each such action and profile name.

#### Scenario: Doctor with an unknown profile
- **GIVEN** `[actions.review]` sets `profile = "gone"` and no profile `gone` is configured
- **WHEN** the user runs `sbx-tui doctor`
- **THEN** the output contains a warning that lists `actions.review` and `gone`
- **AND** the exit status is unaffected by this check

#### Scenario: Doctor with valid profiles
- **GIVEN** every action's `profile` names a configured profile
- **WHEN** the user runs `sbx-tui doctor`
- **THEN** no action-profile warning is reported

### Requirement: Actions do not change remembered launch choices
Launching an action SHALL NOT change the repository's remembered launch profile
or remembered agent settings.

#### Scenario: Action run with a profile
- **GIVEN** the remembered launch profile is `fast`
- **WHEN** the user runs an action whose profile is `deep`
- **THEN** the next `i` or `r` launch form initially selects `fast`

### Requirement: Action persona assignment
An action SHALL accept an optional `persona` field naming a built-in or
configured persona. A run of that action SHALL use the persona with the
repository's remembered provider, or the default provider when none is
remembered, and the action's own timeout or the remembered launch timeout.

#### Scenario: Review action with a persona
- **GIVEN** `[actions.review] persona = "reviewer"` and the remembered provider is Claude Code
- **WHEN** the user runs `:review` on a bookmark
- **THEN** the background run uses `--agent=claude`, `--model=claude-opus-5-5`, effort high and level 4

#### Scenario: Unknown persona
- **WHEN** the config defines `[actions.review] persona = "auditor"` and no persona `auditor` exists
- **THEN** loading fails with an error naming `actions.review.persona`

### Requirement: Action level
An action SHALL accept an optional `level` from 1 to 5. A run of the action
SHALL use that level, otherwise its persona's level, otherwise the project
default level. When the resolved level is below the project default, `:NAME`
SHALL ask for confirmation naming both levels before launching or queueing.

#### Scenario: Action below the project default
- **GIVEN** `security.level = 3` and `[actions.e2e] level = 1`
- **WHEN** the user runs `:e2e`
- **THEN** the TUI asks for confirmation that the run uses level 1, below the project default 3
- **AND** nothing starts if the user declines

#### Scenario: Action level without security levels
- **GIVEN** no `security.level` is configured and `[actions.e2e] level = 2`
- **WHEN** the user runs `:e2e`
- **THEN** the run uses level 2
