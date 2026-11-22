# Spec Delta

## Purpose

Defines task personas that combine a model tier, effort, fast mode and security
level, so actions, workflows and launches name the kind of work instead of a
provider-specific agent setup.

## ADDED Requirements

### Requirement: Built-in personas
sbx-tui SHALL provide these personas as tier / effort / fast / level: planner
high / high / off / 3; checker medium / low / on / 2; implementer medium /
medium / off / 2; debugger frontier / xhigh / off / 2; chore-runner fast /
medium / on / 2; reviewer high / high / off / 4.

#### Scenario: Reviewer on Codex
- **WHEN** the user launches persona reviewer with provider Codex
- **THEN** the launch uses `--agent=codex`, `--model=gpt-6.2-sol`, `--effort=high` and runs at level 4

#### Scenario: Checker on Claude Code
- **WHEN** the user launches persona checker with provider Claude Code
- **THEN** the launch uses `claude-sonnet-5-5`, effort low, fast mode on, and level 2

### Requirement: Internal commit-reviewer persona
sbx-tui SHALL provide an internal `commit-reviewer` persona with tier fast,
effort low and fast mode off, used only by commit review. It SHALL NOT be
offered in launch forms or the workflow picker.

#### Scenario: Persona list in a launch form
- **WHEN** the user opens the persona field
- **THEN** it lists planner, checker, implementer, debugger, chore-runner, reviewer and any configured personas, but not commit-reviewer

### Requirement: Configured personas
`[personas.NAME]` SHALL override the fields of a built-in persona of the same
name or define a new persona, with `tier` (fast, medium, high or frontier),
`effort`, `fast` and `level` (1 to 5). Unset fields of an overridden persona
SHALL keep their built-in values.

#### Scenario: Override a built-in persona
- **GIVEN** `[personas.reviewer] tier = "frontier"`
- **WHEN** the user launches persona reviewer with provider Claude Code
- **THEN** the launch uses `claude-fable-5-2` with effort high at level 4

#### Scenario: New persona without a tier
- **WHEN** the config defines `[personas.triage] effort = "low"` without a tier
- **THEN** loading fails with an error naming `personas.triage.tier`

#### Scenario: Invalid level
- **WHEN** the config defines `[personas.reviewer] level = 6`
- **THEN** loading fails with an error naming `personas.reviewer.level`

### Requirement: Persona resolution
A launch from provider P and persona N SHALL use P's agent, P's model for N's
tier, N's effort and fast mode as the provider row allows, and N's level as the
starting level. The resolved settings SHALL be shown before launch.

#### Scenario: Preview of a persona launch
- **WHEN** the user selects provider Gateway/Anthropic and persona planner in a launch form
- **THEN** the hint shows agent `pi`, model `claude-opus-5-5`, effort high, gateway on and level 3

### Requirement: Persona launches carry no profile arguments
A launch resolved from a provider and persona SHALL add no profile or remembered
custom extra arguments. Shared `sandbox.extra_args` SHALL still apply.

#### Scenario: Shared resource argument
- **GIVEN** `sandbox.extra_args = ["--memory=12g"]` and remembered custom arguments `--cpus=8`
- **WHEN** the user launches persona implementer with provider Codex
- **THEN** the launcher arguments contain `--memory=12g` and no `--cpus=8`
