# agent-profile-diagnostics Specification

## Purpose

Lets `sbx-tui doctor` and `:doctor` report configured agent selections that the
launcher would reject or that are probably wrong, before a launch fails.

## Requirements

### Requirement: Doctor reports discovered capabilities
Doctor SHALL list the launcher's supported agents, the host availability of
each one with its source or reason, the supported VCS access modes, and each
runtime's status. It SHALL say when built-in fallbacks were used because
discovery failed.

#### Scenario: Discovery succeeded
- **WHEN** the user runs `sbx-tui doctor`
- **THEN** the output lists supported agents with availability, VCS access modes, and runtimes with their status and which one `auto` selects

#### Scenario: Discovery failed
- **GIVEN** `sandbox.command --help` fails
- **WHEN** the user runs `sbx-tui doctor`
- **THEN** a warning says that capabilities could not be discovered, and agent checks use the built-in lists

### Requirement: Doctor checks every configured agent selection
Doctor SHALL check each configured agent selection: `defaults`, each
`[profiles.NAME]`, each action with its own `agent` field, and the profile used
for commit review. Each problem SHALL name the config key (for example
`profiles.deep.agent`) and say how to fix it.

#### Scenario: Several problems reported together
- **GIVEN** two profiles with problems
- **WHEN** the user runs `sbx-tui doctor`
- **THEN** each problem is reported with its config key

### Requirement: Selections the launcher would reject fail
Doctor SHALL report a failure when, according to discovered capabilities, an
agent selection would be rejected by the launcher: an unsupported agent, an
agent unavailable on this host, `--vcs-access` in `extra_args` with an
unsupported mode, or `--runtime` in `extra_args` with an unsupported value.

#### Scenario: Unsupported VCS access mode
- **GIVEN** `[profiles.review] extra_args = ["--vcs-access=none"]` and discovered modes full, read-only and hidden
- **WHEN** the user runs `sbx-tui doctor`
- **THEN** doctor reports a failure for `profiles.review.extra_args` listing full, read-only and hidden

#### Scenario: Agent unavailable on host
- **GIVEN** `defaults.agent = "codex"`, no Codex host configuration, and no agent gateway
- **WHEN** the user runs `sbx-tui doctor`
- **THEN** doctor reports a failure for `defaults.agent` with the missing configuration as the reason

#### Scenario: Unavailable agent with in-sandbox login
- **GIVEN** a profile with agent codex and `extra_args = ["--no-forward-credentials"]`
- **WHEN** the user runs `sbx-tui doctor`
- **THEN** doctor reports no availability problem for that profile

### Requirement: Gateway selections are checked
Doctor SHALL report a failure for a selection that uses the gateway with an
agent other than pi, opencode or bash, or without a model. When a gateway is
configured but no selection uses it, doctor SHALL say so as information. Doctor
SHALL NOT contact the gateway.

#### Scenario: Gateway profile without model
- **GIVEN** `[profiles.local] agent = "opencode"`, `gateway = true` and no model
- **WHEN** the user runs `sbx-tui doctor`
- **THEN** doctor reports a failure for `profiles.local.model`, saying the gateway requires a model

#### Scenario: Pi unavailable unless it uses the gateway
- **GIVEN** a Pi profile without `gateway = true`, no Pi host configuration, and a configured gateway
- **WHEN** the user runs `sbx-tui doctor`
- **THEN** doctor reports a failure for that profile's agent, with the hint to set `gateway = true`

#### Scenario: Gateway configured but unused
- **GIVEN** `network.agent_proxy` is set and no default, profile or action uses the gateway
- **WHEN** the user runs `sbx-tui doctor`
- **THEN** doctor reports that the gateway is unused and how to opt a profile in

### Requirement: Selected runtime is checked
Doctor SHALL report a failure when the effective runtime is unavailable,
whether it comes from `sandbox.runtime` or from a profile's `--runtime`, and
SHALL show the launcher's detail text and hint.

#### Scenario: Podman configured but not ready
- **GIVEN** `sandbox.runtime = "podman"` and the launcher doctor reports podman with status `fail`
- **WHEN** the user runs `sbx-tui doctor`
- **THEN** doctor reports a failure for `sandbox.runtime` with the launcher's detail and hint

### Requirement: Questionable selections warn
Doctor SHALL warn, without failing, about a Claude or Codex model that appears
in none of that agent's model suggestions while a model list is available, and
about fast mode set for an agent without fast mode. It SHALL NOT warn about
models for other agents.

#### Scenario: Unknown Codex model
- **GIVEN** a profile with agent codex and a model in none of the Codex suggestions
- **WHEN** the user runs `sbx-tui doctor`
- **THEN** doctor warns that the model is not in the model list and suggests `:update-models` or a `[models]` entry

#### Scenario: Gateway model for Pi
- **GIVEN** a profile with agent pi and a model in no list
- **WHEN** the user runs `sbx-tui doctor`
- **THEN** doctor reports no model warning for that profile

### Requirement: Doctor stays timeboxed
Capability probes in doctor SHALL have time limits. A probe that times out SHALL
be reported as a warning, not stop the other checks.

#### Scenario: Launcher doctor hangs
- **GIVEN** `sandbox.command doctor --json` does not finish within its time limit
- **WHEN** the user runs `:doctor`
- **THEN** the report warns that runtime status is unknown and still shows the remaining checks
