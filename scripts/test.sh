#!/usr/bin/env bash
# Run the tests in parallel with pytest-xdist; arguments go to pytest:
#   scripts/test.sh                          # everything, one worker per CPU
#   scripts/test.sh tests/test_vcs.py -k merge
#   scripts/test.sh -n 4                     # fewer workers (the last -n wins)
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run pytest -n auto "$@"
