# Spec Delta

## Purpose

Lets each launch choose whether its agent goes through the configured agent
gateway, so profiles can mix gateway agents with agents that use host
credentials, and builds launcher arguments the launcher accepts.

## ADDED Requirements

### Requirement: The gateway is configured once and used per launch
`network.agent_proxy` and `network.agent_proxy_key_env` SHALL only define the
gateway. A launch SHALL use the gateway only when its profile, the launch form,
or `defaults.gateway` turns gateway use on. Gateway use is off by default.

#### Scenario: Claude with a gateway configured
- **GIVEN** `network.agent_proxy = "http://127.0.0.1:4000/v1"` and a profile with agent claude
- **WHEN** the user launches that profile
- **THEN** the launcher arguments contain no `--agent-proxy` or `--agent-proxy-key-env`

#### Scenario: Pi profile through the gateway
- **GIVEN** a configured gateway and `[profiles.local] agent = "pi"`, `model = "gpt-5-mini"`, `gateway = true`
- **WHEN** the user launches that profile
- **THEN** the launcher arguments contain `--agent-proxy=<url>`, the key variable if configured, and `--model=gpt-5-mini`

#### Scenario: Same agent from two sources
- **GIVEN** a configured gateway, a Pi profile with `gateway = true` and another Pi profile without it
- **WHEN** the user launches each profile
- **THEN** only the first launch uses the gateway; the second uses Pi's host credentials

### Requirement: Gateway use requires a configured gateway
Loading configuration SHALL fail when a profile or `defaults.gateway` turns
gateway use on but the merged configuration has no `network.agent_proxy`.

#### Scenario: Profile opts in without a gateway
- **GIVEN** `[profiles.local] gateway = true` and no `network.agent_proxy`
- **WHEN** sbx-tui loads the configuration
- **THEN** loading fails with an error naming `profiles.local.gateway` and `network.agent_proxy`

### Requirement: Gateway model arguments match the launcher's rules
For a gateway launch, sbx-tui SHALL always pass `--model`, including for bash.
For OpenCode it SHALL pass the model as `agent-proxy/<id>`, adding the prefix
when the configured model lacks it. Pi and bash models are passed as given.

#### Scenario: OpenCode model without prefix
- **GIVEN** a gateway launch with agent opencode and model `gpt-5-mini`
- **WHEN** sbx-tui builds the launcher arguments
- **THEN** they contain `--model=agent-proxy/gpt-5-mini`

#### Scenario: Bash through the gateway
- **GIVEN** a gateway launch with agent bash and model `gpt-5-mini`
- **WHEN** sbx-tui builds the launcher arguments
- **THEN** they contain `--model=gpt-5-mini`

### Requirement: Launch form offers gateway use for capable agents
When a gateway is configured, the launch form and profile editor SHALL show a
gateway on/off field for pi, opencode and bash only. Turning it on SHALL
require a model before launch. The choice SHALL be remembered per agent, like
fast mode.

#### Scenario: Gateway field for Codex
- **GIVEN** a configured gateway
- **WHEN** the user selects agent codex
- **THEN** no gateway field is shown, and the launch does not use the gateway

#### Scenario: Gateway on without a model
- **GIVEN** agent opencode with gateway on and the model left at the agent default
- **WHEN** the user starts the launch
- **THEN** the form asks for a model and does not run the launcher
