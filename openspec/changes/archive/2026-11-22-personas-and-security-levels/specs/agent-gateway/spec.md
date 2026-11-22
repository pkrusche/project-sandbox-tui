# Spec Delta

## ADDED Requirements

### Requirement: Gateway agent for provider rows
`catalogue.gateway_agent` SHALL choose the agent for gateway provider rows and
SHALL accept only `pi` and `opencode`. Launches from gateway rows SHALL follow
the gateway model argument rules, including the `agent-proxy/` prefix for
OpenCode.

#### Scenario: Invalid gateway agent
- **WHEN** the config sets `catalogue.gateway_agent = "codex"`
- **THEN** loading fails with an error naming `catalogue.gateway_agent` and listing `pi` and `opencode`

#### Scenario: Pi gateway row
- **GIVEN** a configured gateway and the default gateway agent
- **WHEN** a launch uses provider `gateway-openai` at tier fast
- **THEN** the launcher arguments contain `--agent=pi`, `--agent-proxy=<url>` and `--model=gpt-6-luna`

### Requirement: Gateway rows do not change per-agent gateway choices
A launch from a gateway provider row SHALL NOT change the remembered per-agent
gateway setting used by custom launches.

#### Scenario: Custom Pi after a gateway persona launch
- **GIVEN** the remembered gateway setting for pi is off
- **WHEN** the user launches persona implementer with provider Gateway/OpenAI and then selects custom with agent pi
- **THEN** the custom form's gateway field is off
