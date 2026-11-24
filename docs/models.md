# Models, providers and personas

A launch form's **setup** field selects how the agent is chosen:

- **persona** picks a *provider* and a *persona*. sbx-tui resolves them to the
  agent, model, effort and fast mode from its built-in model catalogue, and adds
  no profile or custom CLI arguments.
- A named [agent profile](configuration.md#agent-profiles) uses its saved settings.
- **custom** shows individual agent and model controls. The same controls are
  available when creating or editing a profile with `:config-profiles`.

## Provider rows

A provider is an access route plus a model vendor, with a fixed agent:

| Provider | Agent | Model IDs |
| --- | --- | --- |
| Codex (`codex`) | codex | OpenAI |
| Claude Code (`claude-code`) | claude | Anthropic |
| Gateway/OpenAI (`gateway-openai`) | `catalogue.gateway_agent` (pi or opencode) | OpenAI, through `network.agent_proxy` |
| Gateway/Anthropic (`gateway-anthropic`) | as above | Anthropic, through the gateway |
| Gateway/Gemini (`gateway-gemini`) | as above | Google, through the gateway; no `--effort` |
| Copilot/OpenAI (`copilot-openai`) | copilot | OpenAI |
| Copilot/Anthropic (`copilot-anthropic`) | copilot | Copilot's dotted Claude IDs |
| Copilot/Fusion (`copilot-fusion`) | copilot | Copilot Fusion picks the model; no `--effort` |

Each persona asks for a *tier*. The built-in tier models are:

| Provider | fast | medium | high | frontier |
| --- | --- | --- | --- | --- |
| Codex, Gateway/OpenAI, Copilot/OpenAI | `gpt-6-luna` | `gpt-6.1-terra` | `gpt-6.2-sol` | `gpt-6.2-astra` |
| Claude Code, Gateway/Anthropic | `claude-haiku-5-5` | `claude-sonnet-5-5` | `claude-opus-5-5` | `claude-fable-5-2` |
| Copilot/Anthropic | `claude-haiku-5.5` | `claude-sonnet-5.5` | `claude-opus-5.5` | — |
| Gateway/Gemini | `gemini-4-hydrogen` | `gemini-4-neon` | `gemini-4-argon` | `gemini-4-carbon` |
| Copilot/Fusion | `auto` | `auto` | `auto` | `auto` |

A provider without the requested tier is shown as unavailable with the reason,
for example "Copilot/Anthropic has no frontier model", and the form refuses to
launch. sbx-tui never substitutes another tier or provider. Fast mode is passed
only on Codex and Claude Code. Gateway/Gemini omits `--effort` and the preview
says the provider default applies. Copilot/Fusion passes `--model=auto` at
every tier and no `--effort`: Copilot Fusion routes each request with the
routing profile and Fusion tier from your Copilot configuration
(`copilot config autoTier` and `copilot config fusionTier`), because the
launcher cannot pass `--auto-tier`. OpenCode gateway launches name the model as
`agent-proxy/<id>`. Copilot launches also allow GitHub hosts.

A provider is also unavailable when the launcher does not support its agent,
when the agent cannot run on this host (for example without `~/.codex`), or,
for gateway rows, when no agent gateway is configured. `sbx-tui doctor` lists
every provider with its availability.

List the rows you use in
[`catalogue.providers`](configuration.md#model-catalogue-and-personas) to hide
the others from launch forms and the workflow picker.

Pin or disable models with
[`[catalogue.models]`](configuration.md#model-catalogue-and-personas). Model
IDs are used exactly as configured.

## Personas

| Persona | Tier | Effort | Fast | Level |
| --- | --- | --- | --- | --- |
| planner | high | high | off | 3 |
| checker | medium | low | on | 2 |
| implementer | medium | medium | off | 2 |
| debugger | frontier | xhigh | off | 2 |
| chore-runner | fast | medium | on | 2 |
| reviewer | high | high | off | 4 |

The level is the persona's starting [security level](configuration.md#security-levels).
`[personas.NAME]` overrides fields or adds personas. Commit review uses an
internal `commit-reviewer` persona (fast tier, low effort) that the forms do
not offer.

## Model suggestions

In custom mode, cycle models with `h`/`l` or `←`/`→`. Except for `bash` without
the agent gateway, **custom (enter model name)** then `tab` lets you type a
name. Blank uses the agent default.

For agents that use the [agent gateway](configuration.md#agent-gateway) (`pi`,
`opencode` and `bash` with **gateway** on), list the gateway's plain model IDs
under `[models]`, for example `pi = ["gpt-5-mini"]`. sbx-tui does not query the
gateway; the bundled `sbx-tui-config` skill shows the setup agent how to list
them. A gateway launch needs a model.

Suggestion order:

1. Your `[models]` configuration.
2. Catalogue models of the provider rows that use the agent, in row and tier
   order (for `codex`: `gpt-6-luna`, `gpt-6.1-terra`, `gpt-6.2-sol`,
   `gpt-6.2-astra`). Doctor accepts these as known models.
3. Built-in Claude Code aliases.
4. Codex's `$CODEX_HOME/models_cache.json` (default `~/.codex`).
5. The downloaded catalog, newest first.

## Updating the catalog

Use `:update-models` in the TUI to refresh the catalog from models.dev. To use a
local file or HTTPS URL, pass it as an argument like `:update-models "my catalog.json"`.
The TUI resolves relative paths from the repository root, and the updated
suggestions appear the next time you open a launch form.

The same sources work from the CLI.

```sh
sbx-tui --update-models                   # https://models.dev/api.json
sbx-tui --update-models ~/api.json        # local file in the same format
sbx-tui --update-models https://mirror.example/api.json
```

Downloads require HTTPS, respect `HTTPS_PROXY` and have a 20 MB limit. The catalog
keeps non-deprecated Anthropic and OpenAI models released in the last year with
reasoning and tool calling, including context sizes and prices. Failed validation
or downloads preserve the previous catalog. The form warns if it is missing or
over 30 days old.

See [state storage](configuration.md#state-directory) for the catalog location.
