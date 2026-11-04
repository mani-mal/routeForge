"""Model metrics slice 1, Task 4: completion tokens on endpoint observations."""
from __future__ import annotations

import asyncio
import importlib.util
import time
from pathlib import Path

from app.apps import store
from app.apps.bootstrap import seed_demo_apps
from app.apps.db import Database
from app.config.schema import SignalsCfg
from app.routing.observations import Observation, ObservationSink
from app.routing.signals import SignalStore

CFG = SignalsCfg(min_samples=3, half_life_s=100.0, staleness_s=1000.0)


def test_success_logs_completion_tokens():
    sink = ObservationSink(10)
    s = SignalStore(CFG, sink=sink, wall_offset=1_000_000.0)
    s.observe_success("m", 1200.0, completion_tokens=500, now=5.0)
    (obs,) = sink.drain()
    assert obs.completion_tokens == 500
    assert obs.latency_ms == 1200.0


def test_written_row_round_trips_completion_tokens():
    db = Database(url="sqlite+aiosqlite://", seed=seed_demo_apps)
    now = time.time()
    rows = [Observation(now - 10, "m", "ok", 1200.0, False, None, 500),
            Observation(now - 5, "m", "ok", 900.0, False, None)]

    async def go():
        async with db.session() as s:
            await store.write_observations(s, rows)
            await s.commit()
            return await store.load_seed_observations(
                s, since_hours=1, per_model_cap=100)

    got = asyncio.run(go())
    assert [o.completion_tokens for o in got] == [500, None]


def test_replay_ignores_completion_tokens():
    def build(tokens):
        st = SignalStore(CFG, wall_offset=1_000_000.0)
        st.replay([Observation(1_000_000.0 + 10 + i, "m", "ok", 100.0 + i,
                               False, None, tokens) for i in range(5)])
        return st.snapshot()

    assert build(500) == build(None)


def test_migration_lists_observation_column():
    path = Path(__file__).resolve().parents[2] / "scripts" / "migrate_app_policies.py"
    spec = importlib.util.spec_from_file_location("migrate_app_policies", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.OBSERVATIONS_TABLE == "endpoint_observations"
    assert "completion_tokens" in {c[0] for c in mod.OBSERVATIONS_COLUMNS}
