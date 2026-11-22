# Spec Delta

## ADDED Requirements

### Requirement: Egress modes are discovered from the launcher
sbx-tui SHALL take the supported egress modes from the choices of the
`--egress` option in `--help`. When the launcher has no such option, no egress
mode is supported and levels 2 to 5 SHALL be unavailable with that reason.

#### Scenario: Launcher offers both modes
- **GIVEN** `--help` lists `--egress {firewall,sidecar}`
- **WHEN** sbx-tui discovers capabilities
- **THEN** the supported egress modes are firewall and sidecar

#### Scenario: Older launcher without the option
- **GIVEN** `--help` has no `--egress` option
- **WHEN** the user cycles the level field
- **THEN** levels 2 to 5 are marked unavailable because the launcher has no `--egress` option, and level 1 remains available

### Requirement: Discovered egress modes are cached
Discovered egress modes SHALL be part of the cached capabilities and SHALL be
refreshed with them. A cache written before this change SHALL be treated as
stale.

#### Scenario: Cache without egress modes
- **GIVEN** cached capabilities from an earlier sbx-tui version without egress modes
- **WHEN** sbx-tui starts
- **THEN** it refreshes the capabilities in the background
