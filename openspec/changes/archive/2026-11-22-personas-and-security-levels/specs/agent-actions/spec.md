# Spec Delta

## ADDED Requirements

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

## MODIFIED Requirements

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
