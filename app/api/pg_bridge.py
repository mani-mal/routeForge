"""Cross-replica bridge for the live decision stream (B3).

The local `DecisionBus` only reaches SSE subscribers on THIS API pod. With 2+
pods behind the Kubernetes Service, a request served by pod A must also surface
in a browser streaming from pod B. This bridge carries that fan-out over
Postgres LISTEN/NOTIFY: on publish, pod A issues `NOTIFY <channel> '{rid,origin}'`
(tiny payload — no event bodies, well under the 8 KB cap); every pod LISTENs,
loads the snapshot for `rid`, rebuilds the event sequence, and republishes to
its OWN local bus. The originating pod skips its own notifications (it already
emitted locally on the serve path).

Inert on SQLite (tests/offline dev only): `asyncpg` is imported lazily so the
suite runs without the `postgres` extra, and every method is a no-op. Every real
deploy is Postgres, so the bridge is active exactly when multiple pods exist.
Fail-open: callers wrap `publish_remote` in the serve path's serving guard.
"""
from __future__ import annotations

import asyncio
import json
import logging

from app.apps import store

_log = logging.getLogger("serving.obs")


class PgDecisionBridge:
    def __init__(self, db, bus, *, replica_id: str,
                 channel: str = "route_decisions") -> None:
        self._db = db
        self._bus = bus
        self._replica_id = replica_id
        self._channel = channel
        self._conn = None                       # dedicated asyncpg LISTEN connection
        self._started = False
        self._lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return self._db.engine.url.drivername.startswith("postgresql")

    @staticmethod
    def rebuild_events(summary_json: dict, full_json: dict) -> list[dict]:
        """The exact event sequence the serve path emits locally: one 'request'
        event then one 'step' per trace record."""
        events: list[dict] = [{"type": "request", "summary": summary_json}]
        for index, record in enumerate(full_json.get("trace") or []):
            events.append({"type": "step", "request_id": full_json.get("id"),
                           "record": record, "index": index})
        return events

    async def publish_remote(self, request_id: str) -> None:
        if not self.active:
            return
        from sqlalchemy import text
        payload = json.dumps({"rid": request_id, "origin": self._replica_id})
        # NOTIFY is transactional — it fires on COMMIT, so we must commit.
        async with self._db.session() as s:
            await s.execute(text("SELECT pg_notify(:c, :p)"),
                            {"c": self._channel, "p": payload})
            await s.commit()

    async def ensure_listening(self) -> None:
        if not self.active or self._started:
            return
        async with self._lock:
            if self._started:
                return
            import asyncpg
            url = self._db.engine.url
            self._conn = await asyncpg.connect(
                host=url.host, port=url.port, user=url.username,
                password=url.password, database=url.database)
            await self._conn.add_listener(self._channel, self._on_notify)
            self._started = True

    def _on_notify(self, _conn, _pid, _channel, payload: str) -> None:
        # asyncpg calls this synchronously from its reader; schedule async work.
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            return
        if data.get("origin") == self._replica_id:
            return                              # our own request — already emitted locally
        rid = data.get("rid")
        if rid:
            asyncio.get_event_loop().create_task(self._deliver(rid))

    async def _deliver(self, request_id: str) -> None:
        try:
            from app.apps.models import RequestSnapshot
            async with self._db.session() as s:
                snap = await s.get(RequestSnapshot, request_id)
            if snap is None:                       # evicted/never written — nothing to fan out
                return
            for evt in self.rebuild_events(snap.summary_json, snap.full_json):
                await self._bus.publish(evt)
        except Exception:                          # bridge must never crash the listener
            _log.exception("pg_bridge deliver failed (swallowed)")

    async def stop(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.close()
            finally:
                self._conn = None
                self._started = False
