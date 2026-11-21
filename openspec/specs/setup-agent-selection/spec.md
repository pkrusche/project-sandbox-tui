# setup-agent-selection Specification

## Purpose

Separates the agent that runs the setup interview on the host from the agents
that sandbox profiles configure, and gives the setup agent the discovered
capabilities and gateway instructions it needs to propose profiles that work.

## Requirements

### Requirement: Setup agent comes from the host PATH
The setup wizard SHALL offer as setup agents only agent CLIs found on the host
`PATH`. That choice SHALL NOT limit which agents profiles may use.

#### Scenario: Setup agent installed only on the host
- **GIVEN** only `claude` is on `PATH`
- **WHEN** the user opens the setup wizard
- **THEN** Claude is the only setup agent choice, and other agents can still be chosen for profiles

### Requirement: Profile agents come from discovery
The wizard SHALL offer as profile agents the supported agents that are
available through at least one source. Each choice SHALL show its sources, and
agents with different sources may be combined in one setup.

#### Scenario: Agents from different sources
- **GIVEN** Claude host credentials exist, Pi has no host configuration, and the user said they will use the agent gateway
- **WHEN** the wizard lists profile agents
- **THEN** claude is offered with source "host credentials" and pi with source "agent gateway"

#### Scenario: Copilot supported by the launcher
- **GIVEN** the discovered agents include copilot
- **WHEN** the wizard lists profile agents
- **THEN** copilot is offered, with source "token login"

### Requirement: Setup prompt carries discovered capabilities
The prompt given to the setup agent SHALL list the profile agents with their
sources, the supported VCS access modes, and the usable runtimes. It SHALL say
when these came from built-in fallbacks, so the setup agent checks the
launcher's `--help` itself.

#### Scenario: Restricted VCS access available
- **GIVEN** discovered VCS access modes full, read-only and hidden
- **WHEN** the wizard starts the interview
- **THEN** the prompt lists those modes for mapping to profile `extra_args`

### Requirement: Gateway model discovery is delegated to the setup agent
When the user wants the gateway, the `sbx-tui-config` skill SHALL tell the
setup agent to: list the gateway's models, getting the key the way the launcher
does without printing it; save chosen plain IDs as `[models]` suggestions for
pi, opencode or bash; and set `gateway = true` on profiles that use it. sbx-tui
SHALL NOT contact the gateway.

#### Scenario: User chooses gateway models for Pi
- **GIVEN** the user wants Pi profiles through a configured agent gateway
- **WHEN** the setup agent follows the skill
- **THEN** it lists the gateway's model IDs, the user picks some, they are saved under `[models] pi`, and the Pi profiles get `gateway = true`, without the key appearing in output or files

#### Scenario: OpenCode through the gateway
- **GIVEN** the user wants an OpenCode profile through the gateway
- **WHEN** the setup agent writes the profile
- **THEN** it stores the plain gateway model ID, because sbx-tui adds the `agent-proxy/` prefix OpenCode needs

#### Scenario: Gateway unreachable
- **GIVEN** the gateway's model endpoint cannot be reached from the host
- **WHEN** the setup agent follows the skill
- **THEN** it tells the user, asks for model IDs by hand, and still does not print the key
