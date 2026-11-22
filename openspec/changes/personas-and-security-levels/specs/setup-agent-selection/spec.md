# Spec Delta

## ADDED Requirements

### Requirement: Setup prompt carries providers and levels
The prompt given to the setup agent SHALL list the provider rows with their
availability, the security levels the launcher supports, and the built-in
personas with their levels.

#### Scenario: Launcher without sidecar egress
- **GIVEN** discovered egress modes include only firewall
- **WHEN** the wizard starts the interview
- **THEN** the prompt says that levels 3 to 5 need `security.egress = "firewall"` in the global config

### Requirement: Setup proposes a level, a default provider and action personas
When the setup agent follows the `sbx-tui-config` and `sbx-tui-safety` skills,
it SHALL propose a project `security.level`, a `catalogue.default_provider`, and
a persona for each configured action, and SHALL keep corporate proxy and CA
settings in the global config.

#### Scenario: Python project setup
- **GIVEN** a python-uv project whose tests need packages from PyPI
- **WHEN** the setup agent proposes settings
- **THEN** it explains levels 2 and 3, proposes a project default level, and assigns personas such as checker and reviewer to the test and review actions
