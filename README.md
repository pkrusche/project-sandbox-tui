# project-sandbox-tui

A minimal vim/ranger-style terminal UI for
[project-sandbox](https://github.com/pkrusche/project-sandbox). It lists the
bookmarks of a [jj](https://github.com/jj-vcs/jj) repository and runs **one
sandbox per bookmark**, interactively in your terminal or unsupervised in the
background with a prompt, and notifies you when background work finishes.

```
 sbx-tui   ~/workspace/myproject   4 bookmarks · 1 running
   branch            head      subject                       status       agent              when
   @ working copy ●  qzmvtlno  (no description set)
   main ●            kxyrwpsn  Initial import
 ▶ feature/parser    sutnpmvo  Add streaming parser          running      claude/opus/high   12m
 ✓ fix/flaky-tests   rlvkpnrz  Stabilise integration tests   completed    codex/gpt-5.4      2m ago
 ✗ spike/tui         zsuskuln  Try a ranger-style layout     failed (1)   claude/sonnet      1h ago
```

The TUI only shells out to the `project-sandbox` CLI and to `jj`; isolation,
credentials and workspace teardown stay in project-sandbox.

## Requirements

- Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/)
- `jj` on your `PATH`. Existing git repositories must be colocated first:
  `jj git init --colocate`
- [project-sandbox](https://github.com/pkrusche/project-sandbox)

## Getting started

```sh
uv tool install project-sandbox            # the sandbox itself
uv tool install /path/to/project-sandbox-tui   # or: uv sync && uv run sbx-tui

cd ~/workspace/myproject
sbx-tui --init      # write config templates; set the image source in .project-sandbox-tui.toml
sbx-tui --dry-run   # try the UI; every project-sandbox call gets --dry-run
sbx-tui             # the real thing
```

Press `?` in the TUI for help.

## Documentation

- [Usage and keys](docs/usage.md)
- [Sessions: branches, interactive terminals, notifications](docs/sessions.md)
- [Merging and rebasing several branches](docs/merging.md)
- [Cleaning up workspaces](docs/cleanup.md)
- [Configuration](docs/configuration.md)
- [Model suggestions](docs/models.md)
- [Architecture](docs/architecture.md)
- [Development](docs/development.md)
