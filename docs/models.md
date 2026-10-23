# Model suggestions

The model field cycles with `h`/`l` or `←`/`→`, including an agent-default
option. For any agent except `bash`, choose `custom (enter model name)` and
press `tab` to type one; a blank name uses the agent default. Custom names are
remembered per agent.

Suggestions come from, in order:

1. `[models]` in your configuration
2. Claude Code aliases (`opus`, `sonnet`, `haiku`, `opusplan`, `best`, `fable`, `opus[1m]`, `sonnet[1m]`)
3. Codex's list, `$CODEX_HOME/models_cache.json` (default `~/.codex`)
4. the downloaded list, newest first

## Updating the list

The TUI never goes online by itself. Only `sbx-tui --update-models` (and
`--init` when there is no list yet) downloads from [models.dev](https://models.dev):

```sh
sbx-tui --update-models                  # https://models.dev/api.json
sbx-tui --update-models ~/api.json       # a local file in the same format
sbx-tui --update-models https://mirror.example/api.json
```

Only `https://` URLs and local files are accepted; proxies come from
`HTTPS_PROXY`. The download is capped at 20 MB and validated. Only recent
(last year), non-deprecated Anthropic and OpenAI models with reasoning and tool
calling are kept, with release date, context size and list prices, in
`~/.local/state/project-sandbox-tui/models.json`. The file is replaced
atomically, so a failed update keeps the previous list. The launch form warns
when there is no list or it is older than 30 days.
