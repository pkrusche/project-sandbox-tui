# Spec Delta

## ADDED Requirements

### Requirement: Commit review isolation is pinned
Every commit summary run SHALL use the egress backend's API-only flag
(`--egress=sidecar` or `--egress=firewall` per `security.egress`) and SHALL NOT
receive `--no-firewall`, `--extra-domain`, `--allow-github`, `--pi-ollama` or
repository metadata, whatever the shared network settings, the project level or
the selected profile are. Corporate proxy and CA settings SHALL still apply.

#### Scenario: Shared domains are not passed
- **GIVEN** `network.extra_domains = ["example.org"]` and `network.no_firewall = true`
- **WHEN** the user summarizes commits
- **THEN** the run's launcher arguments contain `--egress=sidecar` and no `--extra-domain` or `--no-firewall`

#### Scenario: Project without security levels
- **GIVEN** no `security.level` is configured
- **WHEN** the user summarizes commits
- **THEN** the run still uses API-only egress

#### Scenario: Profile with a widening argument
- **GIVEN** `commit_review.profile` names a profile whose extra arguments contain `--allow-github`
- **WHEN** the user invokes a summary
- **THEN** the TUI reports that the profile widens commit review access and starts no run

## MODIFIED Requirements

### Requirement: Dedicated agent profile
Commit summary runs SHALL use the internal commit-reviewer persona on
`commit_review.provider`, or on provider Codex when unset. A
`commit_review.profile` setting SHALL instead select an existing named agent
profile and SHALL NOT be combined with `commit_review.provider`. Starting a
summary SHALL preserve the remembered profile for other launches.

#### Scenario: Default profile
- **GIVEN** neither `commit_review.profile` nor `commit_review.provider` is configured
- **WHEN** the user summarizes commits
- **THEN** the run uses Codex with model `gpt-6-luna` and low effort
- **AND** the remembered launch profile is unchanged

#### Scenario: Selected provider
- **GIVEN** `commit_review.provider = "claude-code"`
- **WHEN** the user summarizes commits
- **THEN** the run uses agent claude with `claude-haiku-5-5` and low effort

#### Scenario: Explicit profile
- **GIVEN** `commit_review.profile` names a configured agent profile
- **WHEN** the user summarizes commits
- **THEN** the run uses that profile's agent settings, including its model, effort, and fast mode where supported

#### Scenario: Profile and provider together
- **WHEN** the config sets both `commit_review.profile` and `commit_review.provider`
- **THEN** loading fails with an error naming `commit_review`

#### Scenario: Unknown or incompatible profile
- **WHEN** the user invokes a summary with a profile that is missing or incompatible with isolated analysis
- **THEN** the TUI identifies the problem and leaves the review usable without starting a run
