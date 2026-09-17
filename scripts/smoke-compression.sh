#!/usr/bin/env bash
# Task 7 smoke: Headroom compression (step 5) toggled via SP_CONTEXT_OPT.
#
# Structure: env override -> boot server -> wait for /health -> curl request ->
# python3 assertions on the JSON body. Boots the server TWICE — once with
# compression off, once with it on — using the SP_CONTEXT_OPT env override
# added to app/config/loader.py for exactly this purpose (see
# app/config/loader.py's `_co_opt_env` handling).
#
# The request shape mirrors the `large_context` traffic persona
# (tests/traffic/personas.py::_build_large_context): a single long user
# message containing a duplicated log block, NOT a multi-step/agent request —
# RouteForge runs no agent of its own any more (2026-09-17 removal); tools and
# agent loops belong to the calling application, so there is no more hop
# history for Headroom to compress. This is the single most token-heavy
# request shape RouteForge itself still produces.
#
# Asserts:
#   - OFF: compression_saved_usd == 0 (pass-through, today's behaviour).
#   - ON:  the compression trace record shows tokens_after < tokens_before
#          AND compression_saved_usd > 0 (Headroom actually ran).
#   - ON, onprem_only (SSN) request: ccr_retrievals_to_cloud == 0 — the
#     honesty counter from CLAUDE.md rule 6/the CCR skill — and the raw SSN
#     never appears in the response body (gate redaction still holds). This
#     phase is the privacy assertion the script exists for and must keep
#     passing independent of the phases above.
set -euo pipefail
cd "$(dirname "$0")/.."

export SP_PII_DETECTION=regex
export SP_SERVER_PORT=8080
LOG=/tmp/sp-smoke-compression.log
BODY=/tmp/sp-smoke-compression-body.json

# Content well over context_optimization.min_tokens (2000, config/serving.yaml
# default) so compression actually has something to do rather than bypassing
# on "below_min_tokens" — count_tokens is len(text)//4 (app/gateway/backends/
# base.py). Mirrors _build_large_context's shape: an instruction followed by
# 400 repeats of one duplicated log line — Headroom's compressor collapses
# repeated LINES, not merely long single lines, so duplication (not just
# length) is what gives it real material to compress.
VERBOSE_TASK=$(python3 -c "
line = ('ts=2026-07-22T00:00:00Z level=INFO tool=search_docs status=ok '
        'took_ms=42 result=\"lorem ipsum dolor sit amet consectetur adipiscing\"')
block = '\n'.join([line] * 400)
print('Summarize the deployment status from the log below.\n\n' + block, end='')
")

_start_server() {
  uv run --extra observability uvicorn app.main:app --port "$SP_SERVER_PORT" \
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

_single_turn_request() {
  local content="$1"
  python3 - "$content" <<'PYEOF' > /tmp/sp-smoke-compression-payload.json
import json, sys
print(json.dumps({"model": "auto",
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

echo "== large single-turn request, compression OFF =="
CODE=$(_single_turn_request "$VERBOSE_TASK")
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

echo "== large single-turn request, compression ON =="
CODE=$(_single_turn_request "$VERBOSE_TASK")
if [ "$CODE" != "200" ]; then
  echo "FAIL: expected HTTP 200, got $CODE"; cat "$BODY" || true; tail -n 80 "$LOG" || true; exit 1
fi

python3 - "$BODY" <<'PYEOF'
import json, sys
body = json.load(open(sys.argv[1]))
comp_records = [t for t in body["trace"] if t.get("step") == "compression"]
assert comp_records, f"no compression trace records found: {body['trace']}"
applied = [c for c in comp_records if c["applied"]]
assert applied, f"expected >=1 applied compression record (large duplicated log block), got {comp_records}"
for c in applied:
    assert c["tokens_after"] < c["tokens_before"], (
        f"compression ON: expected tokens_after < tokens_before, got {c}")
assert body["cost"]["compression_saved_usd"] > 0, (
    f"compression ON: expected compression_saved_usd > 0, got {body['cost']['compression_saved_usd']}")
print(f"compression ON: {len(applied)} record(s) compressed, "
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
