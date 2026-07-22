#!/usr/bin/env bash
# End-to-end observability proof (Phase 4): a PII request lands in Langfuse with
# the SSN absent. Requires: scripts/obs-up.sh already ran.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
export SP_PII_DETECTION=regex

uv sync --extra observability >/dev/null
uv run uvicorn app.main:app --port 8080 >/tmp/sp-obs-api.log 2>&1 &
API=$!
trap 'kill $API 2>/dev/null || true' EXIT
for i in $(seq 1 30); do curl -sf localhost:8080/health >/dev/null 2>&1 && break; sleep 0.5; done

SSN="123-45-6789"
RID=$(curl -s localhost:8080/v1/chat/completions \
  -H 'authorization: Bearer hospital-key' -H 'content-type: application/json' \
  -d "{\"model\":\"auto\",\"messages\":[{\"role\":\"user\",\"content\":\"my SSN is $SSN\"}]}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "request $RID sent; flushing to Langfuse..."
sleep 8  # allow BatchSpanProcessor + Langfuse ingestion

AUTH=$(printf '%s:%s' "$LANGFUSE_PUBLIC_KEY" "$LANGFUSE_SECRET_KEY" | base64)
TRACES=$(curl -s "$LANGFUSE_HOST/api/public/traces?limit=50" -H "authorization: Basic $AUTH")
echo "$TRACES" | grep -q "$SSN" && { echo "FAIL: raw SSN found in Langfuse traces"; exit 1; }
echo "$TRACES" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d.get("data"), "no traces ingested"' \
  || { echo "FAIL: no traces ingested"; exit 1; }
echo "SMOKE-OBS: trace ingested, SSN absent — PASS"
