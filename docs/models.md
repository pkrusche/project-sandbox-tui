# Model suggestions

Cycle models with `h`/`l` or `←`/`→`. Except for `bash`, choose **custom (enter
model name)** and press `tab` to type one. Blank uses the agent default; custom
names are remembered per agent.

Suggestions come from these sources, in order:

1. Your `[models]` configuration.
2. Built-in Claude Code aliases.
3. Codex's `$CODEX_HOME/models_cache.json` (default `~/.codex`).
4. The downloaded catalog, newest first.

## Updating the catalog

Only `--update-models`, or `--init` when no catalog exists, fetches model data:

```sh
sbx-tui --update-models                   # https://models.dev/api.json
sbx-tui --update-models ~/api.json        # local file in the same format
sbx-tui --update-models https://mirror.example/api.json
```

Downloads accept HTTPS URLs, use `HTTPS_PROXY` and are capped at 20 MB. The
validated catalog keeps non-deprecated Anthropic and OpenAI models released in
the last year with reasoning and tool calling, including context sizes and
prices. Failed updates preserve the previous catalog. The launch form warns
when it is missing or older than 30 days.

See [state storage](configuration.md#state-directory) for the catalog location.
