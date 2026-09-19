"""Idempotent demo seeding.

Seeds the three example applications that used to live in `app/main.py`'s static
`API_KEYS` dict, using the SAME token strings (`acme-key`, `hospital-key`,
`demo-key`) so existing clients, the traffic generator, and the test suite keep
working unchanged after the switch to token->application resolution.

The application `id` equals the old tenant slug, so `policy.yaml` tenants
(residency defaults) still resolve by that key in `build_context`.

For a real deployment these demo apps/tokens should be removed and real ones
created via the admin dashboard.
"""
from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import AsyncSession

from app.apps import store
from app.apps.admin_auth import seed_admin
from app.apps.store import (
    add_token, create_application, get_application, replace_privacy_profile,
)

# (app_id, display name, default residency, legacy token)
SEED_APPS: list[tuple[str, str, str, str]] = [
    ("acme", "RAG Agent", "private_cloud_ok", "acme-key"),
    ("hospital", "Hospital Agent", "onprem_only", "hospital-key"),
    ("default", "Support Agent", "any", "demo-key"),
]

# Demoted-defaults rule set: same as policy.yaml's global sensitivity_rules,
# except LOCATION+DATE_TIME are demoted from confidential/medium down to
# internal/low/any — a bare "capital of France?" question should target the
# cheap tier, not be treated as sensitive PII. Closes architecture-review 4.1
# for the demo apps (RAG Agent, Support Agent). Hospital keeps them demoted
# too but adds a restricted/high/onprem_only MRN recognizer on top.
_DEMOTED: list[dict] = [
    {"entities": ["US_SSN", "MEDICAL_LICENSE", "US_BANK_NUMBER", "CREDIT_CARD",
                  "EMPLOYEE_ID"],
     "sensitivity": "restricted", "risk_class": "high", "residency": "onprem_only",
     "require_verification": True},
    {"entities": ["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER"],
     "sensitivity": "confidential", "risk_class": "medium",
     "residency": "private_cloud_ok", "require_verification": False},
    {"entities": ["LOCATION", "DATE_TIME"],
     "sensitivity": "internal", "risk_class": "low", "residency": "any",
     "require_verification": False},
    {"entities": [], "sensitivity": "public", "risk_class": "low", "residency": "any",
     "require_verification": False},
]


async def seed_demo_apps(session: AsyncSession) -> None:
    """Create the demo apps + their legacy tokens if absent. Idempotent."""
    for app_id, name, residency, token in SEED_APPS:
        if await get_application(session, app_id) is not None:
            continue
        await create_application(
            session, app_id=app_id, name=name, default_residency=residency)
        await add_token(session, application_id=app_id, raw_token=token, name="seed")


async def seed_privacy_profiles(session: AsyncSession) -> None:
    """Seed each demo app's privacy profile. Idempotent: `replace_privacy_profile`
    deletes+inserts, so re-running yields the same single MRN recognizer for
    hospital and the same demoted rule set for the others."""
    for app_id in ("acme", "default"):
        await replace_privacy_profile(session, app_id, recognizers=[], rules=_DEMOTED)
    hospital_rules = [
        {"entities": ["MRN"], "sensitivity": "restricted", "risk_class": "high",
         "residency": "onprem_only", "require_verification": True},
    ] + _DEMOTED
    await replace_privacy_profile(
        session, "hospital",
        recognizers=[{"name": "MRN", "regex": r"\bMRN-\d{7}\b", "score": 0.9}],
        rules=hospital_rules)


async def seed_test_models(session: AsyncSession) -> None:
    """TEST-ONLY. Reproduces the former built-in Qwen models as onboarded rows
    so the existing pipeline test-suite (which routes to local-8b/local-32b by
    name) works after those built-ins left the YAML pool. Gated on
    SP_SEED_TEST_MODELS — production never sets it, so the pool boots empty.
    Idempotent."""
    if os.environ.get("SP_SEED_TEST_MODELS", "").lower() not in ("1", "true", "yes"):
        return
    for slug, tier, price in (("local-8b", "cheap", 0.05), ("local-32b", "mid", 0.10)):
        if await store.get_onboarded(session, slug):
            continue
        await store.upsert_onboarded(
            session, slug, display_name="Qwen2.5-7B-Instruct (local)",
            provider="onprem", tier=tier, endpoint="http://test-local/v1",
            api_key_env=None, price_input=price, price_output=price,
            enabled=True, probe_status="healthy", allowed_app_ids=None,
            # P1b data-handling declaration. These two rows stand in for a
            # local endpoint INSIDE the residency boundary, which is exactly
            # the endpoint an operator declares trusted with unredacted
            # content -- without it, every suite that drives an app in a
            # `model_input: raw` posture over a request carrying any entity
            # would hit the raw-sensitive exclusion (design §4) and reject,
            # which is the rule working, not the fixture's subject. Region and
            # retention stay UNDECLARED on purpose, so the default fixture
            # keeps exercising the `unknown` path for those two.
            accepts_raw_sensitive=True,
            notes="test fixture")


async def seed_all(session: AsyncSession) -> None:
    """Everything the Database seeds on first boot: demo apps + one admin."""
    await seed_demo_apps(session)
    await seed_admin(session)
    await seed_privacy_profiles(session)
    await seed_test_models(session)
