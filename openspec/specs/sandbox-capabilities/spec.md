# sandbox-capabilities Specification

## Purpose

Tells sbx-tui which agents, VCS access modes and isolation runtimes the
configured project-sandbox launcher supports, and which of them can be used on
this host. The launcher itself is the source, so sbx-tui never offers a choice
the launcher would reject.

## Requirements

### Requirement: Supported agents are discovered from the launcher
sbx-tui SHALL take the supported agents from the choices of the `--agent` option
in the `--help` output of the configured `sandbox.command`, in the order listed.

#### Scenario: Launcher lists copilot
- **GIVEN** `sandbox.command --help` lists `--agent {claude,codex,opencode,pi,copilot,bash}`
- **WHEN** sbx-tui discovers capabilities
- **THEN** the supported agents are claude, codex, opencode, pi, copilot and bash, in that order

#### Scenario: Help output cannot be parsed
- **GIVEN** `--help` fails, times out, or has no `--agent {…}` choices
- **WHEN** sbx-tui discovers capabilities
- **THEN** the built-in agent list applies and the capabilities are marked as not discovered

### Requirement: VCS access modes are discovered from the launcher
sbx-tui SHALL take the supported VCS access modes from the choices of the
`--vcs-access` option in `--help`. When the launcher has no such option, no VCS
access modes are supported.

#### Scenario: Launcher supports restricted VCS access
- **GIVEN** `--help` lists `--vcs-access {full,read-only,hidden}`
- **WHEN** sbx-tui discovers capabilities
- **THEN** the supported VCS access modes are full, read-only and hidden

#### Scenario: Older launcher without the option
- **GIVEN** `--help` has no `--vcs-access` option
- **WHEN** sbx-tui discovers capabilities
- **THEN** no VCS access mode is supported

### Requirement: Isolation runtimes are discovered from the launcher doctor
sbx-tui SHALL take the isolation runtimes and their status from the
`runtimes` section of `sandbox.command doctor --json`. Each runtime has a status
(usable, unavailable or unknown) and the launcher's detail text. The `auto`
entry records which runtime auto-selection picks.

#### Scenario: Runtime usable
- **GIVEN** `doctor --json` reports runtime `docker` with status `ok`
- **WHEN** sbx-tui discovers capabilities
- **THEN** docker is a usable runtime, shown with the launcher's detail text

#### Scenario: Runtime not usable on this platform
- **GIVEN** `doctor --json` reports `apple-container` with status `info` and detail `macOS only`
- **WHEN** sbx-tui discovers capabilities
- **THEN** apple-container is a known runtime that is unavailable, with reason `macOS only`

#### Scenario: Auto selection
- **GIVEN** `doctor --json` reports `auto` with detail `selects docker`
- **WHEN** sbx-tui shows runtime capabilities
- **THEN** it reports that `auto` selects docker

#### Scenario: Doctor unavailable
- **GIVEN** `doctor --json` fails or returns output without a valid runtimes section
- **WHEN** sbx-tui discovers capabilities
- **THEN** the runtimes are the `--runtime {…}` choices from `--help` with unknown status, or the built-in list if `--help` has none

### Requirement: Host availability of supported agents
For each supported agent, sbx-tui SHALL report whether it can run on this host
and through which sources. The sources are: forwarded host credentials, the
configured agent gateway, a token-based login (copilot), or no credentials
needed (bash). An agent with no source is unavailable, with a reason.

#### Scenario: Host credentials present
- **GIVEN** the host has the Claude configuration directory that project-sandbox forwards
- **WHEN** sbx-tui reports agent availability
- **THEN** claude is available with source "host credentials"

#### Scenario: Agent reachable only through the gateway
- **GIVEN** the host has no Pi configuration and `network.agent_proxy` is configured
- **WHEN** sbx-tui reports agent availability
- **THEN** pi is available with source "agent gateway", and only for launches that use the gateway

#### Scenario: Gateway does not serve every agent
- **GIVEN** `network.agent_proxy` is configured and the host has no Codex configuration
- **WHEN** sbx-tui reports agent availability
- **THEN** codex is unavailable, because the gateway serves only pi, opencode and bash

#### Scenario: Agent without any source
- **GIVEN** the host has no Codex configuration and no agent gateway is configured
- **WHEN** sbx-tui reports agent availability
- **THEN** codex is unavailable, with a reason naming the missing host configuration

#### Scenario: Launch-specific login
- **GIVEN** a profile whose `extra_args` contain `--no-forward-credentials`
- **WHEN** sbx-tui checks that profile's agent
- **THEN** the agent counts as available, with source "login in sandbox"

#### Scenario: Availability rules cannot be confirmed
- **GIVEN** the installed project-sandbox package version differs from the launcher's `--version`
- **WHEN** sbx-tui reports agent availability
- **THEN** availability is unknown, and choices are not restricted because of it

### Requirement: Discovery results are cached per launcher
sbx-tui SHALL cache discovery results per resolved `sandbox.command` and
launcher version. Opening the launch form SHALL NOT wait for a probe. Running
`doctor` refreshes the cache. Startup refreshes it in the background when it is
missing, older than one day, or was made by a different command or version.

#### Scenario: Launch form without cache
- **GIVEN** no cached capabilities exist
- **WHEN** the user opens the launch form
- **THEN** the form opens immediately using the built-in lists, and discovery runs in the background

#### Scenario: Launcher upgraded
- **GIVEN** cached capabilities were made with project-sandbox 0.4.1
- **WHEN** `sandbox.command --version` reports 0.5.0
- **THEN** the cache is treated as stale and refreshed

### Requirement: Config loading does not depend on fixed agent and runtime lists
Loading configuration SHALL accept any well-formed agent name and any
well-formed `sandbox.runtime` value, without running the launcher. Support for
these names is checked by doctor and before launches.

#### Scenario: Profile uses a newer agent
- **GIVEN** `[profiles.copilot] agent = "copilot"`
- **WHEN** sbx-tui loads the configuration
- **THEN** loading succeeds

#### Scenario: Malformed agent name
- **GIVEN** `defaults.agent = "claude code"`
- **WHEN** sbx-tui loads the configuration
- **THEN** loading fails with an error naming `defaults.agent`

### Requirement: Launches with unsupported settings are refused before starting
Before starting a launch, sbx-tui SHALL refuse it with an error naming the
setting and the supported values when discovered capabilities show the agent,
`--vcs-access` mode or `--runtime` value is not supported. Only discovered
capabilities count: built-in fallback lists never block a launch.

#### Scenario: Agent not supported by launcher
- **GIVEN** the discovered agents do not include `copilot`
- **WHEN** the user starts a launch with agent copilot
- **THEN** sbx-tui shows an error listing the supported agents and does not run the launcher

#### Scenario: Discovery unavailable
- **GIVEN** capabilities could not be discovered
- **WHEN** the user starts a launch with any well-formed agent
- **THEN** sbx-tui runs the launcher, which reports any error itself

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
