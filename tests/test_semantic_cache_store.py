"""DeterministicSemanticCache — CCR-class semantic-cache substrate (feature #6
Task 1). NOT wired into the pipeline yet; these tests exercise the store in
isolation. See docs/architecture/semantic-cache-residency-spec.md."""

from app.cache.store import DeterministicSemanticCache


def _store(cache, **overrides):
    kwargs = dict(
        tenant_id="acme",
        onprem_only=False,
        allowed_providers=frozenset({"onprem", "azure"}),
        query="What is the capital of France?",
        response_text="Paris.",
        model="gpt-4o",
        provider="azure",
    )
    kwargs.update(overrides)
    cache.store(**kwargs)


def test_store_and_lookup_same_partition_hits():
    c = DeterministicSemanticCache(ttl_seconds=3600, clock=lambda: 1000.0)
    _store(c, onprem_only=False, provider="azure")
    entry = c.lookup(tenant_id="acme", onprem_only=False,
                      allowed_providers=frozenset({"onprem", "azure"}),
                      query="What is the capital of France?")
    assert entry is not None
    assert entry.response_text == "Paris."
    assert entry.provider == "azure"
    assert c.hits == 1


# --- C2: tenant isolation --------------------------------------------------

def test_tenant_isolation_no_cross_tenant_hit():
    c = DeterministicSemanticCache(ttl_seconds=3600, clock=lambda: 0.0)
    _store(c, tenant_id="acme")
    entry = c.lookup(tenant_id="other-tenant", onprem_only=False,
                      allowed_providers=frozenset({"onprem", "azure"}),
                      query="What is the capital of France?")
    assert entry is None
    assert c.hits == 0


def test_falsy_tenant_id_raises_on_store_and_lookup():
    c = DeterministicSemanticCache(ttl_seconds=3600, clock=lambda: 0.0)
    try:
        _store(c, tenant_id="")
        assert False, "expected ValueError"
    except ValueError:
        pass
    try:
        c.lookup(tenant_id="", onprem_only=False,
                 allowed_providers=frozenset({"onprem"}), query="hi")
        assert False, "expected ValueError"
    except ValueError:
        pass


# --- C3: residency partition (LOAD-BEARING) --------------------------------

def test_cloud_produced_entry_never_served_to_onprem_only_lookup():
    """A cloud-produced entry stored under a non-onprem partition must never
    be returned to an onprem_only lookup for the same query, and the honesty
    counter must stay 0."""
    c = DeterministicSemanticCache(ttl_seconds=3600, clock=lambda: 0.0)
    _store(c, tenant_id="acme", onprem_only=False,
           allowed_providers=frozenset({"onprem", "azure"}), provider="azure")
    entry = c.lookup(tenant_id="acme", onprem_only=True,
                      allowed_providers=frozenset({"onprem", "azure"}),
                      query="What is the capital of France?")
    assert entry is None
    assert c.served_from_cloud_entry == 0
    assert c.hits == 0


def test_store_refuses_residency_inconsistent_entry_outright():
    """store() itself refuses to persist a cloud-produced entry under an
    onprem_only partition -- the write-side half of C3's defense in depth."""
    c = DeterministicSemanticCache(ttl_seconds=3600, clock=lambda: 0.0)
    _store(c, tenant_id="acme", onprem_only=True,
           allowed_providers=frozenset({"onprem"}), provider="azure")
    assert c.size == 0
    entry = c.lookup(tenant_id="acme", onprem_only=True,
                      allowed_providers=frozenset({"onprem"}),
                      query="What is the capital of France?")
    assert entry is None


def test_lookup_itself_refuses_rogue_cloud_entry_in_onprem_partition():
    """Defense-in-depth: even if a cloud-produced entry ends up inside an
    onprem_only partition (bypassing store()'s own write-side refusal --
    e.g. a future backend, a bug, direct data manipulation), lookup() must
    still refuse to return it. This proves C3 is enforced INSIDE lookup, not
    merely a consequence of store() always behaving."""
    c = DeterministicSemanticCache(ttl_seconds=3600, clock=lambda: 0.0)
    part = ("acme", True, frozenset({"onprem"}))
    from app.cache.store import CacheEntry
    c._data[part] = {
        "rogue query": CacheEntry(response_text="leaked cloud answer",
                                   model="gpt-4o", provider="azure",
                                   timestamp=0.0)
    }
    entry = c.lookup(tenant_id="acme", onprem_only=True,
                      allowed_providers=frozenset({"onprem"}),
                      query="rogue query")
    assert entry is None
    assert c.served_from_cloud_entry == 1  # honesty counter fires on the rogue path
    assert c.hits == 0


def test_onprem_produced_entry_is_served_to_onprem_only_lookup():
    c = DeterministicSemanticCache(ttl_seconds=3600, clock=lambda: 0.0)
    _store(c, tenant_id="acme", onprem_only=True,
           allowed_providers=frozenset({"onprem"}), provider="onprem")
    entry = c.lookup(tenant_id="acme", onprem_only=True,
                      allowed_providers=frozenset({"onprem"}),
                      query="What is the capital of France?")
    assert entry is not None
    assert entry.provider == "onprem"
    assert c.hits == 1
    assert c.served_from_cloud_entry == 0


def test_allowed_providers_is_part_of_the_partition():
    """A different allowed_providers set is a different partition even for the
    same tenant/onprem_only/query -- an app whose policy narrowed providers
    must not see an entry cached under a broader/different provider set."""
    c = DeterministicSemanticCache(ttl_seconds=3600, clock=lambda: 0.0)
    _store(c, tenant_id="acme", onprem_only=False,
           allowed_providers=frozenset({"onprem", "azure"}), provider="azure")
    entry = c.lookup(tenant_id="acme", onprem_only=False,
                      allowed_providers=frozenset({"onprem", "bedrock"}),
                      query="What is the capital of France?")
    assert entry is None


# --- C5: TTL ----------------------------------------------------------------

def test_ttl_expiry_evicts_on_lookup():
    t = [0.0]
    c = DeterministicSemanticCache(ttl_seconds=10, clock=lambda: t[0])
    _store(c)
    t[0] = 11.0
    entry = c.lookup(tenant_id="acme", onprem_only=False,
                      allowed_providers=frozenset({"onprem", "azure"}),
                      query="What is the capital of France?")
    assert entry is None
    assert c.size == 0
    assert c.hits == 0


def test_ttl_expiry_swept_on_next_write_without_lookup():
    t = [0.0]
    c = DeterministicSemanticCache(ttl_seconds=10, clock=lambda: t[0])
    for i in range(5):
        _store(c, query=f"query {i}")
    assert c.size == 5
    t[0] = 100.0  # all 5 now well past TTL
    _store(c, query="fresh query")  # this write sweeps the 5 expired first
    assert c.size == 1


# --- normalization -----------------------------------------------------------

def test_normalized_query_match():
    c = DeterministicSemanticCache(ttl_seconds=3600, clock=lambda: 0.0)
    _store(c, query="Hello  World")
    entry = c.lookup(tenant_id="acme", onprem_only=False,
                      allowed_providers=frozenset({"onprem", "azure"}),
                      query="hello world")
    assert entry is not None


def test_different_query_misses():
    c = DeterministicSemanticCache(ttl_seconds=3600, clock=lambda: 0.0)
    _store(c, query="Hello World")
    entry = c.lookup(tenant_id="acme", onprem_only=False,
                      allowed_providers=frozenset({"onprem", "azure"}),
                      query="Goodbye World")
    assert entry is None
