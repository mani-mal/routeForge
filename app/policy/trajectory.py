"""Trajectory-scoped policy envelope (B72, steps 1-2).

An agent's task is a sequence of model calls. RouteForge sees each call as its
own request, so a placement raised on one call (a detected identifier) used to
exist for that call only: a client that later sent a summary without the
identifier was routed as if nothing sensitive had happened.

The envelope remembers, per trajectory, the entity TYPES seen so far. The gate
adds them to what it detects in the current request before matching the
sensitivity rules, so whatever a matched rule sets -- placement, sensitivity,
risk class, verification -- is inherited by later calls.

    P_{t+1} = meet(P_t, classify(O_t))

It only tightens. Nothing lowers an envelope except expiry: a type stops
counting one lifetime after it was last DETECTED in a request of the
trajectory (an inherited type does not renew itself). It stores type names
and a hash, never text.

Per-app `trajectory_mode`:
  off     no envelope (the default; behaviour as before).
  header  calls carrying the same `x-routeforge-trajectory` id share one.
          A call with no id has no envelope.
  sticky  fail closed against a client that drops or changes its id: every
          call inherits what ANY of the app's live envelopes has seen.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass
from typing import Protocol

TRAJECTORY_MODES: tuple[str, ...] = ("off", "header", "sticky")
#: The envelope a `sticky` call with no usable id reads and writes.
ANONYMOUS_KEY = "-"
_ID_PAT = re.compile(r"[A-Za-z0-9._:\-]{1,128}")


def resolve_trajectory_mode(value) -> tuple[str, bool]:
    """`(mode, parsed)`. None = `off`. Anything unparseable denies to the
    protective end, `sticky`, and reports `parsed=False`."""
    if value is None:
        return "off", True
    if isinstance(value, str) and value in TRAJECTORY_MODES:
        return value, True
    return "sticky", False


def parse_trajectory_id(raw: str | None) -> tuple[str | None, str]:
    """`(id, source)` with source `client` | `none` | `invalid`. An invalid id
    is dropped, never guessed at: the call is then handled as one with no id,
    which `sticky` treats as restricted and `header` as unlinked."""
    if raw is None or raw == "":
        return None, "none"
    raw = raw.strip()
    if _ID_PAT.fullmatch(raw):
        return raw, "client"
    return None, "invalid"


def trajectory_key(app_id: str, trajectory_id: str | None) -> str:
    """The stored key: a hash scoped to the application, so two applications
    using the same id never share an envelope and the id itself is not kept."""
    if trajectory_id is None:
        return ANONYMOUS_KEY
    return hashlib.sha256(f"{app_id}\x00{trajectory_id}".encode()).hexdigest()[:32]


class TrajectoryStore(Protocol):
    async def read(self, app_id: str, key: str | None, now: dt.datetime) -> frozenset[str]:
        """Entity types of the live envelope `key`, or of every live envelope
        of the application when `key` is None."""

    async def add(self, app_id: str, key: str, types: frozenset[str],
                  now: dt.datetime) -> None:
        """Record that `types` were seen in envelope `key` at `now`."""


# An envelope is a set of APPEND-ONLY observations: each says "these types
# were seen in this trajectory, and count until `expires_at`". Reading unions
# the live ones. Nothing is ever updated in place, so two requests of one
# trajectory arriving together cannot overwrite each other -- the worst a race
# does is write the same observation twice, which a union does not notice.
#
# A new observation is skipped when a live one already covers the same types
# with at least half its lifetime left, so a long conversation writes about
# two rows per lifetime rather than one per call.

def _covered(rows, types: frozenset[str], now: dt.datetime, ttl: dt.timedelta) -> bool:
    fresh: set[str] = set()
    for row_types, expires in rows:
        if expires - now >= ttl / 2:
            fresh |= set(row_types)
    return types <= fresh


class InMemoryTrajectoryStore:
    """Single-process store for tests and the offline harness."""

    def __init__(self, ttl_seconds: float = 86400.0) -> None:
        self._ttl = dt.timedelta(seconds=ttl_seconds)
        self._rows: list[tuple[str, str, frozenset[str], dt.datetime]] = []

    async def read(self, app_id, key, now):
        out: set[str] = set()
        for a, k, types, expires in self._rows:
            if a == app_id and expires > now and (key is None or k == key):
                out |= types
        return frozenset(out)

    async def add(self, app_id, key, types, now):
        mine = [(t, e) for a, k, t, e in self._rows if a == app_id and k == key and e > now]
        if not _covered(mine, types, now, self._ttl):
            self._rows.append((app_id, key, frozenset(types), now + self._ttl))


class DbTrajectoryStore:
    """Envelopes in the deployment's own database, so every replica reads the
    same one. Inside the residency boundary by construction: it is the same
    database that holds the applications' policies."""

    def __init__(self, db, ttl_seconds: float = 86400.0) -> None:
        self._db = db
        self._ttl = dt.timedelta(seconds=ttl_seconds)

    async def _live(self, session, app_id, key, now):
        from sqlalchemy import select
        from app.apps.models import TrajectoryEnvelope
        q = select(TrajectoryEnvelope).where(
            TrajectoryEnvelope.application_id == app_id,
            TrajectoryEnvelope.expires_at > now)
        if key is not None:
            q = q.where(TrajectoryEnvelope.trajectory_key == key)
        return (await session.execute(q)).scalars().all()

    async def read(self, app_id, key, now):
        async with self._db.session() as s:
            rows = await self._live(s, app_id, key, now)
        out: set[str] = set()
        for r in rows:
            out |= set(r.entity_types or [])
        return frozenset(out)

    async def add(self, app_id, key, types, now):
        from app.apps.models import TrajectoryEnvelope
        async with self._db.session() as s:
            rows = await self._live(s, app_id, key, now)
            if _covered([(r.entity_types or [], _aware(r.expires_at)) for r in rows],
                        types, now, self._ttl):
                return
            s.add(TrajectoryEnvelope(
                application_id=app_id, trajectory_key=key,
                entity_types=sorted(types), updated_at=now,
                expires_at=now + self._ttl))
            await s.commit()


