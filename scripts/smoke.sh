#!/usr/bin/env bash
set -euo pipefail
export SP_PII_DETECTION="${SP_PII_DETECTION:-regex}"
uv run uvicorn app.main:app --port 8080 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT
for i in $(seq 1 30); do
  curl -sf http://localhost:8080/health >/dev/null && break || sleep 0.5
done
curl -sf http://localhost:8080/health >/dev/null || { echo "FAIL: server never became healthy"; exit 1; }

echo "== health =="
curl -sf http://localhost:8080/health

echo; echo "== clean request (expect cheap tier) =="
curl -sf http://localhost:8080/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"capital of France?"}]}' \
  | uv run python -c "import sys,json; d=json.load(sys.stdin); h=[t for t in d['trace'] if t['step']=='router'][0]; assert h['chosen']=='local-8b', f\"expected cheap tier local-8b, got {h['chosen']}\"; print('chosen:', h['chosen']); print('total/baseline:', d['cost']['total_usd'], d['cost']['baseline_usd'])"

echo; echo "== SSN request (expect onprem_only, cloud excluded, no raw SSN) =="
OUT=$(curl -sf http://localhost:8080/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"verify SSN 123-45-6789"}]}')
echo "$OUT" | uv run python -c "import sys,json; d=json.load(sys.stdin); assert d['policy']['onprem_only'] is True, 'onprem_only not set'; print('onprem_only:', d['policy']['onprem_only']); assert '123-45-6789' not in json.dumps(d), 'RAW SSN LEAKED'; print('no raw SSN in response: OK')"

echo; echo "== stream:true (expect 400) =="
code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"hi"}],"stream":true}')
test "$code" = "400" && echo "stream rejected: OK ($code)" || { echo "FAIL: $code"; exit 1; }

echo; echo "SMOKE: all checks passed"
