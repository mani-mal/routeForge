# Serving Plane API image — multi-stage, non-root, pinned bases.
# Default runtime is the demo-safe path: inference_backend=mock, regex PII
# detector, deterministic compressor — no GPU, no network, no API keys.
#
# EXTRAS build-arg controls which optional dependency groups are installed:
#   "agent,observability"  (default) — NAT multi-step agent + Langfuse export
#   ""                     — lean mock-only API (smallest image)
#   "agent,observability,presidio" — + Presidio (needs a spaCy model download)
# The NAT (`agent`) extra is large (LangChain stack); the default image is
# therefore well above the skill's 500MB aspiration. Pass EXTRAS="" for a lean
# build when multi-step/observability aren't needed.
ARG PYTHON_VERSION=3.12

# ---- builder ---------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

# uv: fast, reproducible installs from the committed lockfile.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# `postgres` (asyncpg) is included by default so the multi-tenant store can use
# the shared Langfuse Postgres via DATABASE_URL; dev/tests fall back to SQLite.
ARG EXTRAS="agent,observability,postgres"

# Copy only what's needed to resolve/install deps first, so the (slow) dep
# layer caches across source-only changes.
COPY pyproject.toml uv.lock ./
COPY nat_tools/ ./nat_tools/
# Build the extra flags: "a,b" -> "--extra a --extra b"
RUN EXTRA_FLAGS=""; \
    for e in $(echo "${EXTRAS}" | tr ',' ' '); do EXTRA_FLAGS="$EXTRA_FLAGS --extra $e"; done; \
    uv sync --frozen --no-install-project ${EXTRA_FLAGS}

# Now the source, then install the project itself into the venv.
COPY app/ ./app/
COPY config/ ./config/
COPY tests/ ./tests/
RUN EXTRA_FLAGS=""; \
    for e in $(echo "${EXTRAS}" | tr ',' ' '); do EXTRA_FLAGS="$EXTRA_FLAGS --extra $e"; done; \
    uv sync --frozen ${EXTRA_FLAGS}

# Presidio's AnalyzerEngine loads a spaCy pipeline at construction time and the
# model is NOT a pip dependency — without this the presidio build installs
# cleanly and then dies on the first request with OSError: [E050] Can't find
# model 'en_core_web_lg'. Only paid for when presidio is actually requested.
RUN case ",${EXTRAS}," in \
      *,presidio,*) /app/.venv/bin/python -m spacy download en_core_web_lg ;; \
      *) echo "presidio extra not requested; skipping spaCy model" ;; \
    esac

# ---- runtime ---------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

# curl for the HEALTHCHECK; tini for correct signal handling as PID 1.
RUN apt-get update && apt-get install -y --no-install-recommends curl tini \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user.
RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app

# The venv + source. WORKDIR is the repo root so config/*.yaml and
# tests/personas.yaml (the /api/traffic live-fire generator, Phase 7) resolve
# via their cwd-relative paths.
COPY --from=builder --chown=app:app /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    # Demo-safe defaults; override via compose/Helm env.
    SP_INFERENCE_BACKEND=mock \
    SP_PII_DETECTION=regex \
    SP_SERVER_PORT=8080

USER app
EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=5 \
    CMD curl -fsS "http://127.0.0.1:${SP_SERVER_PORT:-8080}/health" || exit 1

ENTRYPOINT ["tini", "--"]
# Honor SP_SERVER_PORT (Helm sets it from servingPlane.server.port) so the port
# uvicorn binds, the healthcheck, and the probes never diverge. Defaults to 8080.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${SP_SERVER_PORT:-8080}"]
