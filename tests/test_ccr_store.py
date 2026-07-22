from app.compression.ccr import CCRStore


def test_store_and_retrieve_local():
    t = [1000.0]
    s = CCRStore(ttl_seconds=60, clock=lambda: t[0])
    cid = s.store("original secret-free but full text")
    assert s.retrieve(cid, to_cloud=False) == "original secret-free but full text"
    assert s.retrievals == 1
    assert s.retrievals_to_cloud == 0


def test_ttl_expiry_evicts():
    t = [0.0]
    s = CCRStore(ttl_seconds=10, clock=lambda: t[0])
    cid = s.store("x")
    t[0] = 11.0
    assert s.retrieve(cid, to_cloud=False) is None
    assert s.size == 0  # expired entry evicted
    # a retrieve that returns nothing must NOT bump the honesty counters
    assert s.retrievals == 0
    assert s.retrievals_to_cloud == 0


def test_missing_id_never_bumps_counters():
    s = CCRStore(ttl_seconds=60, clock=lambda: 0.0)
    assert s.retrieve("never-stored", to_cloud=True) is None
    assert s.retrievals == 0
    assert s.retrievals_to_cloud == 0


def test_to_cloud_counter():
    s = CCRStore(ttl_seconds=60, clock=lambda: 0.0)
    cid = s.store("x")
    s.retrieve(cid, to_cloud=True)
    assert s.retrievals == 1 and s.retrievals_to_cloud == 1


def test_store_sweeps_expired_entries_without_retrieval():
    """TTL must be enforced even when nothing ever retrieves — the demo's
    steady state. An opportunistic sweep on store() keeps the cache bounded
    (CLAUDE.md rule 6: enforced TTL, not an unbounded retention liability)."""
    t = [0.0]
    s = CCRStore(ttl_seconds=10, clock=lambda: t[0])
    for _ in range(5):
        s.store("original")
    assert s.size == 5
    t[0] = 100.0  # all 5 now well past TTL
    s.store("fresh")           # this write sweeps the 5 expired entries first
    assert s.size == 1         # only the fresh one survives — TTL actually fired
