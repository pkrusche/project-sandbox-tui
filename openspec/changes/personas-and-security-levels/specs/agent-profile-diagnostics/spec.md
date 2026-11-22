# Spec Delta

## ADDED Requirements

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
