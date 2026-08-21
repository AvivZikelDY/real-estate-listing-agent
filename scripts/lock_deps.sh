#!/usr/bin/env bash
#
# Regenerate the lock file and the hash-pinned requirements exports.
#
# Run this whenever pyproject.toml dependencies change, then commit
# uv.lock, requirements.txt, requirements-dev.txt and requirements-build.txt
# together. CI fails if these drift apart (see .github/workflows/ci.yml).
#
# The `exclude-newer` cooldown in pyproject.toml is applied automatically by
# uv during resolution, so the exports can never contain a release that is
# newer than the cooldown window.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export_reqs() {
  local out="$1"
  shift
  uv export --no-emit-project --format requirements.txt -o "$out" "$@"
}

uv lock

export_reqs requirements.txt --no-dev --no-default-groups
export_reqs requirements-dev.txt --extra dev --no-default-groups
export_reqs requirements-build.txt --only-group build

echo "Regenerated uv.lock, requirements.txt, requirements-dev.txt, requirements-build.txt."
