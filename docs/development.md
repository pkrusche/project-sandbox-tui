# Development

```sh
uv sync --locked
scripts/test.sh               # unit, Textual Pilot and pty tests in parallel; needs jj on PATH
uv run pytest                 # the same, serially
uv run ruff check .
uv run ruff format --check .  # omit --check to format
uv run ty check               # src and tests; warnings are errors
```

[CI](../.github/workflows/ci.yml) runs these checks on Linux with Python 3.12
and 3.14 for pushes to `main` and pull requests. macOS is supported but untested
in CI. Tests use `fake_project_sandbox.py` for session records, logs and
summaries; `test_pty_session.py` drives the TUI in a pseudo-terminal.

## Dependency updates

- Runtime bounds in `pyproject.toml` exclude untested major versions because
  `uv tool install` ignores the lockfile.
- Ruff and ty are pinned exactly; `uv.lock` locks the rest. Run `uv lock` and
  commit it whenever dependencies change; CI uses `uv sync --locked`.
- CI pins uv (`UV_VERSION`; keep `[tool.uv] required-version` compatible),
  actions by SHA, and jj by `JJ_VERSION` plus `JJ_SHA256` (update together).
- [Dependabot](../.github/dependabot.yml) proposes monthly action and lockfile updates.

See [architecture](architecture.md) for module responsibilities and job handling.
