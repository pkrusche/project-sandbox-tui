#!/usr/bin/env bash
# Interactive release workflow for project-sandbox-tui; see docs/development.md.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
die() { echo "Error: $*" >&2; exit 1; }
if [[ $# -gt 0 ]]; then
    case "$*" in
        --help|-h)
            cat <<'HELP'
Usage: scripts/make-release.sh
Check clean, pushed main; optionally bump and commit the version; run checks and
the real container smoke test (scripts/e2e-test.sh; SBX_E2E_RUNTIME selects the
runtime); build and verify artifacts; create a GitHub release; publish to TestPyPI
and PyPI with separate confirmations and hidden token prompts.
Resume by running again. Progress and artifacts live in .release-status/ and are
scoped to the release version and source commit; completed state is removed.
Verified artifacts replace old builds in dist/. Requires uv, git, jj, gh,
project-sandbox, and a running container runtime.
HELP
            exit 0 ;;
        *) die "Unknown argument: $*" ;;
    esac
fi
confirm() {
    local reply
    read -r -p "$* [y/N] " reply || return 1
    [[ "$reply" == y || "$reply" == Y || "$reply" == yes ]]
}
for tool in uv git jj gh; do
    command -v "$tool" >/dev/null || die "Required tool missing: $tool"
done
JJ=false
REPO_GIT_DIR=""
if jj root >/dev/null 2>&1; then
    JJ=true
    REPO_GIT_DIR=$(jj git root)
fi
# Scope GIT_DIR to repository operations: tests create their own repositories.
repo_command() {
    if [[ -n "$REPO_GIT_DIR" ]]; then
        GIT_DIR="$REPO_GIT_DIR" "$@"
    else
        "$@"
    fi
}
check_clean() {
    if $JJ; then
        [[ -n "$(jj log -r '@ & empty()' --no-graph -T commit_id)" ]] || die "jj working copy must be empty."
        COMMIT=$(jj log -r '@- & main & main@origin & immutable()' --no-graph -T commit_id)
        [[ -n "$COMMIT" ]] || die "jj parent must be immutable main, pushed to origin."
    else
        [[ -z "$(git status --porcelain)" ]] || die "Git working tree must be clean."
        [[ "$(git branch --show-current)" == main ]] || die "Release from main."
        COMMIT=$(git rev-parse HEAD)
        [[ "$COMMIT" == "$(git rev-parse refs/remotes/origin/main)" ]] || die "Push main to origin first."
    fi
    local remote
    remote=$(repo_command git ls-remote --exit-code origin refs/heads/main)
    [[ "${remote%%$'\t'*}" == "$COMMIT" ]] || die "Remote main differs; fetch and reconcile before releasing."
}
check_clean
VERSION=$(uv version --short)
read -r -p "Version $VERSION; bump (major/minor/patch/stable/alpha/beta/rc/post/dev; blank to keep): " bump
if [[ -n "$bump" ]]; then
    case "$bump" in
        major|minor|patch|stable|alpha|beta|rc|post|dev) ;;
        *) die "Unknown version bump: $bump" ;;
    esac
    uv version --bump "$bump" --no-sync
    VERSION=$(uv version --short)
    if $JJ; then
        jj describe -m "Bump version to $VERSION"
        jj new
        echo "Move main to the version commit and push it, then run this script again."
    else
        git add -- pyproject.toml uv.lock
        git commit -m "Bump version to $VERSION"
        echo "Push main, then run this script again."
    fi
    exit 0
fi
[[ "$VERSION" =~ ^[0-9][A-Za-z0-9.+-]*$ ]] || die "Invalid release version: $VERSION"
STATE="$ROOT/.release-status/$VERSION-$COMMIT"
mkdir -p "$STATE"
done_step() { [[ -f "$STATE/$1" ]]; }
mark() { touch "$STATE/$1"; }
if ! done_step checks; then
    uv sync --locked
    uv run ruff check .
    uv run ruff format --check .
    uv run ty check
    scripts/test.sh
    mark checks
fi
if ! done_step smoke; then
    echo "Running the real container smoke test; see docs/development.md."
    scripts/e2e-test.sh || die "Container smoke test failed; fix it before releasing."
    mark smoke
