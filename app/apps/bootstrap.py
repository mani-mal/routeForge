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

from sqlalchemy.ext.asyncio import AsyncSession

from app.apps.store import add_token, create_application, get_application

# (app_id, display name, default residency, legacy token)
SEED_APPS: list[tuple[str, str, str, str]] = [
    ("acme", "Acme Corp", "private_cloud_ok", "acme-key"),
    ("hospital", "General Health", "onprem_only", "hospital-key"),
    ("default", "Demo Tenant", "any", "demo-key"),
]


async def seed_demo_apps(session: AsyncSession) -> None:
    """Create the demo apps + their legacy tokens if absent. Idempotent."""
    for app_id, name, residency, token in SEED_APPS:
        if await get_application(session, app_id) is not None:
            continue
        await create_application(
            session, app_id=app_id, name=name, default_residency=residency)
        await add_token(session, application_id=app_id, raw_token=token, name="seed")
