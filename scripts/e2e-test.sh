#!/usr/bin/env bash
# Run the real container smoke test (tests/test_container_smoke.py).
#
#   scripts/e2e-test.sh                        # detect a running runtime
#   scripts/e2e-test.sh --runtime podman       # or SBX_E2E_RUNTIME=podman
#   scripts/e2e-test.sh --install              # install the CI-pinned project-sandbox if needed
#   scripts/e2e-test.sh --command 'uv run --project ../project-sandbox project-sandbox'
#   scripts/e2e-test.sh -- --container-base-image python:3.14-slim   # extra pytest arguments
#
# Checks jj, project-sandbox and the runtime before starting pytest. Detection
# picks the first runtime that answers: Apple container (macOS only), then
# docker, then podman. For Apple container, unless --basetemp is given, test
# state goes to a fresh .project-sandbox/container-smoke.* directory, because
# its build VM cannot read macOS's per-user temp directory.
set -euo pipefail
cd "$(dirname "$0")/.."

usage() { sed -n '2,14s/^# \{0,1\}//p' "$0"; }
die() { printf 'e2e-test: %s\n' "$*" >&2; exit 1; }
info() { printf 'e2e-test: %s\n' "$*" >&2; }

runtime=${SBX_E2E_RUNTIME:-auto}
command=${SBX_E2E_COMMAND:-}
install=0
pytest_args=()
while (($#)); do
    case $1 in
        --runtime) (($# > 1)) || die "--runtime needs a value"; runtime=$2; shift 2 ;;
        --runtime=*) runtime=${1#*=}; shift ;;
        --command) (($# > 1)) || die "--command needs a value"; command=$2; shift 2 ;;
        --command=*) command=${1#*=}; shift ;;
        --install) install=1; shift ;;
        -h | --help) usage; exit 0 ;;
        --) shift; pytest_args+=("$@"); break ;;
        *) pytest_args+=("$1"); shift ;;
    esac
done
case $runtime in
    auto | docker | podman | apple-container) ;;
    *) die "unknown runtime '$runtime' (auto, docker, podman or apple-container)" ;;
esac

# Minimums and the pin come from their single sources, so update-pins.py keeps them current.
python_tuple() {  # NAME FILE -> dotted version from `NAME = (a, b, c)`
    sed -n "s/^$1 = (\([0-9, ]*\)).*/\1/p" "$2" | tr -d ' ' | tr ',' '.'
}
min_jj=$(python_tuple TESTED_JJ src/project_sandbox_tui/doctor.py)
min_ps=$(python_tuple MIN_PROJECT_SANDBOX src/project_sandbox_tui/doctor.py)
pinned_ps=$(sed -n 's/.*project-sandbox==\([0-9.]*\).*/\1/p' .github/workflows/ci.yml | head -n 1)
[[ $min_jj && $min_ps && $pinned_ps ]] || die "could not read version requirements from doctor.py/ci.yml"

version_of() { grep -Eo '[0-9]+(\.[0-9]+)+' <<<"$1" | head -n 1; }
at_least() { [[ $(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n 1) == "$2" ]]; }

command -v uv >/dev/null || die "uv not found on PATH"

# jj: the test treats doctor's "older than tested" warning as a failure.
command -v jj >/dev/null || die "jj not found on PATH (need >= $min_jj): https://jj-vcs.github.io/jj/"
jj_version=$(version_of "$(jj --version)")
at_least "$jj_version" "$min_jj" || die "jj $jj_version is older than $min_jj"
info "jj $jj_version"

# project-sandbox: the default command may be installed on request; a custom one is only checked.
ps_version() {
    local -a cmd
    read -ra cmd <<<"${command:-project-sandbox}"
    command -v "${cmd[0]}" >/dev/null || return 1
    version_of "$("${cmd[@]}" --version 2>&1)"
}
ps_found=$(ps_version || true)
if [[ -z $command ]] && { [[ -z $ps_found ]] || ! at_least "$ps_found" "$min_ps"; }; then
    hint="uv tool install --force project-sandbox==$pinned_ps"
    if ((install)); then
        info "installing project-sandbox $pinned_ps (CI pin)"
        uv tool install --force "project-sandbox==$pinned_ps"
        ps_found=$(ps_version || true)
    elif [[ -z $ps_found ]]; then
        die "project-sandbox not found on PATH; run '$hint' or pass --install"
    fi
fi
[[ -n $ps_found ]] || die "'${command:-project-sandbox} --version' failed"
at_least "$ps_found" "$min_ps" \
    || die "project-sandbox $ps_found is older than $min_ps; run 'uv tool install --force project-sandbox==$pinned_ps' or pass --install"
[[ $ps_found == "$pinned_ps" ]] || info "note: project-sandbox $ps_found differs from the CI pin $pinned_ps"
info "project-sandbox $ps_found (${command:-project-sandbox})"

# Container runtime: an explicit choice must answer; auto takes the first that does.
runtime_cli() { if [[ $1 == apple-container ]]; then echo container; else echo "$1"; fi; }
probe() {
    local cli
    cli=$(runtime_cli "$1")
    command -v "$cli" >/dev/null || { echo "not on PATH"; return 1; }
    local -a check=("$cli" info)
    [[ $cli == container ]] && check=(container system status)
    local out
    out=$("${check[@]}" 2>&1) && return 0
    echo "'${check[*]}' failed: $(tail -n 1 <<<"$out")"
    return 1
}
if [[ $runtime == auto ]]; then
    candidates=(docker podman)
    [[ $(uname -s) == Darwin ]] && candidates=(apple-container "${candidates[@]}")
    problems=()
    for candidate in "${candidates[@]}"; do
        if why=$(probe "$candidate"); then runtime=$candidate; break; fi
        problems+=("  $candidate: $why")
    done
    [[ $runtime != auto ]] || die "no running container runtime found:"$'\n'"$(printf '%s\n' "${problems[@]}")"
else
    why=$(probe "$runtime") || die "$runtime is not usable: $why"
fi
info "runtime $runtime"

args=(-n 0 -v -s tests/test_container_smoke.py --container-smoke --container-runtime "$runtime")
[[ -n $command ]] && args+=(--container-command "$command")
if [[ $runtime == apple-container ]] && [[ " ${pytest_args[*]-} " != *" --basetemp"* ]]; then
    mkdir -p .project-sandbox
    basetemp=$(mktemp -d "$PWD/.project-sandbox/container-smoke.XXXXXX")
    info "test state in $basetemp"
    args+=(--basetemp "$basetemp")
fi
exec scripts/test.sh "${args[@]}" "${pytest_args[@]}"
