#!/usr/bin/env bash
#
# demo-traffic.sh — generate data for the routeForge PCAI demo dashboard.
#
# Runs from the PCAI notebook terminal (or any host that can reach the app URL).
# Two ways to drive the demo, both against the PUBLIC app URL:
#
#   burst          Fill the WHOLE dashboard with the 500-request persona mix
#                  (~70% cheap tier, PII -> onprem, cost vs all-frontier baseline).
#                  Admin-gated: logs in first and carries the rf_admin_session
#                  cookie. This is the same thing the UI's "Run burst" button does.
#
#   single-simple  ONE OpenAI-compatible /v1/chat/completions call as Acme
#                  (cloud-eligible tenant) with a trivial question -> routes to the
#                  cheap tier. Uses a per-app Bearer token; NO admin cookie.
#
#   single-pii     ONE call as General Health (hospital, default_residency
#                  onprem_only) carrying a synthetic SSN + email -> the policy gate
#                  detects PII, floors residency to on-prem, redacts telemetry.
#                  This is the privacy story in a single request + decision trace.
#
# The two "single" variants return the full DecisionRecord trace, so they're the
# ones to run live while narrating "watch where this request goes and why".
#
# Usage:
#   ./demo-traffic.sh burst [N] [RPS]     # defaults: 500 requests @ 20 rps
#   ./demo-traffic.sh single-simple
#   ./demo-traffic.sh single-pii
#
# Override the target / creds via env if the domain or password changes:
#   BASE_URL=... ADMIN_EMAIL=... ADMIN_PASSWORD=... ./demo-traffic.sh burst
#
# In-cluster fallback (if the public URL is ever unreachable but the notebook's
# kubectl works): wrap any curl below in a throwaway pod and hit the API Service
# FQDN directly (bypasses the ingress) —
#   NS=project-user-mani-malarvannan
#   kubectl run rf-x --rm -i --restart=Never --image=curlimages/curl -n "$NS" -- \
#     sh -c 'sleep 4; curl ... http://routeforge-api.'"$NS"'.svc.cluster.local:8080/...'
#   (the `sleep 4` lets the pod's Istio sidecar become ready before curl fires).
#
set -euo pipefail

BASE_URL="${BASE_URL:-https://routeforge-project-user-mani-malarvannan.ai-application.pcai0109.dc15.hpecolo.net}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@routeforge.local}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-changeme}"

# Seeded demo app tokens (app/apps/bootstrap.py SEED_APPS). Bearer token == tenant:
#   acme-key      -> acme      (private_cloud_ok — cloud providers eligible)
#   hospital-key  -> hospital  (onprem_only     — cloud providers filtered out)
#   demo-key      -> default   (any)
ACME_KEY="acme-key"
HOSPITAL_KEY="hospital-key"

# Pretty-print JSON if python is available; otherwise pass through raw.
pretty() { python -c 'import sys,json; print(json.dumps(json.load(sys.stdin),indent=2))' 2>/dev/null || cat; }

cmd="${1:-burst}"
case "$cmd" in
  burst)
    N="${2:-500}"; RPS="${3:-20}"
    jar="$(mktemp)"
    echo "-- login as $ADMIN_EMAIL --"
    curl -s -c "$jar" -o /dev/null -w "login=%{http_code}\n" -X POST "$BASE_URL/admin/login" \
      -H "Content-Type: application/json" \
      -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}"
    echo "-- start burst: $N requests @ $RPS rps (runs inside the API pod) --"
    curl -s -b "$jar" -X POST "$BASE_URL/api/traffic" \
      -H "Content-Type: application/json" \
      -d "{\"requests\":$N,\"rps\":$RPS}" | pretty
    rm -f "$jar"
    echo "Refresh the dashboard in ~$(( N / RPS + 5 ))s — totals come from the DB."
    ;;

  single-simple)
    echo "-- Acme (cloud-eligible) simple question -> expect cheap tier --"
    curl -s -X POST "$BASE_URL/v1/chat/completions" \
      -H "Authorization: Bearer $ACME_KEY" \
      -H "Content-Type: application/json" \
      -d '{"messages":[{"role":"user","content":"What is the capital of France?"}]}' | pretty
    ;;

  single-pii)
    echo "-- General Health (onprem_only) with a synthetic SSN -> expect PII detected, on-prem, cloud ineligible --"
    curl -s -X POST "$BASE_URL/v1/chat/completions" \
      -H "Authorization: Bearer $HOSPITAL_KEY" \
      -H "Content-Type: application/json" \
      -d '{"messages":[{"role":"user","content":"My SSN is 452-11-9871 and you can reach me at jane.doe@example.com. Please update my patient record with this contact info."}]}' | pretty
    ;;

  *)
    echo "usage: $0 {burst [N] [RPS]|single-simple|single-pii}" >&2
    exit 2
    ;;
esac
