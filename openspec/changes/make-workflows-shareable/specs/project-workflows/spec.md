# Spec Delta

## ADDED Requirements

### Requirement: Workflows from config and files
`:workflow` SHALL list and start workflows defined inline as `[workflows.NAME]`
in the effective config and workflows from trusted workflow files. Inline
workflows SHALL behave as before this change. Steps of a workflow file SHALL be
shown and run under their namespaced names.

#### Scenario: Listing both sources
- **GIVEN** the project config defines `[workflows.pre-release]` and a trusted `spec.toml` exists
- **WHEN** the user types `:workflow`
- **THEN** both `pre-release` and `spec` are listed

#### Scenario: Picker for a file workflow
- **GIVEN** `spec.toml` has planning action `plan` and steps `check` and `implement`
- **WHEN** the user runs `:workflow spec CSV export` on an idle bookmark
- **THEN** the picker shows `spec/plan`, `spec/check` and `spec/implement` in that order, each with the choices of the step profile picker

#### Scenario: Unchanged inline workflow
- **GIVEN** an inline `[workflows.pre-release]` whose steps refer to main config actions and `host:smoke`
- **WHEN** the user runs `:workflow pre-release`
- **THEN** it starts exactly as an inline workflow did before this change
