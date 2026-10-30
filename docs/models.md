# Model suggestions

Cycle models with `h`/`l` or `←`/`→`. Except for `bash`, **custom (enter model
name)** then `tab` lets you type a name. Blank uses the agent default.

Suggestion order:

1. Your `[models]` configuration.
2. Built-in Claude Code aliases.
3. Codex's `$CODEX_HOME/models_cache.json` (default `~/.codex`).
4. The downloaded catalog, newest first.

## Updating the catalog

Only `--update-models` (or `--init` without a catalog) fetches data:

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
