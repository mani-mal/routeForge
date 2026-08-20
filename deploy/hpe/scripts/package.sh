#!/usr/bin/env bash
# Lint + package the chart into a .tgz you upload at the wizard's
# "Framework Chart" step (Select or Upload associated Helm Chart).
#
# Usage:  ./package.sh [output-dir]
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
chart="$(cd "$here/../routeforge" && pwd)"
outdir="${1:-$here/..}"

echo "==> helm lint"
helm lint "$chart"

echo "==> helm template (smoke render with defaults)"
helm template routeforge "$chart" >/dev/null

echo "==> helm package -> $outdir"
helm package "$chart" --destination "$outdir"

echo
echo "Upload the resulting routeforge-<version>.tgz at the Import Framework"
echo "wizard's 'Framework Chart' step, then edit values at 'Framework Values'."
