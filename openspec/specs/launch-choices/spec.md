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
`[models]` entries for the agent, Claude aliases, Codex's model cache, then the
downloaded catalog. `[models]` entries SHALL be accepted for any well-formed
agent name, including agents that use the agent gateway.

#### Scenario: Gateway models configured for Pi
- **GIVEN** `[models] pi = ["gpt-5-mini"]`
- **WHEN** the user selects agent pi in the launch form
- **THEN** `gpt-5-mini` is the first model suggestion

### Requirement: Agent-specific options follow agent support
The fast-mode field SHALL appear only for agents with fast mode (Claude and
Codex). The effort field SHALL be hidden for bash.

#### Scenario: Bash selected
- **WHEN** the user selects agent bash
- **THEN** neither the effort nor the fast-mode field is shown
