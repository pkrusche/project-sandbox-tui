# Spec Delta

## Purpose

Gives sbx-tui a curated, built-in list of providers and models so users pick a
provider and a capability tier instead of agent-specific model IDs, constrained
to what each agent and access route can run.

## ADDED Requirements

### Requirement: Provider rows
sbx-tui SHALL offer one list of providers, each a route plus vendor with a
fixed agent: `codex` (Codex, agent codex), `claude-code` (Claude Code, agent
claude), `gateway-openai`, `gateway-anthropic` and `gateway-gemini` (through the
agent gateway), and `copilot-openai`, `copilot-anthropic` and `copilot-gemini`
(agent copilot). Each row SHALL be shown with a readable label such as
`Gateway/Anthropic`.

#### Scenario: Provider list
- **WHEN** the user opens the provider field in a launch form
- **THEN** it lists Codex, Claude Code, Gateway/OpenAI, Gateway/Anthropic, Gateway/Gemini, Copilot/OpenAI, Copilot/Anthropic and Copilot/Gemini in that order

#### Scenario: Agent follows from the row
- **WHEN** a launch uses provider `claude-code`
- **THEN** the launcher arguments contain `--agent=claude` and no `--agent-proxy`

### Requirement: Built-in OpenAI tiers
The built-in OpenAI tier models SHALL be fast `gpt-6-luna`, medium
`gpt-6.1-terra`, high `gpt-6.2-sol` and frontier `gpt-6.2-astra`, for the
`codex`, `gateway-openai` and `copilot-openai` rows.

#### Scenario: Codex at high tier
- **WHEN** a launch uses provider `codex` at tier high
- **THEN** the launcher arguments contain `--agent=codex` and `--model=gpt-6.2-sol`

### Requirement: Built-in Anthropic tiers
The built-in Anthropic tier models SHALL be fast `claude-haiku-5-5`, medium
`claude-sonnet-5-5`, high `claude-opus-5-5` and frontier `claude-fable-5-2`, for
the `claude-code` and `gateway-anthropic` rows. The `copilot-anthropic` row
SHALL use the Copilot CLI IDs `claude-haiku-5.5`, `claude-sonnet-5.5` and
`claude-opus-5.5`, and SHALL have no frontier model.

#### Scenario: Copilot uses its own IDs
- **WHEN** a launch uses provider `copilot-anthropic` at tier medium
- **THEN** the launcher arguments contain `--agent=copilot` and `--model=claude-sonnet-5.5`

#### Scenario: Claude Code does not receive Copilot IDs
- **WHEN** a launch uses provider `claude-code` at tier medium
- **THEN** the launcher arguments contain `--model=claude-sonnet-5-5`

### Requirement: Built-in Google tiers
The `gateway-gemini` row SHALL use fast `gemini-4-hydrogen`, medium
`gemini-4-argon`, high `gemini-4-neon` and frontier `gemini-4-carbon`. The
`copilot-gemini` row SHALL offer only fast `gemini-4-hydrogen`.

#### Scenario: Gemini frontier through the gateway
- **WHEN** a launch uses provider `gateway-gemini` at tier frontier
- **THEN** the launch uses model `gemini-4-carbon` through the agent gateway

### Requirement: Missing tiers are disabled, never substituted
When a provider row has no model for the requested tier, sbx-tui SHALL show that
provider as unavailable for the selection with the reason, and SHALL NOT launch
it with a model from another tier or another row.

#### Scenario: Frontier persona on Copilot/Anthropic
- **GIVEN** a persona with tier frontier
- **WHEN** the user selects provider Copilot/Anthropic for it
- **THEN** the form shows that Copilot/Anthropic has no frontier model and does not launch

### Requirement: Gateway rows use the agent gateway
A gateway row SHALL launch through `network.agent_proxy` with the agent named by
`catalogue.gateway_agent` (`pi` by default, or `opencode`). Without a configured
gateway, gateway rows SHALL be unavailable with that reason.

#### Scenario: Gateway row with OpenCode
- **GIVEN** a configured gateway and `catalogue.gateway_agent = "opencode"`
- **WHEN** a launch uses provider `gateway-anthropic` at tier high
- **THEN** the launcher arguments contain `--agent=opencode`, `--agent-proxy=<url>` and `--model=agent-proxy/claude-opus-5-5`

#### Scenario: No gateway configured
- **GIVEN** `network.agent_proxy` is empty
- **WHEN** the user opens the provider field
- **THEN** the gateway rows are marked unavailable because no agent gateway is configured

### Requirement: Effort and fast mode follow the row
A launch from a provider row SHALL pass the requested effort, except for
`gateway-gemini`, which SHALL omit `--effort` and show that the provider default
applies. Fast mode SHALL be passed only for the `codex` and `claude-code` rows.

#### Scenario: Gemini gateway effort
- **WHEN** a launch uses provider `gateway-gemini` with requested effort high
- **THEN** the launcher arguments contain no `--effort`, and the preview shows the provider default effort

#### Scenario: Fast mode on a gateway row
- **WHEN** a launch with fast mode requested uses provider `gateway-openai`
- **THEN** the launcher arguments contain no `--fast`

### Requirement: Catalogue overrides
`[catalogue.models.<vendor>]` and `[catalogue.models.<row>]` SHALL override tier
models, a row entry taking precedence over its vendor entry. An empty value
SHALL make that tier unavailable for the row. Model IDs SHALL be used exactly as
configured.

#### Scenario: Pin a gateway model
- **GIVEN** `[catalogue.models.gateway-anthropic] high = "claude-opus-5"`
- **WHEN** a launch uses provider `gateway-anthropic` at tier high
- **THEN** the launch uses model `claude-opus-5`, while `claude-code` at tier high still uses `claude-opus-5-5`

#### Scenario: Unknown row in overrides
- **WHEN** the config defines `[catalogue.models.gateway-mistral]`
- **THEN** loading fails with an error naming `catalogue.models.gateway-mistral`

### Requirement: Default provider
`catalogue.default_provider` SHALL name the provider a selection starts with
when none is remembered. When unset, the first available row SHALL be used.

#### Scenario: Configured default provider
- **GIVEN** `catalogue.default_provider = "claude-code"` and no remembered provider
- **WHEN** the user opens a launch form in persona mode
- **THEN** the provider field starts at Claude Code

### Requirement: Provider availability follows agent availability
A provider row SHALL be unavailable, with the reason, when its agent is not
supported by the launcher or not available on this host through any source.

#### Scenario: Codex without host configuration
- **GIVEN** the host has no Codex configuration
- **WHEN** the user opens the provider field
- **THEN** Codex is marked unavailable with the missing configuration as the reason
