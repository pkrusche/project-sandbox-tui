# Model suggestions

In a launch form, select **custom** in the **profile** field to show individual
agent and model controls. The same controls are available when creating or
editing an [agent profile](configuration.md#agent-profiles) with
`:config-profiles`. A named profile uses its saved model.

Cycle models with `h`/`l` or `←`/`→`. Except for `bash` without the agent
gateway, **custom (enter model name)** then `tab` lets you type a name. Blank uses
the agent default.

For agents that use the [agent gateway](configuration.md#agent-gateway) (`pi`,
`opencode` and `bash` with **gateway** on), list the gateway's plain model IDs
under `[models]`, for example `pi = ["gpt-5-mini"]`. sbx-tui does not query the
gateway; the bundled `sbx-tui-config` skill shows the setup agent how to list
them. A gateway launch needs a model.

Suggestion order:

1. Your `[models]` configuration.
2. Built-in Claude Code aliases.
3. Codex's `$CODEX_HOME/models_cache.json` (default `~/.codex`).
4. The downloaded catalog, newest first.

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
