#!/usr/bin/env bash
# Bring up local Langfuse (Phase 4). First boot takes ~1-2 min (migrations).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || cp .env.example .env
set -a; . ./.env; set +a
docker compose -f deploy/compose/langfuse.yml up -d
echo "Langfuse coming up at $LANGFUSE_HOST (login demo@example.com / demodemo)"
echo "waiting for health..."
for i in $(seq 1 90); do
  curl -sf "$LANGFUSE_HOST/api/public/health" >/dev/null 2>&1 && { echo "ready"; exit 0; }
  sleep 2
done
echo "timed out waiting for Langfuse" >&2; exit 1
