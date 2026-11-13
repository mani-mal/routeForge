#!/usr/bin/env bash
# B110 — check RouteForge's Switchyard adapter against NVIDIA's NEWEST upstream
# release, without touching the pinned dependency.
#
# What it does:
#   1. Builds a throwaway venv from the locked dependencies (all extras the
#      switchyard tests touch), exactly like the normal test env.
#   2. Replaces the pinned nemo-switchyard with the newest published version
#      (pre-releases included), or with $SWITCHYARD_VERSION if set.
#   3. Runs ONLY the adapter contract suite: tests/routing/switchyard/.
#
# The pinned build is never changed by this script. A failure here means the
# next upstream release needs adapter work before the pin is bumped — see the
# upgrade playbook in docs/reference/switchyard-integration.md.
#
# Usage (run where the full suite runs, e.g. the DGX test pod, from the repo root):
#   scripts/switchyard_upstream_check.sh                 # newest upstream release
#   SWITCHYARD_VERSION=0.4.0 scripts/switchyard_upstream_check.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${SWITCHYARD_CHECK_VENV:-${TMPDIR:-/tmp}/routeforge-switchyard-upstream}"
TARGET="${SWITCHYARD_VERSION:-}"

cd "$REPO_ROOT"

echo "== building isolated venv at $VENV"
UV_PROJECT_ENVIRONMENT="$VENV" UV_LINK_MODE=copy \
  uv sync --quiet --frozen --extra switchyard --extra observability

PINNED="$("$VENV/bin/python" -c 'from importlib.metadata import version; print(version("nemo-switchyard"))')"

if [[ -n "$TARGET" ]]; then
  SPEC="nemo-switchyard==$TARGET"
else
  SPEC="nemo-switchyard"
fi
echo "== installing newest upstream ($SPEC, pre-releases allowed) over pinned $PINNED"
uv pip install --quiet --python "$VENV/bin/python" --prerelease allow --upgrade "$SPEC"

UPSTREAM="$("$VENV/bin/python" -c 'from importlib.metadata import version; print(version("nemo-switchyard"))')"
echo "== pinned: $PINNED   under test: $UPSTREAM"
if [[ "$UPSTREAM" == "$PINNED" ]]; then
  echo "== no newer upstream release than the pin; running the suite anyway"
fi

set +e
"$VENV/bin/python" -m pytest -p no:cacheprovider -p no:warnings tests/routing/switchyard/
STATUS=$?
set -e

if [[ $STATUS -eq 0 ]]; then
  echo "== PASS: adapter contract suite passes on nemo-switchyard $UPSTREAM"
else
  echo "== FAIL: adapter contract suite fails on nemo-switchyard $UPSTREAM (pinned $PINNED)."
  echo "   Read the upstream CHANGELOG breaking entries for $UPSTREAM, then follow the"
  echo "   upgrade playbook in docs/reference/switchyard-integration.md."
  echo "   Note: test_version_contract / the lint fail by design when $UPSTREAM is outside SUPPORTED."
fi
exit $STATUS
