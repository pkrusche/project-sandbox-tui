# project-sandbox-tui

A minimal [vim](https://www.vim.org/)/[ranger](https://ranger.fm/)-style terminal UI for
agentic engineering using [project-sandbox](https://github.com/pkrusche/project-sandbox). 

> 🤖 Written largely by AI, reviewed by a human. Use at your own risk.

Key ideas:

- We want to run many unsupervised coding tasks in the background and sometimes steer using an interactive agent session.
- Each coding task lives in a specific [bookmark](https://docs.jj-vcs.dev/latest/bookmarks/)
  and workspace in [jj](https://github.com/jj-vcs/jj).
- When many tasks are done we want some automation to bring them together. This is accomplished through merging or rebasing, 
  or octopus merging, followed agentic conflict resolution.
- We use `jj` since this natively supports this type of merge without needing interactive interventions.
- All of this in a simple text-based UI with familiar keyboard shortcuts.
- Differently from most other open source agent orchestrators, we use containers for our agents to do all this somewhat more safely.

`sbx-tui` lists the
bookmarks of a [jj](https://github.com/jj-vcs/jj) repository and runs **one
agent / shell sandbox per bookmark**, interactively in your terminal or unsupervised in the
background with a prompt, and notifies you when background work finishes.

```
 sbx-tui   ~/workspace/myproject   4 bookmarks · 1 running
   bookmark          head      subject                       status       agent              when
   @ working copy ●  qzmvtlno  (no description set)
   main ●            kxyrwpsn  Initial import
 ▶ feature/parser    sutnpmvo  Add streaming parser          running      claude/opus/high   12m
 ✓ fix/flaky-tests   rlvkpnrz  Stabilise integration tests   completed    codex/gpt-5.4      2m ago
 ✗ spike/tui         zsuskuln  Try a ranger-style layout     failed (1)   claude/sonnet      1h ago
```

The TUI only shells out to the `project-sandbox` CLI and to `jj`; isolation,
credentials and workspace teardown stay in project-sandbox.

## Requirements

- Linux or macOS (on Windows, use WSL). CI tests Linux with Python 3.12 and 3.14.
- Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/)
- `jj` on your `PATH`. Existing git repositories must be colocated first:
  `jj git init --colocate`
- [project-sandbox](https://github.com/pkrusche/project-sandbox)
- a supported container runtime: Apple Containers, Docker, Podman.

## Getting started

```sh
uv tool install project-sandbox   # the sandbox itself
uv tool install git+https://github.com/pkrusche/project-sandbox-tui
# from a checkout instead: uv tool install .   (or: uv sync && uv run sbx-tui)

cd ~/workspace/myproject
sbx-tui doctor      # check jj, project-sandbox and the container runtime
sbx-tui --init      # write config templates; set the image source in .project-sandbox-tui.toml
sbx-tui --dry-run   # try the UI; every project-sandbox call gets --dry-run
sbx-tui             # the real thing
```

Press `?` in the TUI for help.

## Documentation

- [Usage and keys](docs/usage.md)
- [Sessions: bookmarks, interactive terminals, notifications](docs/sessions.md)
- [Merging and rebasing several bookmarks](docs/merging.md)
- [Cleaning up workspaces](docs/cleanup.md)
- [Configuration](docs/configuration.md)
- [Model suggestions](docs/models.md)
- [Architecture](docs/architecture.md)
- [Development](docs/development.md)

## License

[MIT](LICENSE)
