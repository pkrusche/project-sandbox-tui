# Development

```sh
uv sync --locked
scripts/test.sh              # pytest-xdist, -n auto; needs jj on PATH
scripts/test.sh tests/test_vcs.py -k merge  # arguments pass through to pytest
uv run ruff check .
uv run ruff format --check .  # omit --check to format
uv run ty check              # src and tests; warnings are errors
uv build                    # wheel built from the source distribution
```

[CI](../.github/workflows/ci.yml) runs these checks on Linux with Python 3.12
and 3.14 on pushes to `main` and pull requests; macOS is not tested in CI.
The packaging job also installs the wheel in a clean environment, checks the
CLI and imports the application outside the checkout. The packaging regression
test seeds private files and jj metadata into an isolated checkout and verifies
that the source archive excludes them.

## Tests

Tests cover units, Textual Pilot interactions and pseudo-terminal sessions.
`fake_project_sandbox.py` supplies session records, logs and summaries.

- Keep state in `tmp_path`, never shared or global paths: tests must pass in
  parallel. `conftest.py` isolates `XDG_*` and `CODEX_HOME` per test.
- Use `repo`, `jj_repo`, `jj_repo_no_bookmarks`, or `repo_copy(build, dest)` for
  a new layout. Each test gets a mutable copy of a repo built once per worker.

## Real container smoke test

The opt-in smoke test launches the real `project-sandbox` **bash** agent through
`JobManager`. It checks a successful run, exit code 7, and a timeout (124), plus
live session matching, duplicate-launch prevention, adoption by a new manager,
logs and JSON summaries, bookmark capture, notification persistence, and stopped
containers. It uses no model API or credentials and keeps the firewall enabled.
Bash produces plain-text logs, so a Markdown transcript is not required.

Install `project-sandbox` ≥ 0.2.2 and jj ≥ 0.46, start Docker, Podman, or Apple
Containers, then run:

```sh
uv tool install project-sandbox==0.2.2  # version used by the manual CI smoke job
scripts/test.sh -n 0 -v -s tests/test_container_smoke.py \
  --container-smoke --container-runtime docker
```

Use `--container-runtime podman`, `apple-container`, or `auto` as appropriate.
`--container-command 'uv run --project /path/to/project-sandbox project-sandbox'`
tests a source checkout; `--container-base-image IMAGE` overrides the default
`python:3.12-slim` base. The first run downloads/builds an image and has a
20-minute build deadline; the other runs reuse it. Once opted in, missing or
unhealthy dependencies fail the test. Ordinary test runs skip it without
probing a runtime.

All repository and session state lives under pytest's temporary directory. For
Apple Containers (whose build VM cannot read macOS's per-user temp directory),
or Docker Desktop with restricted file sharing, use a fresh directory in a
runtime-accessible location:

```sh
mkdir -p .project-sandbox
smoke_tmp=$(mktemp -d "$PWD/.project-sandbox/container-smoke.XXXXXX")
scripts/test.sh -n 0 -v -s tests/test_container_smoke.py \
  --container-smoke --container-runtime apple-container --basetemp "$smoke_tmp"
```

`--basetemp` must be disposable: pytest clears it before use. Failure messages
include output/log paths and tails. The test stops its child processes and
removes only its recorded containers and uniquely tagged image in `finally`;
repository files and logs remain for inspection under the pytest temp directory.
Downloaded base images and shared build-cache layers are retained.

In GitHub Actions, manually run **CI** with **container_smoke** enabled to run
the Docker smoke on Python 3.12. This does not add container builds to normal
pushes or pull requests. See the upstream
[bash-agent workflow](https://github.com/pkrusche/project-sandbox/blob/v0.2.2/scripts/e2e-jj-workflow.sh)
for the underlying sandbox behavior.

## Dependency updates

- Runtime bounds in `pyproject.toml` exclude untested major versions:
  `uv tool install` ignores the lockfile.
- Ruff and ty are pinned exactly; `uv.lock` locks the rest. Run `uv lock` and
  commit it after dependency changes; CI uses `uv sync --locked`.
- CI pins uv (`UV_VERSION`; keep `[tool.uv] required-version` compatible),
  actions by SHA, and jj by `JJ_VERSION` plus `JJ_SHA256` (update together).
- [Dependabot](../.github/dependabot.yml) proposes monthly action and lockfile updates.

See [architecture](architecture.md) for modules and job handling.
