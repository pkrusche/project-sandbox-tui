# project-workflows Specification

## Purpose
Defines how a configured workflow (`[workflows.NAME]`) is started on a bookmark,
including choosing the agent profile for each of its steps before anything
launches or is queued.

## Requirements

### Requirement: Step profile picker on workflow start
After `:workflow NAME [TOPIC]` passes its existing checks, the TUI SHALL show a
picker listing the planning action and each step in order, each with a profile
choice, before launching or queueing anything. The picker SHALL appear even when
every step's action names a profile.

#### Scenario: Picker lists every step
- **GIVEN** `[workflows.spec]` has planning action `spec-plan` and steps `spec-check` and `spec-implement`
- **WHEN** the user runs `:workflow spec CSV export` on an idle bookmark
- **THEN** a picker shows `spec-plan`, `spec-check` and `spec-implement` in that order, each with a profile choice
- **AND** no session has started and nothing has been queued

#### Scenario: Workflow refused before the picker
- **GIVEN** a workflow is already in progress on the selected bookmark
- **WHEN** the user runs `:workflow spec`
- **THEN** the workflow is refused as before and no picker is shown

### Requirement: Picker choices
Each step's profile choice SHALL offer every configured profile and Custom. A
step whose action sets its own `agent`, `model` or `effort` SHALL also offer
**action settings**, which applies the action's own fields as an action run does.

#### Scenario: Action with own fields
- **GIVEN** step action `spec-check` sets `effort = "low"` and no profile
- **WHEN** the picker opens
- **THEN** the `spec-check` choice offers the configured profiles, action settings and Custom

#### Scenario: Action without own fields
- **GIVEN** step action `spec-chores` sets only a prompt
- **WHEN** the picker opens
- **THEN** the `spec-chores` choice offers the configured profiles and Custom, but not action settings

### Requirement: Picker initial values
Each step's choice SHALL start at its action's configured profile. If the
action has no profile but sets its own agent fields, it SHALL start at action
settings. Otherwise it SHALL start at the remembered launch profile, resolved as
the `i`/`r` launch forms resolve their initial selection.

#### Scenario: Mixed initial values
- **GIVEN** the remembered launch profile is `fast`
- **AND** `spec-implement` sets `profile = "deep"`, `spec-check` sets `effort = "low"`, and `spec-plan` sets only a prompt
- **WHEN** the picker opens
- **THEN** `spec-implement` starts at `deep`, `spec-check` at action settings, and `spec-plan` at `fast`

#### Scenario: Unknown profile in a step action
- **GIVEN** `spec-implement` sets `profile = "gone"` and no profile `gone` is configured
- **AND** the remembered launch profile is `fast`
- **WHEN** the picker opens
- **THEN** the TUI shows a warning that `actions.spec-implement` refers to unknown profile `gone`
- **AND** `spec-implement` starts at `fast`

### Requirement: Starting or cancelling from the picker
Accepting the picker SHALL start the workflow as before this change, with each
step launched using its chosen profile. Cancelling SHALL leave the bookmark's
queue, sessions and remembered launch choices unchanged.

#### Scenario: Accept with overrides
- **GIVEN** the picker shows `spec-implement` at `deep`
- **WHEN** the user changes `spec-implement` to `fast` and accepts
- **THEN** the workflow starts its planning step in its configured mode
- **AND** the `spec-implement` step later runs with profile `fast`, including its extra arguments

#### Scenario: Cancel
- **WHEN** the user cancels the picker
- **THEN** no session starts, no task is queued, and a new `:workflow` on the bookmark is not refused as in progress

### Requirement: Step settings are snapshotted at start
The settings resolved from each step's choice SHALL be fixed when the workflow
starts. Later edits to profiles or actions SHALL NOT change steps of a workflow
that has already started, including after a restart of the TUI.

#### Scenario: Profile edited mid-workflow
- **GIVEN** a workflow started with `spec-implement` on profile `deep` with effort `high`
- **WHEN** the user changes profile `deep` to effort `low` before `spec-implement` runs
- **THEN** `spec-implement` runs with effort `high`

### Requirement: Picker choices are not remembered
Choices made in the workflow picker SHALL NOT change the repository's remembered
launch profile or remembered agent settings.

#### Scenario: Remembered profile after a workflow
- **GIVEN** the remembered launch profile is `fast`
- **WHEN** the user starts a workflow with every step set to `deep`
- **THEN** the next `i` or `r` launch form initially selects `fast`
- **AND** the next workflow picker starts steps without own settings at `fast`

### Requirement: Dry-run preview shows step profiles
In dry-run mode, accepting the picker SHALL show the workflow preview without
launching or queueing, and the preview SHALL show for each step its chosen
profile (or action settings or Custom) and resolved agent settings alongside
its mode and prompt.

#### Scenario: Dry-run preview
- **GIVEN** the TUI runs in dry-run mode
- **WHEN** the user accepts the picker with `spec-implement` on `deep`
- **THEN** the preview lists `spec-implement` with profile `deep` and its agent, model and effort
- **AND** nothing is launched or queued
