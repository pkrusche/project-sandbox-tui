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
  parallel. `conftest.py` isolates XDG state, config, data and runtime directories,
  `CODEX_HOME` and `JJ_CONFIG` per test. The offline packaging test reuses uv's
  dependency cache; its checkout and build artifacts live in `tmp_path`.
- `conftest.py` tracks real child processes and stops and reaps them at teardown,
  including detached job/task groups when an assertion fails. Keep explicit
  cleanup for resources outside those groups, such as containers and tmux servers.
  On Linux it also adopts and reaps orphaned descendants in those groups, so tests
  do not depend on a container's PID 1 to collect them.
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
uv tool install project-sandbox==0.3.0  # version used by the manual CI smoke job
scripts/test.sh -n 0 -v -s tests/test_container_smoke.py \
  --container-smoke --container-runtime docker
```

Or let `scripts/e2e-test.sh` check jj and project-sandbox against the minimums
in `doctor.py`, pick the first responding runtime (Apple container on macOS,
then Docker, then Podman), and run the command above. `--runtime NAME` (or
`SBX_E2E_RUNTIME`) overrides detection, `--install` installs the CI-pinned
project-sandbox when it is missing or too old, `--command CMD` tests another
project-sandbox command, and arguments after `--` go to pytest. For Apple
Containers it creates the `--basetemp` directory described below.

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
[bash-agent workflow](https://github.com/pkrusche/project-sandbox/blob/v0.3.0/scripts/e2e-jj-workflow.sh)
for the underlying sandbox behavior.

## Dependency updates

Run `uv run scripts/update-pins.py` to review updates interactively, or add
`--dry-run` to list candidates without changing files. `--yes` accepts all
updates. Stable releases must be at least 24 hours old by default;
`--min-age-hours HOURS` changes that threshold. The script checks exact Python
pins, registry packages in `uv.lock`, CI's uv version, action SHAs and version
comments, jj and its archive checksum, and the project-sandbox smoke-test pin
(including its documentation). Optional `GH_TOKEN`/`GITHUB_TOKEN` authentication
avoids GitHub's anonymous API rate limit.

Lockfile updates retain the project's dependency bounds, so the resolver may
select an older compatible release than the displayed upstream candidate, or
leave a package unchanged. The release-age cutoff also applies during resolution.
Review the diff and run the checks above after updates. The script does not
change runtime compatibility bounds or `[tool.uv] required-version`; check that
the selected CI uv version remains compatible when changing that requirement.

- Runtime bounds in `pyproject.toml` exclude untested major versions:
  `uv tool install` ignores the lockfile.
- Ruff and ty are pinned exactly; `uv.lock` locks the rest. Run `uv lock` and
  commit it after dependency changes; CI uses `uv sync --locked`.
- CI pins uv (`UV_VERSION`; keep `[tool.uv] required-version` compatible),
  actions by SHA, and jj by `JJ_VERSION` plus `JJ_SHA256` (update together).
- [Dependabot](../.github/dependabot.yml) proposes monthly action and lockfile updates.

## Releases

Run `scripts/make-release.sh` from a checkout with uv, git, jj, and an authenticated
GitHub CLI (`gh`) on PATH. For Git, start on clean `main` matching `origin/main`.
For jj (including non-colocated workspaces), start with an empty working copy
whose parent is immutable `main` and `main@origin`. The script also checks the
live remote main commit.

Choose a version bump or leave the version unchanged. A bump updates the project
and lockfile and records a Git commit or jj change, then stops. Move the jj
`main` bookmark to that change if needed, push main, and rerun the script.
It runs the locked install, Ruff lint and formatting, ty, and `scripts/test.sh`,
then asks you to confirm the real container smoke test described above was run
for that commit. It builds the sdist and wheel and checks the installed CLI and
application import in a separate environment before creating a GitHub release
at the verified commit. GitHub release creation, TestPyPI, and PyPI each require
confirmation; registry tokens are read without echo and passed only to uv.

Progress and artifacts are kept under `.release-status/<version>-<commit>/`.
Rerun to resume after a declined prompt or failed step; completed steps are
skipped only for the same version and commit. Artifact hashes are checked before
publishing, and only that release's wheel and sdist are uploaded. Existing remote
tags/releases are rejected rather than overwritten. If a remote operation succeeds
but its local progress marker is not saved, inspect the remote result before
reconciling the corresponding marker; blindly restarting can attempt a duplicate
upload. Keep the state directory until the release is complete. It is ignored by
version control and excluded from distribution archives.

See [architecture](architecture.md) for modules and job handling.
