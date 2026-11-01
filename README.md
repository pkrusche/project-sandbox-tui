# project-sandbox-tui

[![PyPI](https://img.shields.io/pypi/v/project-sandbox-tui)](https://pypi.org/project/project-sandbox-tui/)

A [vim](https://www.vim.org/)/[ranger](https://ranger.fm/)-style terminal UI for
running coding agents in [project-sandbox](https://github.com/pkrusche/project-sandbox)
containers, with one sandbox per [jj bookmark](https://docs.jj-vcs.dev/latest/bookmarks/).

> 🤖 Written largely by AI, reviewed by a human. Use at your own risk.

- Run agents interactively or in the background with completion notifications.
- Merge or rebase tasks from separate jj workspaces with agent assistance.
- Follow each bookmark's prompts and merges in a history graph (`L` for every run); delete and undo with `dd` / `u`.
- Run shell or configured commands in the foreground (`:!make`, `:test`), once in the repository background (`:&test`), or in each selected workspace (`:*test`); see the [command comparison](docs/usage.md#choosing-how-to-run-a-shell-command). Map any form to leader keys.
- Let project-sandbox handle isolation, credentials and workspace teardown.

```text
 sbx-tui   ~/workspace/myproject   5 bookmarks · 1 running
         bookmark          vs main    reviewed  head      subject                       status
         @ working copy ●  ↑1                   qzmvtlno  (no description set)
 ▶─╮     merge/parser-tui  ↑6 Δ9      0/9       wnrtlxqz  Merge feature/parser, spike…  merging
 ✓ │     feature/parser    ↑3 Δ5      5/5       sutnpmvo  Add streaming parser          completed
 │ ✗     spike/tui         ↑2 ↓1 Δ4   1/4       zsuskuln  Try a ranger-style layout     failed (1)
 │ │ ○   fix/flaky-tests   ↑1 Δ2      0/2       rlvkpnrz  Stabilise integration tests
 ○─┴─╯   main ●            =                    kxyrwpsn  Initial import
```

## Requirements

- Linux or macOS (WSL on Windows)
- Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/)
- [jj](https://github.com/jj-vcs/jj) ≥ 0.46 on `PATH`; for an existing Git repository, run `jj git init --colocate`
- [project-sandbox](https://github.com/pkrusche/project-sandbox) ≥ 0.2.2 and Apple Containers, Docker or Podman

## Getting started

```sh
uv tool install project-sandbox
uv tool install project-sandbox-tui
# For latest from Github:
# uv tool install git+https://github.com/pkrusche/project-sandbox-tui
# From a checkout: uv tool install . (or uv sync && uv run sbx-tui)

cd ~/workspace/myproject
sbx-tui --init      # detect the project type, write config and install a local setup skill
sbx-tui doctor      # check dependencies, runtime and configuration
sbx-tui --dry-run   # preview project-sandbox calls
sbx-tui             # launch
```

Press `?` for full help.

`--init` detects a root `Dockerfile`, Python/uv, Rust/Cargo or Node/npm project
(in that order). It installs a gitignored `sbx-tui-config` agent skill to help
tailor the configuration. Existing config and skill files are preserved; see
[initialization](docs/configuration.md#initialization) for detection and skill paths.

## Documentation

- [Usage and keys](docs/usage.md)
- [Worked example: foreground and background commands](docs/command-workflow.md)
- [Sessions, terminals and notifications](docs/sessions.md)
- [Merging and rebasing](docs/merging.md)
- [Workspace cleanup](docs/cleanup.md)
- [Configuration](docs/configuration.md)
- [Model suggestions](docs/models.md)
- [Architecture](docs/architecture.md)
- [Development](docs/development.md)

## License

[MIT](LICENSE)
