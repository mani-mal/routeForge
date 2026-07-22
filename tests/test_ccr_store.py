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


def test_to_cloud_counter():
    s = CCRStore(ttl_seconds=60, clock=lambda: 0.0)
    cid = s.store("x")
    s.retrieve(cid, to_cloud=True)
    assert s.retrievals == 1 and s.retrievals_to_cloud == 1
