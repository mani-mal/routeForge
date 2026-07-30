"""Task 2: ORM tables for per-app recognizers and sensitivity rules.

The suite has no pytest-asyncio/anyio plugin installed (see
tests/test_decision_stream.py's module docstring) — async behavior is driven
with `asyncio.run(...)` inside an ordinary sync `def test_...`, not
`@pytest.mark.asyncio`. Mirrored here rather than the brief's suggested
`@pytest.mark.asyncio` marker, which this suite can't collect.
"""
from __future__ import annotations

import asyncio

from app.apps.db import Database
from app.apps.models import Application, AppRecognizer, AppSensitivityRule


def test_recognizer_and_rule_persist_and_cascade():
    async def _run():
        db = Database(url="sqlite+aiosqlite://", seed=None)  # isolated in-memory DB
        async with db.session() as s:
            s.add(Application(id="t1", name="T1", default_residency="any"))
            s.add(AppRecognizer(application_id="t1", name="MRN",
                                 regex=r"\bMRN-\d{7}\b", score=0.9))
            s.add(AppSensitivityRule(
                application_id="t1", position=0, entities=["MRN"],
                sensitivity="restricted", risk_class="high",
                residency="onprem_only", require_verification=True))
            await s.commit()

        async with db.session() as s:
            app = await s.get(Application, "t1")
            # deleting the app cascades to its recognizers/rules
            await s.delete(app)
            await s.commit()

        async with db.session() as s:
            assert await s.get(AppRecognizer, ("t1", "MRN")) is None
            assert await s.get(AppSensitivityRule, ("t1", 0)) is None

        await db.dispose()

    asyncio.run(_run())
