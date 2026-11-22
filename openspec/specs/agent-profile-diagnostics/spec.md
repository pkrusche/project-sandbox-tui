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

### Requirement: Doctor reports level conflicts
When a security level is in effect, doctor SHALL report a failure for
`sandbox.extra_args` and each profile whose extra arguments contain a flag that
levels own, naming the config key and the flag. It SHALL warn that
`network.no_firewall` is ignored when that setting is on.

#### Scenario: Shared firewall switch
- **GIVEN** `security.level = 3` and `sandbox.extra_args = ["--no-firewall"]`
- **WHEN** the user runs `sbx-tui doctor`
- **THEN** doctor reports a failure for `sandbox.extra_args` naming `--no-firewall` and suggesting level 1 for the jobs that need it

#### Scenario: Ignored network setting
- **GIVEN** `security.level = 3` and `network.no_firewall = true`
- **WHEN** the user runs `sbx-tui doctor`
- **THEN** doctor warns that `network.no_firewall` is ignored while security levels are in effect

### Requirement: Doctor reports the egress backend
Doctor SHALL report the effective egress backend and whether the launcher
supports it. For the sidecar backend, it SHALL explain that default settings are
used when `$XDG_CONFIG_HOME/project-sandbox/egress.toml` is missing and otherwise
list the hosts it allows when it can read them. Doctor SHALL NOT change that file.

#### Scenario: Sidecar policy present
- **GIVEN** the egress backend is sidecar and `egress.toml` allows two hosts
- **WHEN** the user runs `sbx-tui doctor`
- **THEN** doctor reports the sidecar backend, lists both hosts and notes that the policy is shared by all projects on the runtime

#### Scenario: Sidecar default settings
- **GIVEN** the egress backend is sidecar and no `egress.toml` exists
- **WHEN** the user runs `sbx-tui doctor`
- **THEN** doctor reports that the sidecar uses default settings and notes that GitHub Copilot needs its hosts allowed by the sidecar

### Requirement: Doctor reports provider availability
Doctor SHALL list each provider row with its availability and reason. It SHALL
report a failure when `catalogue.default_provider` or `commit_review.provider`
names a provider that is unavailable on this host.

#### Scenario: Default provider without gateway
- **GIVEN** `catalogue.default_provider = "gateway-anthropic"` and no `network.agent_proxy`
- **WHEN** the user runs `sbx-tui doctor`
- **THEN** doctor reports a failure for `catalogue.default_provider` saying that no agent gateway is configured
