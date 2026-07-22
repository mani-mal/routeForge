# scripts
- dev-backend.sh  — run the API (uvicorn, reload). `uv sync`s deps first (creates .venv if missing). Defaults to the regex PII detector; `SP_PII_DETECTION=presidio` installs the extra + spaCy model.
- dev-frontend.sh — run the UI dev server (stub UI this pass)
- test.sh         — run pytest
- smoke.sh        — boot API + live httpx checks
- lint-config.sh  — validate the 4 config YAMLs
- traffic.sh      — run the traffic generator (stub this pass)
- obs-up.sh       — start local Langfuse (Postgres/ClickHouse/Redis/MinIO)
- smoke-obs.sh    — prove a PII request lands in Langfuse with the SSN redacted
- smoke-agent.sh  — prove a mock_multistep request runs the real NAT agent and yields >=2 routed agent_hop records
