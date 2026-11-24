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
Each step's choice SHALL offer every configured profile and Custom. A step whose
action names a persona SHALL also offer that persona. A step whose action sets
its own `agent`, `model` or `effort` SHALL also offer **action settings**, which
applies the action's own fields as an action run does.

#### Scenario: Action with own fields
- **GIVEN** step action `spec-check` sets `effort = "low"` and no profile
- **WHEN** the picker opens
- **THEN** the `spec-check` choice offers the configured profiles, action settings and Custom

#### Scenario: Action without own fields
- **GIVEN** step action `spec-chores` sets only a prompt
- **WHEN** the picker opens
- **THEN** the `spec-chores` choice offers the configured profiles and Custom, but not action settings

#### Scenario: Action with a persona
- **GIVEN** step action `spec-chores` sets `persona = "chore-runner"`
- **WHEN** the picker opens
- **THEN** the `spec-chores` choice offers persona chore-runner, the configured profiles and Custom

### Requirement: Picker initial values
Each step's choice SHALL start at its action's persona. If the action has no
persona, it SHALL start at its configured profile. If the action has neither but
sets its own agent fields, it SHALL start at action settings. Otherwise it SHALL
start at the remembered launch selection, resolved as the `i`/`r` launch forms
resolve their initial selection.

#### Scenario: Mixed initial values
- **GIVEN** the remembered launch profile is `fast`
- **AND** `spec-implement` sets `profile = "deep"`, `spec-check` sets `effort = "low"`, and `spec-plan` sets only a prompt
- **WHEN** the picker opens
- **THEN** `spec-implement` starts at `deep`, `spec-check` at action settings, and `spec-plan` at `fast`

#### Scenario: Persona step
- **GIVEN** `spec-chores` sets `persona = "chore-runner"`
- **WHEN** the picker opens
- **THEN** `spec-chores` starts at persona chore-runner with the remembered or default provider

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
Choices made in the workflow picker, including providers and levels, SHALL NOT
change the repository's remembered launch selection, provider or remembered
agent settings.

#### Scenario: Remembered profile after a workflow
- **GIVEN** the remembered launch profile is `fast`
- **WHEN** the user starts a workflow with every step set to `deep`
- **THEN** the next `i` or `r` launch form initially selects `fast`
- **AND** the next workflow picker starts steps without own settings at `fast`

#### Scenario: Remembered provider after a workflow
- **GIVEN** the remembered provider is Codex
- **WHEN** the user starts a workflow with every persona step on Claude Code
- **THEN** the next workflow picker starts persona steps at Codex

### Requirement: Dry-run preview shows step profiles
In dry-run mode, accepting the picker SHALL show the workflow preview without
launching or queueing, and the preview SHALL show for each step its chosen
persona and provider, profile, action settings or Custom, its resolved agent
settings, and its level with the flags it compiles to, alongside its mode and
prompt.

#### Scenario: Dry-run preview
- **GIVEN** the TUI runs in dry-run mode
- **WHEN** the user accepts the picker with `spec-implement` on `deep`
- **THEN** the preview lists `spec-implement` with profile `deep` and its agent, model and effort
- **AND** nothing is launched or queued

#### Scenario: Dry-run preview of a persona step
- **GIVEN** the TUI runs in dry-run mode and `security.level = 3`
- **WHEN** the user accepts the picker with `review` on persona reviewer and provider Codex
- **THEN** the preview lists `review` with persona reviewer, provider Codex, `gpt-6.2-sol`, effort high, level 4 and `--egress=sidecar --vcs-access=read-only`

### Requirement: Provider and level per step
For a step whose choice is a persona, the picker SHALL show a provider field
starting at the remembered provider or the default provider. Every step SHALL
show a level field when a security level is in effect, starting at the
action's level, else the persona's level, else the project default, and shown
in red when below the project default.

#### Scenario: Workflow on another provider
- **GIVEN** `spec-implement` sets `persona = "implementer"` and `spec-check` sets `persona = "checker"`
- **WHEN** the user sets both steps' provider to Gateway/Gemini and accepts
- **THEN** `spec-implement` later runs with `gemini-4-neon` through the gateway at level 2, and `spec-check` with `gemini-4-neon` at level 2

#### Scenario: Reviewer step level
- **GIVEN** `security.level = 3` and step action `review` sets `persona = "reviewer"`
- **WHEN** the picker opens
- **THEN** the `review` step's level starts at 4 and is not shown in red

#### Scenario: Persona tier missing on the chosen provider
- **GIVEN** step action `deep-dive` sets `persona = "debugger"`
- **WHEN** the user selects provider Copilot/Anthropic for that step
- **THEN** the picker shows that Copilot/Anthropic has no frontier model, and accepting is refused until the step's provider or choice changes
