"""Per-process cache of resolved app tokens (B86).

Every `/v1/*` request used to resolve its bearer token with three SELECTs and
stamp `last_used_at` with a write + commit. This cache keeps, per token hash,
exactly what the request path needs from that lookup -- the application id,
its policy view, its own Langfuse identity, the token id, expiry and session
binding -- for `server.auth_cache_ttl_s` seconds (`config/serving.yaml`).

The guarantees, in order of importance:

- **Fail closed.** Only a SUCCESSFUL lookup is cached. An unknown, revoked or
  expired token, or one whose application is not active, is never cached, so
  it always reaches the database (and a newly issued token works at once,
  everywhere). Expiry and session binding are re-checked on every hit against
  the cached values. Any error inside the cache is treated as a miss.
- **Same process: immediate.** Any committed write to `applications`,
  `app_policies` or `app_tokens` (other than the `last_used_at` stamp) through
  a `Database` session clears the whole cache (`app/apps/db.py`). A lookup
  that started before such a clear cannot repopulate the cache with what it
  read (generation check).
- **Other replicas: at most one TTL.** A revoked token can keep working on
  another replica for up to `auth_cache_ttl_s` seconds; set it to 0 to
  disable the cache (every request then hits the database, as before B86).

`last_used_at` is stamped at most once per `stamp_interval_s` (60 s) per
token per process, so the admin console's "last used" can be up to 60 s old.
With the cache disabled (TTL 0) every request stamps it, as before B86.
"""
from __future__ import annotations

import datetime as dt
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

DEFAULT_MAX_ENTRIES = 10_000
STAMP_INTERVAL_S = 60.0


@dataclass(frozen=True)
class AuthEntry:
    """What `_authenticate` needs from one successful token lookup."""
    app_id: str
    policy: object             # AppPolicyView (frozen)
    app_langfuse: object | None
    token_id: str
    expires_at: dt.datetime | None
    session_binding: str | None


class AuthCache:
    def __init__(self, ttl_s: float, *, max_entries: int = DEFAULT_MAX_ENTRIES,
                 stamp_interval_s: float = STAMP_INTERVAL_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s = float(ttl_s)
        self.max_entries = max_entries
        self.stamp_interval_s = stamp_interval_s
        self._clock = clock
        self._d: OrderedDict[str, tuple[float, AuthEntry]] = OrderedDict()
        self._stamped: OrderedDict[str, float] = OrderedDict()
        self.generation = 0

    @property
    def enabled(self) -> bool:
        return self.ttl_s > 0

    def get(self, token_hash: str) -> AuthEntry | None:
        """The cached entry, or None if absent, older than the TTL, or the
        cache is disabled."""
        if not self.enabled:
            return None
        hit = self._d.get(token_hash)
        if hit is None:
            return None
        stored_at, entry = hit
        if self._clock() - stored_at >= self.ttl_s:
            self._d.pop(token_hash, None)
            return None
        self._d.move_to_end(token_hash)
        return entry

    def put(self, token_hash: str, entry: AuthEntry, generation: int) -> None:
        """Store a successful lookup -- unless the cache was cleared since the
        lookup began (`generation` is `self.generation` read before it)."""
        if not self.enabled or generation != self.generation:
            return
        self._d[token_hash] = (self._clock(), entry)
        self._d.move_to_end(token_hash)
        while len(self._d) > self.max_entries:
            self._d.popitem(last=False)

    def clear(self) -> None:
        self.generation += 1
        self._d.clear()

    def __len__(self) -> int:
        return len(self._d)

    def stamp_due(self, token_id: str) -> bool:
        """True (and the stamp recorded) when this token's `last_used_at` was
        not stamped by this process in the last `stamp_interval_s`. With the
        cache disabled (TTL 0) always True: every request stamps, exactly as
        before B86."""
        if not self.enabled:
            return True
        now = self._clock()
        last = self._stamped.get(token_id)
        if last is not None and now - last < self.stamp_interval_s:
            return False
        self._stamped[token_id] = now
        self._stamped.move_to_end(token_id)
        while len(self._stamped) > self.max_entries:
            self._stamped.popitem(last=False)
        return True
