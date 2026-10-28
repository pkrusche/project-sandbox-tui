# Development

```sh
uv sync
uv run pytest                 # unit, Textual Pilot and pty tests; needs jj on PATH
uv run pytest -n auto         # the same, in parallel (pytest-xdist)
uv run ruff check .           # lint
uv run ruff format --check .  # formatting (drop --check to apply)
uv run ty check               # types (src and tests; warnings are errors)
```

CI (`.github/workflows/ci.yml`) runs lint (ruff check, ruff format, ty) and
pytest on Linux with Python 3.12 and 3.14 and jj, on pushes to `main` and pull
requests. macOS is supported but not yet tested in CI.

## Version pinning

- Runtime dependencies have lower and upper bounds in `pyproject.toml`:
  `uv tool install` ignores `uv.lock`, so untested major versions stay out.
- Dev tools (ruff, ty) are pinned exactly; everything else is locked in
  `uv.lock`. CI installs with `uv sync --locked`, so commit `uv.lock` whenever
  `pyproject.toml` changes (`uv lock`).
- CI pins uv (`UV_VERSION`, matched by `[tool.uv] required-version`), GitHub
  Actions by commit SHA, and the jj release by version and sha256
  (`JJ_VERSION`, `JJ_SHA256`; update both together).
- Dependabot (`.github/dependabot.yml`) proposes updates to the action SHAs and
  `uv.lock` monthly.

Tests use `tests/fake_project_sandbox.py`, a stand-in CLI that writes session
records, logs, a transcript and a JSON summary like project-sandbox does.
`tests/test_pty_session.py` drives the real TUI in a pseudo-terminal.

See [architecture.md](architecture.md) for the design.
