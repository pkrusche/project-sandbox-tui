# Development

```sh
uv sync
uv run pytest                 # unit, Textual Pilot and pty tests; needs jj on PATH
uv run ruff check .           # lint
uv run ruff format --check .  # formatting (drop --check to apply)
uv run ty check               # types (src and tests; warnings are errors)
```

CI (`.github/workflows/ci.yml`) runs lint (ruff check, ruff format, ty) and
pytest on Linux with Python 3.14 and jj, on pushes to `main` and pull requests.
Dependencies install with `uv sync --locked`, so commit `uv.lock` whenever
`pyproject.toml` changes.

Tests use `tests/fake_project_sandbox.py`, a stand-in CLI that writes session
records, logs, a transcript and a JSON summary like project-sandbox does.
`tests/test_pty_session.py` drives the real TUI in a pseudo-terminal.

See [architecture.md](architecture.md) for the design.