fi
ARTIFACTS="$STATE/dist"
WHEEL="$ARTIFACTS/project_sandbox_tui-$VERSION-py3-none-any.whl"
SDIST="$ARTIFACTS/project_sandbox_tui-$VERSION.tar.gz"
if ! done_step build; then
    # Discard partial output from a failed build before trying again.
    rm -rf -- "$ARTIFACTS"
    uv build --out-dir "$ARTIFACTS"
    [[ -f "$WHEEL" && -f "$SDIST" ]] || die "Expected release artifacts are missing."
    uv venv "$STATE/venv" --clear
    uv pip install --python "$STATE/venv/bin/python" "$WHEEL"
    (
        cd "$STATE"
        CLI_VERSION=$("$STATE/venv/bin/sbx-tui" --version)
        echo "$CLI_VERSION"
        [[ "$CLI_VERSION" == "sbx-tui $VERSION" ]] || die "Installed CLI version differs from $VERSION."
        "$STATE/venv/bin/sbx-tui" --help
        "$STATE/venv/bin/python" -I -c 'import project_sandbox_tui.app'
    )
    "$STATE/venv/bin/python" - "$WHEEL" "$SDIST" > "$STATE/hashes" <<'PY'
import hashlib
import sys
from pathlib import Path
for filename in sys.argv[1:]:
    print(hashlib.sha256(Path(filename).read_bytes()).hexdigest())
PY
    mark build
fi
# Refuse to publish modified or missing artifacts when resuming.
"$STATE/venv/bin/python" - "$STATE/hashes" "$WHEEL" "$SDIST" <<'PY'
import hashlib
import sys
from pathlib import Path
expected = Path(sys.argv[1]).read_text().splitlines()
actual = [hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in sys.argv[2:]]
if actual != expected:
    raise SystemExit("Release artifacts changed since verification; refusing to publish.")
PY
# Keep the conventional build directory current, including on resumed releases.
rm -rf -- "$ROOT/dist"
mkdir -p "$ROOT/dist"
cp -- "$WHEEL" "$SDIST" "$ROOT/dist/"
# Checks and build tools must not have changed the source being released.
EXPECTED_COMMIT="$COMMIT"
check_clean
[[ "$COMMIT" == "$EXPECTED_COMMIT" ]] || die "Source commit changed during release."
TAG="v$VERSION"
if ! done_step github; then
    if output=$(repo_command git ls-remote --exit-code --tags origin "refs/tags/$TAG" 2>&1); then
        die "Tag $TAG already exists."
    else
        status=$?
        [[ $status == 2 ]] || die "Cannot check remote tag: $output"
    fi
    if output=$(repo_command gh api "repos/{owner}/{repo}/releases/tags/$TAG" 2>&1); then
        die "GitHub release $TAG already exists."
    else
        [[ "$output" == *"HTTP 404"* ]] || die "Cannot check GitHub release: $output"
    fi
    confirm "Create GitHub release $TAG at $COMMIT?" || exit 0
    release_args=(--target "$COMMIT" --title "$TAG" --generate-notes)
    if [[ "$VERSION" =~ (a|b|rc|dev)[0-9] ]]; then
        release_args+=(--prerelease)
    fi
    repo_command gh release create "$TAG" "${release_args[@]}"
    mark github
fi
publish() {
    local registry="$1" marker="$2" token
    shift 2
    done_step "$marker" && return 0
    echo "Artifacts: $WHEEL $SDIST"
    confirm "Publish $TAG to $registry?" || exit 0
    read -r -s -p "$registry API token: " token
    echo
    [[ -n "$token" ]] || die "Token cannot be empty."
    UV_PUBLISH_TOKEN="$token" uv publish "$@" "$WHEEL" "$SDIST"
    unset token
    mark "$marker"
}
publish TestPyPI testpypi --publish-url https://test.pypi.org/legacy/
publish PyPI pypi
rm -rf -- "$STATE"
# Other unfinished releases may still need their progress and verified artifacts.
if [[ -z "$(ls -A "$ROOT/.release-status")" ]]; then
    rmdir -- "$ROOT/.release-status"
fi
echo "Release $TAG complete. Artifacts are in $ROOT/dist; completed release state removed."
