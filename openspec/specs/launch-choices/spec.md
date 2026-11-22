# launch-choices Specification

## Purpose

Defines which agents, models and options the launch form and profile editor
offer, so that users pick from what the launcher supports and this host can run.

## Requirements

### Requirement: Agent field offers supported agents
The launch form and profile editor SHALL offer the supported agents. An agent
that is unavailable on this host SHALL stay selectable but be marked with the
reason, because it may still work with launch-specific flags.

#### Scenario: Unavailable agent selected
- **GIVEN** codex is supported but unavailable because the host has no Codex configuration
- **WHEN** the user cycles the agent field to codex
- **THEN** the form shows that codex is unavailable on this host and why

#### Scenario: Newly supported agent
- **GIVEN** the discovered agents include copilot
- **WHEN** the user cycles the agent field
- **THEN** copilot is one of the choices

### Requirement: Remembered or configured agents outside the supported set
When the default, remembered or profile agent is not in the discovered supported
agents, the form SHALL show that agent as the current value and mark it as not
supported by the launcher. It SHALL NOT silently replace it.

#### Scenario: Profile names an agent the launcher dropped
- **GIVEN** a profile with `agent = "opencode"` and discovered agents without opencode
- **WHEN** the user selects that profile in the launch form
- **THEN** the agent field shows opencode, marked as not supported by the launcher

### Requirement: Model suggestions keep the existing sources
Model suggestions SHALL keep the current sources and order: configured
`[models]` entries for the agent, then the catalogue models of provider rows
that use the agent, then Claude aliases, Codex's model cache, then the
downloaded catalog. `[models]` entries SHALL be accepted for any well-formed
agent name, including agents that use the agent gateway.

#### Scenario: Gateway models configured for Pi
- **GIVEN** `[models] pi = ["gpt-5-mini"]`
- **WHEN** the user selects agent pi in the launch form
- **THEN** `gpt-5-mini` is the first model suggestion

#### Scenario: Catalogue models in custom mode
- **GIVEN** no `[models]` entries for codex
- **WHEN** the user selects agent codex in custom mode
- **THEN** the first suggestions are `gpt-6-luna`, `gpt-6.1-terra`, `gpt-6.2-sol` and `gpt-6.2-astra`

### Requirement: Agent-specific options follow agent support
The fast-mode field SHALL appear only for agents with fast mode (Claude and
Codex). The effort field SHALL be hidden for bash.

#### Scenario: Bash selected
- **WHEN** the user selects agent bash
- **THEN** neither the effort nor the fast-mode field is shown

### Requirement: Three ways to select a launch setup
The `i` / `r` launch forms SHALL select a setup by **persona** (a provider and a
persona), by a named **profile**, or by **custom** settings. Persona mode SHALL
show provider and persona fields. Profile and custom modes SHALL keep their
existing controls.

#### Scenario: Persona mode fields
- **WHEN** the user selects persona mode in a launch form
- **THEN** the form shows provider and persona fields and hides the custom agent controls

#### Scenario: Profile mode unchanged
- **GIVEN** a configured profile `deep`
- **WHEN** the user selects profile `deep`
- **THEN** the form launches with the profile's agent, model, effort, fast mode and extra arguments as before

### Requirement: Initial launch setup
A launch form SHALL start with the remembered selection: persona mode with the
remembered provider and persona, a remembered profile, or custom. Before any
choice, it SHALL start at the first configured profile, or at persona mode with
the default provider and persona implementer when no profile is configured.

#### Scenario: No profiles and nothing remembered
- **GIVEN** no profiles are configured and no selection is remembered
- **WHEN** the user opens a launch form
- **THEN** the form is in persona mode with the default provider and persona implementer

#### Scenario: Remembered persona selection
- **GIVEN** the last accepted launch used provider Claude Code and persona debugger
- **WHEN** the user opens a launch form
- **THEN** the form is in persona mode with Claude Code and debugger

### Requirement: Persona selections are remembered
An accepted interactive launch or `r` submission in persona mode SHALL remember
the mode, provider and persona per repository, without changing the remembered
custom agent settings or custom extra arguments.

#### Scenario: Custom settings survive a persona launch
- **GIVEN** remembered custom extra arguments `--memory=12g`
- **WHEN** the user launches in persona mode and later selects custom
- **THEN** the custom CLI arguments field contains `--memory=12g`
