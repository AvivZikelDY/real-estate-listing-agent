#!/usr/bin/env bash
#
# Hardened dependency install for the listing agent.
#
# Supply-chain guards applied here:
#   --require-hashes          every artifact must match a hash pinned in the
#                             lock file, so a swapped/backdoored release on the
#                             index is rejected instead of installed
#   --only-binary=:all:       never build from sdist, so no setup.py / build
#                             backend from a dependency executes on this machine
#   --uploaded-prior-to=P7D   ignore anything uploaded in the last 7 days, the
#                             window in which compromised releases are usually
#                             caught and yanked (mirrors `exclude-newer` in
#                             pyproject.toml so pip and uv agree)
#   --no-deps                 install exactly the locked closure, nothing else
#
# Usage:
#   scripts/install_deps.sh          # runtime dependencies only
#   scripts/install_deps.sh --dev    # runtime + dev dependencies
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

REQUIREMENTS="requirements.txt"
if [[ "${1:-}" == "--dev" ]]; then
  REQUIREMENTS="requirements-dev.txt"
elif [[ $# -gt 0 ]]; then
  echo "usage: $0 [--dev]" >&2
  exit 2
fi

PYTHON="${PYTHON:-python3}"

# --uploaded-prior-to landed in pip 26.1; without it the cooldown guard would
# silently do nothing, so fail loudly rather than install unguarded.
"$PYTHON" -m pip install --upgrade 'pip>=26.1'

guarded_install() {
  "$PYTHON" -m pip install \
    --require-hashes \
    --only-binary=:all: \
    --uploaded-prior-to=P7D \
    --no-deps \
    -r "$1"
}

# Build backend first, so the editable install below can skip build isolation
# (which would otherwise fetch an unverified setuptools straight from PyPI).
guarded_install requirements-build.txt
guarded_install "$REQUIREMENTS"

# The project itself is a local path, so it has no index hash to verify. Install
# it without dependencies: the locked closure above is already complete.
"$PYTHON" -m pip install --no-deps --no-build-isolation -e .

echo "Installed from $REQUIREMENTS with hash + cooldown + wheel-only guards."
