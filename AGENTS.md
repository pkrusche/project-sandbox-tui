# Agent notes

## Testing

Run the tests with `scripts/test.sh` (pytest in parallel via pytest-xdist, `-n auto`);
extra arguments go to pytest, e.g. `scripts/test.sh tests/test_vcs.py -k merge`.
The tests need `jj` on `PATH`. Lint and types: see [docs/development.md](docs/development.md).

- Tests must pass in parallel: keep state in `tmp_path`, never in shared or global paths.
  `conftest.py` points `XDG_*` and `CODEX_HOME` at `tmp_path` for every test.
- For jj repos, use the `repo`, `jj_repo` and `jj_repo_no_bookmarks` fixtures, or
  `repo_copy(build, dest)` for a new layout: each repo is built once per worker
  and every test gets its own copy, so tests may change it freely.
