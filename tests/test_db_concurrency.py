import asyncio
import os
import tempfile

from sqlalchemy import func, select

from app.apps.db import Database
from app.apps.models import RequestHistory
from app.apps import history_store as hs
from tests.test_history_store_write import _resp


def test_concurrent_writes_do_not_lose_rows_on_memory_sqlite():
    async def _inner():
        db = Database(url="sqlite+aiosqlite://")

        async def write(i):
            async with db.session() as s:
                await hs.record_history(s, _resp(f"r{i}"), tenant_id="acme",
                                        tier="cheap", provider="onprem",
                                        retention_rows=5000)
                await s.commit()

        # Many concurrent writers on the shared single connection.
        await asyncio.gather(*(write(i) for i in range(25)))

        async with db.session() as s:
            n = (await s.execute(
                select(func.count()).select_from(RequestHistory))).scalar_one()
        await db.dispose()
        return n

    assert asyncio.run(_inner()) == 25   # no writes lost


def test_serialize_sessions_false_for_file_url():
    assert Database(url="sqlite+aiosqlite:///./x.db")._serialize_sessions is False


async def _race_peak(db: Database) -> tuple[int, int]:
    """Force two writers to race for `db.session()` and report how many
    sessions were ever open at once (`peak`), plus how many were open right
    before writer A is released (`observed_before_release` — proves B could
    not enter while A still held its session, when serialization is on).

    Deterministic: `ensure_ready()` is warmed up first (its own lock/I-O is
    irrelevant to what's under test here), and B only tries to enter its
    session after A has confirmed entry via `a_entered`. B's attempt is given
    a bounded number of scheduling opportunities (`asyncio.sleep(0)` ticks)
    rather than an unbounded wait, so a lock that fails to release ever would
    fail this test on a timeout instead of hanging it.
    """
    await db.ensure_ready()
    open_count = 0
    peak = 0
    a_entered = asyncio.Event()
    release = asyncio.Event()

    async def writer_a():
        nonlocal open_count, peak
        async with db.session():
            open_count += 1
            peak = max(peak, open_count)
            a_entered.set()
            await release.wait()
            open_count -= 1

    async def writer_b():
        nonlocal open_count, peak
        await a_entered.wait()
        async with db.session():
            open_count += 1
            peak = max(peak, open_count)
            await asyncio.sleep(0)
            open_count -= 1

    task_a = asyncio.create_task(writer_a())
    task_b = asyncio.create_task(writer_b())

    # Bounded window for B to attempt entry while A is still inside.
    for _ in range(10):
        await asyncio.sleep(0)
    observed_before_release = open_count

    release.set()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)
    return peak, observed_before_release


def test_serialization_prevents_overlapping_sessions_on_memory_sqlite():
    """Mechanism-level guard for B18: proves `_session_lock` actually
    serializes whole sessions on the shared in-memory engine, independent of
    whether any particular data race happens to trigger within a test run.
    A future change that deletes the `_serialize_sessions` guard or narrows
    the lock's scope (e.g. only around the write, not the whole session)
    will fail this test directly.
    """
    async def _inner():
        db = Database(url="sqlite+aiosqlite://")
        assert db._serialize_sessions is True
        peak, observed_before_release = await _race_peak(db)
        await db.dispose()
        return peak, observed_before_release

    peak, observed_before_release = asyncio.run(_inner())
    # B could not enter its session while A still held its own.
    assert observed_before_release == 1
    # At most one session was ever open at a time.
    assert peak == 1


def test_no_serialization_allows_overlapping_sessions_on_file_sqlite():
    """Companion to the above: proves the gate actually gates — with a real
    per-connection pool (file sqlite), the same race is allowed to overlap.
    """
    async def _inner():
        with tempfile.TemporaryDirectory() as d:
            db = Database(url=f"sqlite+aiosqlite:///{os.path.join(d, 'mech.db')}")
            assert db._serialize_sessions is False
            peak, _ = await _race_peak(db)
            await db.dispose()
            return peak

    assert asyncio.run(_inner()) == 2
