#!/usr/bin/env bash
# Task 7 smoke: Headroom compression (step 5) toggled via SP_CONTEXT_OPT.
#
# Mirrors scripts/smoke-agent.sh's structure (env override -> boot server ->
# wait for /health -> curl request -> python3 assertions on the JSON body),
# but boots the server TWICE — once with compression off, once with it on —
# using the SP_CONTEXT_OPT env override added to app/config/loader.py for
# exactly this purpose (see app/config/loader.py's `_co_opt_env` handling).
#
# Asserts:
#   - OFF: compression_saved_usd == 0 (pass-through, today's behaviour).
#   - ON:  the compression trace record shows tokens_after < tokens_before
#          AND compression_saved_usd > 0 (Headroom actually ran).
#   - ON, onprem_only (SSN) request: ccr_retrievals_to_cloud == 0 — the
#     honesty counter from CLAUDE.md rule 6/the CCR skill — and the raw SSN
#     never appears in the response body (gate redaction still holds).
set -euo pipefail
cd "$(dirname "$0")/.."

export SP_PII_DETECTION=regex
export SP_SERVER_PORT=8080
LOG=/tmp/sp-smoke-compression.log
BODY=/tmp/sp-smoke-compression-body.json

# Content well over context_optimization.min_tokens (2000, config/serving.yaml
# default) so compression actually has something to do rather than bypassing
# on "below_min_tokens" — count_tokens is len(text)//4 (app/gateway/backends/
# base.py), so ~700 repeats of a 14-char line is comfortably >2000 tokens.
VERBOSE_TASK="Look up account 42 then compute 12 * 7. $(python3 -c "print('INFO log line\n' * 700, end='')")"

_start_server() {
  uv run --extra agent --extra observability uvicorn app.main:app --port "$SP_SERVER_PORT" \
    >"$LOG" 2>&1 &
  API=$!
  echo "waiting for API to become healthy (port $SP_SERVER_PORT, SP_CONTEXT_OPT=${SP_CONTEXT_OPT:-unset})..."
  UP=0
  for i in $(seq 1 60); do
    if curl -sf "http://localhost:$SP_SERVER_PORT/health" >/dev/null 2>&1; then
      UP=1
      break
    fi
    sleep 0.5
  done
  if [ "$UP" -ne 1 ]; then
    echo "FAIL: server never became healthy — see $LOG"
    tail -n 80 "$LOG" || true
    exit 1
  fi
}

_stop_server() {
  kill "$API" 2>/dev/null || true
  wait "$API" 2>/dev/null || true
}

_multistep_request() {
  local content="$1"
  python3 - "$content" <<'PYEOF' > /tmp/sp-smoke-compression-payload.json
import json, sys
print(json.dumps({"model": "auto", "mock_multistep": True,
                   "messages": [{"role": "user", "content": sys.argv[1]}]}))
PYEOF
  curl -s -o "$BODY" -w '%{http_code}' \
    "http://localhost:$SP_SERVER_PORT/v1/chat/completions" \
    -H 'authorization: Bearer acme-key' \
    -H 'content-type: application/json' \
    --data @/tmp/sp-smoke-compression-payload.json
}

# ---------------------------------------------------------------------------
# Phase 1: compression OFF (SP_CONTEXT_OPT=off) — pass-through, matches
# today's default (context_optimization.enabled: false in config/serving.yaml).
# ---------------------------------------------------------------------------
export SP_CONTEXT_OPT=off
_start_server
trap '_stop_server' EXIT

echo "== multi-step request, compression OFF =="
CODE=$(_multistep_request "$VERBOSE_TASK")
if [ "$CODE" != "200" ]; then
  echo "FAIL: expected HTTP 200, got $CODE"; cat "$BODY" || true; tail -n 80 "$LOG" || true; exit 1
fi

python3 - "$BODY" <<'PYEOF'
import json, sys
body = json.load(open(sys.argv[1]))
assert body["cost"]["compression_saved_usd"] == 0, (
    f"compression OFF: expected compression_saved_usd == 0, got {body['cost']['compression_saved_usd']}")
print("compression OFF: compression_saved_usd == 0 -- PASS")
PYEOF

_stop_server
trap - EXIT

# ---------------------------------------------------------------------------
# Phase 2: compression ON (SP_CONTEXT_OPT=on).
# ---------------------------------------------------------------------------
export SP_CONTEXT_OPT=on
_start_server
trap '_stop_server' EXIT

echo "== multi-step request, compression ON =="
CODE=$(_multistep_request "$VERBOSE_TASK")
if [ "$CODE" != "200" ]; then
  echo "FAIL: expected HTTP 200, got $CODE"; cat "$BODY" || true; tail -n 80 "$LOG" || true; exit 1
fi

python3 - "$BODY" <<'PYEOF'
import json, sys
body = json.load(open(sys.argv[1]))
comp_records = [t for t in body["trace"] if t.get("step") == "compression"]
assert comp_records, f"no compression trace records found: {body['trace']}"
applied = [c for c in comp_records if c["applied"]]
assert applied, f"expected >=1 applied compression record (hop history was verbose), got {comp_records}"
for c in applied:
    assert c["tokens_after"] < c["tokens_before"], (
        f"compression ON: expected tokens_after < tokens_before, got {c}")
assert body["cost"]["compression_saved_usd"] > 0, (
    f"compression ON: expected compression_saved_usd > 0, got {body['cost']['compression_saved_usd']}")
print(f"compression ON: {len(applied)} hop(s) compressed, "
      f"compression_saved_usd={body['cost']['compression_saved_usd']} -- PASS")
PYEOF

echo "== onprem_only (SSN) request, compression ON: CCR must never retrieve to cloud =="
SSN="123-45-6789"
CODE=$(curl -s -o "$BODY" -w '%{http_code}' \
  "http://localhost:$SP_SERVER_PORT/v1/chat/completions" \
  -H 'authorization: Bearer hospital-key' \
  -H 'content-type: application/json' \
  -d "{\"messages\":[{\"role\":\"user\",\"content\":\"verify SSN $SSN\"}]}")
if [ "$CODE" != "200" ]; then
  echo "FAIL: expected HTTP 200, got $CODE"; cat "$BODY" || true; exit 1
fi

# Raw SSN must never leak into the response body (gate redaction, unrelated
# to compression, still holds) — grep -v asserts the token is ABSENT.
grep -v "$SSN" "$BODY" >/dev/null || { echo "FAIL: raw SSN leaked into response body"; exit 1; }

python3 - "$BODY" <<'PYEOF'
import json, sys
body = json.load(open(sys.argv[1]))
assert body["policy"]["onprem_only"] is True, "expected onprem_only for SSN request"
comp_records = [t for t in body["trace"] if t.get("step") == "compression"]
assert comp_records, f"no compression trace record found: {body['trace']}"
for c in comp_records:
    assert c["ccr_retrievals_to_cloud"] == 0, (
        f"honesty counter violated: ccr_retrievals_to_cloud != 0 for onprem_only: {c}")
print("onprem_only SSN request: ccr_retrievals_to_cloud == 0, no raw SSN in body -- PASS")
PYEOF

echo
echo "SMOKE-COMPRESSION: all checks passed"
