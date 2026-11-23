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

- Keep test state in `tmp_path`; never use shared or global paths. Tests must
  pass in parallel. `conftest.py` gives each test isolated XDG state, config,
  data and runtime directories, plus its own `CODEX_HOME` and `JJ_CONFIG`. The
  offline packaging test reuses uv's dependency cache but keeps its checkout
  and build artifacts in `tmp_path`.
- `conftest.py` tracks real child processes and stops and reaps them at teardown,
  including detached job/task groups and groups created with `process_group=0`
  after assertion failures. It also closes their pipes. Explicitly clean up
  resources outside those groups, such as containers and tmux servers. On Linux,
  it adopts and reaps orphaned descendants so tests do not rely on the container's
  PID 1 to collect them.
- Use `repo`, `jj_repo`, `jj_repo_no_bookmarks`, or `repo_copy(build, dest)` to
  create a layout. Each test gets a mutable copy of a repository built once per
  worker.
- Wait for observable completion instead of fixed startup delays. For screens
  opened by workers, use `wait_for_screen` in `tests/test_app.py`: screen type,
  focus and an empty table do not prove that `on_mount` has finished. For child
  processes, emit a readiness signal after setup and wait for it before sending
  input or signals.

## Real container smoke test

The opt-in suite runs credential-free Bash sessions through a real container
runtime and verifies job lifecycle, workspace skills, isolated commit summaries
and gateway egress. The gateway case also needs a configured egress sidecar.

Install `project-sandbox` ≥ 0.4.1 and jj ≥ 0.46, then start Docker, Podman or
Apple Containers before running:

```sh
uv tool install project-sandbox==0.4.1  # version used by the manual CI smoke job
scripts/test.sh -n 0 -v -s tests/test_container_smoke.py \
  --container-smoke --container-runtime docker
```

Alternatively, `scripts/e2e-test.sh` checks versions and selects a responding
runtime. Use `--runtime NAME` to choose one or `--install` to install the pinned
launcher. Pass pytest options after `--`.

For a runtime with restricted file sharing, provide a disposable, accessible
`--basetemp` outside any checkout. Pytest clears this directory. The smoke
suite retains logs there and cleans up its containers and uniquely tagged image.

In GitHub Actions, manually run **CI** with **container_smoke** enabled to run
the Docker smoke on Python 3.12. This does not add container builds to normal
pushes or pull requests. See the upstream
[bash-agent workflow](https://github.com/pkrusche/project-sandbox/blob/v0.4.1/scripts/e2e-jj-workflow.sh)
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
- Dependabot proposes monthly action and lockfile updates.
  [Repository configuration](https://github.com/pkrusche/project-sandbox-tui/blob/main/.github/dependabot.yml)

## OpenSpec skills

`skills/openspec-*` holds the OpenSpec agent skills. `[[workspace_skills]]` in
`.project-sandbox-tui.toml` copies them into each bookmark workspace's ignored
`.agents/skills/` and `.claude/skills/` directories. Relative sources are read
from the checkout that opened the TUI, so that checkout's revision must contain
them. To refresh after an OpenSpec upgrade, regenerate them in a scratch
directory and replace the copies:

```sh
tmp=$(mktemp -d)
git -C "$tmp" init -q
openspec init --tools agents --no-animation "$tmp"
rm -rf skills/openspec-*
cp -R "$tmp"/.agents/skills/openspec-* skills/
```

Keep the `[[workspace_skills]]` entries in sync if the skill set changes, then
review the config with `sbx-tui --trust-config`. Existing workspace copies are
kept: delete them while the workspace is idle to pick up new versions.

## Releases

Run `scripts/make-release.sh` from a checkout with uv, git, jj, and an authenticated
GitHub CLI (`gh`) on PATH. For Git, start on clean `main` matching `origin/main`.
For jj (including non-colocated workspaces), start with an empty working copy
whose only parent is immutable `main` and `main@origin`. One release can run
per checkout at a time. The script also checks the
live remote main commit.

Start the runtime required for smoke tests and choose the version bump when
prompted. If the version changes, push the verified main revision and rerun.
The script checks, builds, verifies the installed package, then prompts before
creating a release or uploading packages. See `scripts/make-release.sh --help`.

Release state lives under `.release-status/<version>-<commit>/`. Rerun at the same
commit to resume. Verified artifacts replace the contents of `dist/`.
