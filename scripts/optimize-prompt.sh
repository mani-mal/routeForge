#!/usr/bin/env bash
set -euo pipefail
exec uv run python -m tools.prompt_opt.optimize "$@"
