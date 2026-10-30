# Development

```sh
uv sync --locked
scripts/test.sh              # pytest-xdist, -n auto; needs jj on PATH
scripts/test.sh tests/test_vcs.py -k merge  # arguments pass through to pytest
uv run ruff check .
uv run ruff format --check .  # omit --check to format
uv run ty check              # src and tests; warnings are errors
```

[CI](../.github/workflows/ci.yml) runs these checks on Linux with Python 3.12
and 3.14 on pushes to `main` and pull requests; macOS is not tested in CI.

## Tests

Tests cover units, Textual Pilot interactions and pseudo-terminal sessions.
`fake_project_sandbox.py` supplies session records, logs and summaries.

- Keep state in `tmp_path`, never shared or global paths: tests must pass in
  parallel. `conftest.py` isolates `XDG_*` and `CODEX_HOME` per test.
- Use `repo`, `jj_repo`, `jj_repo_no_bookmarks`, or `repo_copy(build, dest)` for
  a new layout. Each test gets a mutable copy of a repo built once per worker.

## Dependency updates

- Runtime bounds in `pyproject.toml` exclude untested major versions:
  `uv tool install` ignores the lockfile.
- Ruff and ty are pinned exactly; `uv.lock` locks the rest. Run `uv lock` and
  commit it after dependency changes; CI uses `uv sync --locked`.
- CI pins uv (`UV_VERSION`; keep `[tool.uv] required-version` compatible),
  actions by SHA, and jj by `JJ_VERSION` plus `JJ_SHA256` (update together).
- [Dependabot](../.github/dependabot.yml) proposes monthly action and lockfile updates.

See [architecture](architecture.md) for modules and job handling.
