#!/usr/bin/env bash
# Phase 5 acceptance: a mock_multistep request to the public
# /v1/chat/completions runs a real NeMo Agent Toolkit tool_calling_agent
# whose LLM calls re-enter our router via the internal endpoint, producing
# >=2 agent_hop records in the response trace. Self-contained: boots its
# own uvicorn, no external services required.
set -euo pipefail
cd "$(dirname "$0")/.."

export SP_PII_DETECTION=regex
# The app self-calls its own port for the agent runtime (see
# app/main.py's _self_port() / app/pipeline.py) — the bound port MUST
# match SP_SERVER_PORT.
export SP_SERVER_PORT=8080

uv run --extra agent --extra observability uvicorn app.main:app --port "$SP_SERVER_PORT" \
  >/tmp/sp-smoke-agent.log 2>&1 &
API=$!
trap 'kill $API 2>/dev/null || true' EXIT

echo "waiting for API to become healthy (port $SP_SERVER_PORT)..."
UP=0
for i in $(seq 1 60); do
  if curl -sf "http://localhost:$SP_SERVER_PORT/health" >/dev/null 2>&1; then
    UP=1
    break
  fi
  sleep 0.5
done
if [ "$UP" -ne 1 ]; then
  echo "FAIL: server never became healthy — see /tmp/sp-smoke-agent.log"
  tail -n 80 /tmp/sp-smoke-agent.log || true
  exit 1
fi

echo "== multi-step agent request =="
HTTP_CODE=$(curl -s -o /tmp/sp-smoke-agent-body.json -w '%{http_code}' \
  "http://localhost:$SP_SERVER_PORT/v1/chat/completions" \
  -H 'authorization: Bearer acme-key' \
  -H 'content-type: application/json' \
  -d '{"model":"auto","mock_multistep":true,"messages":[{"role":"user","content":"Look up account 42 then compute 12 * 7"}]}')

if [ "$HTTP_CODE" != "200" ]; then
  echo "FAIL: expected HTTP 200, got $HTTP_CODE"
  cat /tmp/sp-smoke-agent-body.json || true
  echo "--- server log ---"
  tail -n 80 /tmp/sp-smoke-agent.log || true
  exit 1
fi

python3 - /tmp/sp-smoke-agent-body.json <<'PYEOF'
import json
import sys

with open(sys.argv[1]) as f:
    body = json.load(f)

hops = [t for t in body["trace"] if t.get("step") == "agent_hop"]
assert len(hops) >= 2, f"expected >=2 agent_hop records, got {len(hops)}: {hops}"
for h in hops:
    assert h.get("chosen") is not None, f"agent_hop missing chosen: {h}"

content = body["choices"][0]["message"]["content"]
assert content, "final answer content is empty"

expected_total = sum(h.get("actual_cost_usd") or h.get("est_cost_usd", 0) for h in hops)
total = body["cost"]["total_usd"]
assert abs(total - expected_total) < 1e-9, (
    f"cost.total_usd ({total}) != sum of hop costs ({expected_total})"
)

assert body["cost"]["baseline_usd"] > 0, "cost.baseline_usd must be > 0"
assert body["cost"]["routing_saved_usd"] >= 0, "cost.routing_saved_usd must be >= 0"

print(f"SMOKE-AGENT: {len(hops)} routed hops, final answer present — PASS")
PYEOF
