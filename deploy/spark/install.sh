#!/usr/bin/env bash
# deploy/spark/install.sh — preflight + bring-up of routeForge on a DGX Spark.
#
#   bash deploy/spark/install.sh            # router only (api + ui + postgres)
#   bash deploy/spark/install.sh --langfuse # + self-hosted Langfuse
#   bash deploy/spark/install.sh --check    # preflight only, change nothing
#
# Everything is built from source on this machine, so the images are natively
# arm64. This script deploys NO inference — see README.md step 5.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/../.." && pwd)"
PROJECT=routeforge-spark

WITH_LANGFUSE=0
CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --langfuse) WITH_LANGFUSE=1 ;;
    --check)    CHECK_ONLY=1 ;;
    -h|--help)  sed -n '2,9p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "  ok   $*"; }
warn() { echo "  WARN $*" >&2; }

echo "== preflight =="

arch="$(uname -m)"
case "$arch" in
  aarch64|arm64) ok "architecture $arch" ;;
  *) warn "architecture is $arch, not aarch64 — this file targets a DGX Spark (GB10).
       It still works on x86_64; only the image-architecture notes below differ." ;;
esac

command -v docker >/dev/null 2>&1 || fail "docker not found. Install Docker Engine + the compose plugin."
docker compose version >/dev/null 2>&1 || fail "'docker compose' (v2 plugin) not found. The old docker-compose v1 will not work."
ok "docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '?') with compose v2"
docker info >/dev/null 2>&1 || fail "cannot talk to the Docker daemon. Is it running, and is your user in the 'docker' group?"
ok "docker daemon reachable"

# Every third-party image this stack pulls must have an arm64 variant. The api
# and ui images are built locally, so they are not in this list.
if [ "$arch" = "aarch64" ] || [ "$arch" = "arm64" ]; then
  images="postgres:16"
  [ "$WITH_LANGFUSE" = 1 ] && images="$images langfuse/langfuse:3 langfuse/langfuse-worker:3 clickhouse/clickhouse-server:24 redis:7 minio/minio minio/mc"
  has_arm64() { docker manifest inspect "$1" 2>/dev/null | grep -q '"architecture": *"arm64"'; }
  for img in $images; do
    # Tried twice: the first registry call of a session can fail while the
    # client fetches its auth token, which would otherwise read as a false
    # "no arm64 variant" warning.
    if has_arm64 "$img" || has_arm64 "$img"; then
      ok "arm64 image available: $img"
    else
      warn "could not confirm an arm64 variant of $img (offline, or genuinely amd64-only).
       If 'docker compose up' later fails with 'no matching manifest for linux/arm64',
       that image is the reason — see README.md 'arm64 notes'."
    fi
  done
fi

if [ ! -f "${HERE}/.env" ]; then
  if [ "$CHECK_ONLY" = 1 ]; then
    warn "deploy/spark/.env missing (copy it from .env.example)"
  else
    cp "${HERE}/.env.example" "${HERE}/.env"
    fail "created deploy/spark/.env from the example.
       Edit it and set ADMIN_PASSWORD and POSTGRES_PASSWORD, then re-run."
  fi
else
  # shellcheck disable=SC1090
  set -a; . "${HERE}/.env"; set +a
  [ "${ADMIN_PASSWORD:-}" = "changeme-before-first-boot" ] && fail "ADMIN_PASSWORD is still the placeholder in deploy/spark/.env"
  [ -n "${ADMIN_PASSWORD:-}" ] || fail "ADMIN_PASSWORD unset in deploy/spark/.env"
  [ -n "${POSTGRES_PASSWORD:-}" ] || fail "POSTGRES_PASSWORD unset in deploy/spark/.env"
  ok "deploy/spark/.env present, required values set"
fi

free_port() {
  if command -v ss >/dev/null 2>&1 && ss -ltn "( sport = :$1 )" 2>/dev/null | grep -q LISTEN; then
    warn "port $1 is already in use — change ${2} in deploy/spark/.env"
  else
    ok "port $1 free"
  fi
}
free_port "${API_PORT:-8080}" API_PORT
free_port "${UI_PORT:-3001}" UI_PORT
[ "$WITH_LANGFUSE" = 1 ] && free_port "${LANGFUSE_PORT:-3000}" LANGFUSE_PORT

if [ "$CHECK_ONLY" = 1 ]; then
  echo "== preflight only (--check); nothing started =="
  exit 0
fi

FILES=(-f "${HERE}/docker-compose.yml")
if [ "$WITH_LANGFUSE" = 1 ]; then
  FILES+=(-f "${REPO}/deploy/compose/langfuse.yml" -f "${HERE}/docker-compose.langfuse.yml")
fi

echo "== build + up =="
echo "   (first build compiles the whole dependency stack for arm64 — allow 10-20 min)"
docker compose -p "$PROJECT" "${FILES[@]}" up -d --build

echo "== waiting for the api to report healthy =="
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${API_PORT:-8080}/health" >/dev/null 2>&1; then
    echo "  api healthy after ${i}0s"
    break
  fi
  [ "$i" = 60 ] && fail "api never became healthy. Logs: docker compose -p $PROJECT logs api"
  sleep 10
done

host="${SPARK_HOST:-localhost}"
cat <<SUMMARY

== routeForge is up ==
  console   http://${host}:${UI_PORT:-3001}     (login ${ADMIN_EMAIL:-admin@routeforge.local})
  api       http://${host}:${API_PORT:-8080}/health
$([ "$WITH_LANGFUSE" = 1 ] && echo "  langfuse  http://${host}:${LANGFUSE_PORT:-3000}  (demo@example.com / demodemo)")

The on-prem model pool is EMPTY — routeForge deploys no inference. Start your own
OpenAI-compatible server on the Spark's GPU and onboard it (README.md step 5),
or every onprem_only request will be rejected by design.
SUMMARY
