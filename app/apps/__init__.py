"""Multi-tenant application layer (spec 2026-07-25).

routeForge is a model-router product: client *applications* register, get API
tokens, and call the OpenAI-compatible endpoint. This package owns application
identity, API tokens (hashed), per-application policy, durable usage metering,
and admin users — persisted in SQLAlchemy (SQLite for dev/test, the shared
Langfuse Postgres in deployment via DATABASE_URL).

The routing/privacy/compression core is untouched; this is the product skin.
"""