async def prune_envelopes(session, now: dt.datetime | None = None) -> int:
    """Delete expired observations (caller commits). Returns rows deleted."""
    from sqlalchemy import delete
    from app.apps.models import TrajectoryEnvelope
    now = now or dt.datetime.now(dt.timezone.utc)
    result = await session.execute(
        delete(TrajectoryEnvelope).where(TrajectoryEnvelope.expires_at <= now))
    return result.rowcount


def _aware(ts: dt.datetime) -> dt.datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=dt.timezone.utc)


@dataclass(frozen=True)
class Trajectory:
    """One request's handle on its envelope: what the route parsed off the
    request, plus the store. The pipeline calls `inherited` before the gate
    and `observe` after it, inside the gate's fail-closed block -- a store
    that cannot be read or written rejects the request."""

    store: TrajectoryStore
    app_id: str
    trajectory_id: str | None
    source: str

    @property
    def key(self) -> str:
        return trajectory_key(self.app_id, self.trajectory_id)

    def recorded_key(self, mode: str) -> str | None:
        """What goes on the outcome row: the key of the envelope this request
        wrote to -- the hashed id, or the anonymous key for a `sticky` call
        with no usable id -- and None when no envelope was in use."""
        if mode == "off" or (mode == "header" and self.trajectory_id is None):
            return None
        return self.key

    async def inherited(self, mode: str, now: dt.datetime | None = None) -> frozenset[str]:
        now = now or dt.datetime.now(dt.timezone.utc)
        if mode == "off":
            return frozenset()
        if mode == "sticky":
            return await self.store.read(self.app_id, None, now)
        if self.trajectory_id is None:          # header mode, unlinked call
            return frozenset()
        return await self.store.read(self.app_id, self.key, now)

    async def observe(self, mode: str, types, now: dt.datetime | None = None) -> None:
        """`types` are the entity types detected in THIS request's own text,
        never the inherited ones: writing those back would let an envelope
        renew itself forever."""
        types = frozenset(types)
        if mode == "off" or not types:
            return
        if mode == "header" and self.trajectory_id is None:
            return
        await self.store.add(self.app_id, self.key, types,
                             now or dt.datetime.now(dt.timezone.utc))
