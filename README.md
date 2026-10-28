# project-sandbox-tui

A [vim](https://www.vim.org/)/[ranger](https://ranger.fm/)-style terminal UI for
running coding agents in [project-sandbox](https://github.com/pkrusche/project-sandbox)
containers, with one sandbox per [jj bookmark](https://docs.jj-vcs.dev/latest/bookmarks/).

> 🤖 Written largely by AI, reviewed by a human. Use at your own risk.

- Run tasks in the background and get notified when they finish, or steer agents interactively.
- Keep tasks in separate jj workspaces; select several to merge or rebase with agent-assisted integration.
- Delegate container isolation, credentials and workspace teardown to project-sandbox.

```text
 sbx-tui   ~/workspace/myproject   4 bookmarks · 1 running
   bookmark          head      subject                       status       agent              when
   @ working copy ●  qzmvtlno  (no description set)
   main ●            kxyrwpsn  Initial import
 ▶ feature/parser    sutnpmvo  Add streaming parser          running      claude/opus/high   12m
 ✓ fix/flaky-tests   rlvkpnrz  Stabilise integration tests   completed    codex/gpt-5.4      2m ago
 ✗ spike/tui         zsuskuln  Try a ranger-style layout     failed (1)   claude/sonnet      1h ago
```

## Requirements

- Linux or macOS (WSL on Windows)
- Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/)
- [jj](https://github.com/jj-vcs/jj) on `PATH`; for an existing Git repository, run `jj git init --colocate`
- [project-sandbox](https://github.com/pkrusche/project-sandbox) and Apple Containers, Docker or Podman

## Getting started

```sh
uv tool install project-sandbox
uv tool install git+https://github.com/pkrusche/project-sandbox-tui
# From a checkout: uv tool install . (or uv sync && uv run sbx-tui)

cd ~/workspace/myproject
sbx-tui --init      # write config templates; set the image source in .project-sandbox-tui.toml
sbx-tui doctor      # check dependencies, runtime and configuration
sbx-tui --dry-run   # preview project-sandbox calls
sbx-tui             # launch
```

Press `?` for full help. The main-window cheat-sheet collapses to one line when
space is tight, leaving room for the bookmark list.

## Documentation

- [Usage and keys](docs/usage.md)
- [Sessions, terminals and notifications](docs/sessions.md)
- [Merging and rebasing](docs/merging.md)
- [Workspace cleanup](docs/cleanup.md)
- [Configuration](docs/configuration.md)
- [Model suggestions](docs/models.md)
- [Architecture](docs/architecture.md)
- [Development](docs/development.md)

## License

[MIT](LICENSE)
