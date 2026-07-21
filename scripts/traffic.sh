#!/usr/bin/env bash
set -euo pipefail
exec uv run python -m tests.traffic "$@"
