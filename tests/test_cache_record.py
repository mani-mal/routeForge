from app.models import CacheRecord


def test_cache_record_is_counts_only():
    r = CacheRecord(enabled=True, hit=True, bypass_reason="", tokens_saved=120,
                     cache_saved_usd=0.002, entries_size=3,
                     served_from_cloud_entry=0, latency_ms=0.4)
    d = r.model_dump()
    assert d["hit"] is True
    assert d["tokens_saved"] == 120
    # structural: no query/response content field ever
    assert "query" not in d
    assert "response" not in d
    assert "response_text" not in d
    assert "text" not in d


def test_cache_record_defaults():
    r = CacheRecord(enabled=False, hit=False)
    assert r.bypass_reason == ""
    assert r.tokens_saved == 0
    assert r.cache_saved_usd == 0.0
    assert r.entries_size == 0
    assert r.served_from_cloud_entry == 0
    assert r.latency_ms == 0.0
