#!/usr/bin/env bash
# Run the Serving Plane API locally.
#
# Ensures the uv-managed venv exists and deps are installed, then boots uvicorn
# with --reload. `uv sync` creates .venv if it is missing and installs from
# uv.lock — do NOT hand-roll `python -m venv` + `pip`.
#
# PII detector (SP_PII_DETECTION):
#   regex     (default) — fast, no extra downloads, same detector smoke.sh uses
#   presidio            — real detector; pulls the `presidio` extra + spaCy model
#   off                 — no detection
#
#   bash scripts/dev-backend.sh                            # regex
#   SP_PII_DETECTION=presidio bash scripts/dev-backend.sh  # real Presidio
set -euo pipefail
cd "$(dirname "$0")/.."

DETECTOR="${SP_PII_DETECTION:-regex}"

if [ "$DETECTOR" = "presidio" ]; then
  # Extra pip deps + the spaCy model AnalyzerEngine() loads by default.
  uv sync --extra presidio
  uv run python -m spacy download en_core_web_lg
else
  uv sync
fi

export SP_PII_DETECTION="$DETECTOR"
echo "dev-backend: pii_detection=$DETECTOR, http://127.0.0.1:8080"
exec uv run uvicorn app.main:app --reload --port 8080
