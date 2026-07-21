#!/usr/bin/env bash
set -euo pipefail
exec uv run uvicorn app.main:app --reload --port 8080
