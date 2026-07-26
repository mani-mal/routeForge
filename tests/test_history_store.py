from app.api.history import HistoryStore


def _resp(rid, model="local-8b", tier="cheap", total=0.0, base=0.001,
          onprem=True, sens="public", ents=None, comp_saved=0.0, route_saved=0.001):
    return {
        "id": rid, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "trace": [{"step": "router", "chosen": model, "reason": "tier " + tier,
                   "latency_ms": 200.0, "tokens_priced": 10}],
        "cost": {"total_usd": total, "baseline_usd": base,
                 "routing_saved_usd": route_saved, "compression_saved_usd": comp_saved},
        "policy": {"sensitivity": sens, "onprem_only": onprem,
                   "allowed_providers": ["onprem"], "detected_entities": ents or []},
    }


def test_record_and_list_newest_first():
    h = HistoryStore(capacity=3)
    h.record(_resp("a", tier="cheap"), "acme", "cheap")
    h.record(_resp("b", tier="cheap"), "acme", "cheap")
    ids = [s.id for s in h.list()]
    assert ids == ["b", "a"]


def test_capacity_evicts_oldest():
    h = HistoryStore(capacity=2)
    for r in ("a", "b", "c"):
        h.record(_resp(r, tier="cheap"), "acme", "cheap")
    assert [s.id for s in h.list()] == ["c", "b"]
    assert h.get("a") is None


def test_get_returns_full_response():
    h = HistoryStore()
    h.record(_resp("a", model="local-32b", tier="mid"), "acme", "mid")
    assert h.get("a")["model"] == "local-32b"


def test_stats_empty_state_safe():
    s = HistoryStore().stats()
    assert s["count"] == 0 and s["total_usd"] == 0.0 and s["tier_distribution"] == {}


def test_stats_aggregate():
    h = HistoryStore()
    h.record(_resp("a", tier="cheap", onprem=True, base=0.002, route_saved=0.002),
              "acme", "cheap")
    h.record(_resp("b", tier="frontier", onprem=False, base=0.004, route_saved=0.001,
                   comp_saved=0.001), "acme", "frontier")
    s = h.stats()
    assert s["count"] == 2
    assert s["tier_distribution"] == {"cheap": 1, "frontier": 1}
    assert s["onprem_count"] == 1
    assert abs(s["baseline_usd"] - 0.006) < 1e-9
    assert abs(s["compression_saved_usd"] - 0.001) < 1e-9


def test_hops_counts_only_generation_records_not_all_trace():
    """A direct request has 1 hop (one 'router' record); verify/compression
    records must NOT inflate it. A multi-step run has one 'agent_hop' per LLM
    call. The flow visualizer distinguishes single-call vs multi-step on this."""
    h = HistoryStore()
    direct = _resp("d")
    direct["trace"] = [
        {"step": "router", "chosen": "local-8b", "latency_ms": 200.0},
        {"step": "verify", "passed": True},
        {"step": "compression", "applied": False},
    ]
    assert h.record(direct, "acme", "cheap").hops == 1

    multi = _resp("m")
    multi["trace"] = [
        {"step": "agent_hop", "chosen": "local-8b", "hop_index": 1, "latency_ms": 200.0},
        {"step": "agent_hop", "chosen": "local-32b", "hop_index": 2, "latency_ms": 500.0},
        {"step": "compression", "applied": True},
        {"step": "compression", "applied": True},
        {"step": "verify", "passed": True},
    ]
    assert h.record(multi, "acme", "cheap").hops == 2


def test_list_filters_by_app():
    from app.api.history import HistoryStore
    store = HistoryStore()
    base = {"id": "x", "trace": [], "cost": {}, "policy": {}, "model": "m",
            "model_display": "M", "usage": {}}
    store.record({**base, "id": "a1"}, tenant_id="acme", tier="cheap")
    store.record({**base, "id": "h1"}, tenant_id="hospital", tier="mid")
    store.record({**base, "id": "a2"}, tenant_id="acme", tier="cheap")
    ids = [s.id for s in store.list(app="acme")]
    assert ids == ["a2", "a1"]           # newest-first, hospital excluded
    assert len(store.list()) == 3        # unfiltered unchanged


def test_fleet_groups_by_app():
    from app.api.history import HistoryStore
    store = HistoryStore()
    def rec(id, tenant, entities):
        store.record({"id": id, "trace": [{"step": "router", "latency_ms": 10}],
                      "cost": {"total_usd": 1.0, "baseline_usd": 3.0,
                               "routing_saved_usd": 1.5, "compression_saved_usd": 0.5},
                      "policy": {"onprem_only": tenant == "hospital",
                                 "detected_entities": entities},
                      "model": "m", "model_display": "M", "usage": {}},
                     tenant_id=tenant, tier="cheap")
    rec("a1", "acme", [])
    rec("h1", "hospital", ["US_SSN"])
    rec("a2", "acme", ["EMAIL"])
    rows = {r["app"]: r for r in store.fleet()}
    assert rows["acme"]["count"] == 2
    assert rows["acme"]["pii_count"] == 1
    assert rows["hospital"]["onprem_count"] == 1
    assert store.fleet()[0]["count"] >= store.fleet()[-1]["count"]  # sorted desc


def test_list_non_positive_limit_returns_empty():
    """Regression: the newest-first/early-break loop must not append before
    checking `limit`, or `limit=0` (a reachable `GET /api/requests?limit=0`)
    would return 1 item instead of `[]` like the old `[:limit]` slice did."""
    from app.api.history import HistoryStore
    store = HistoryStore()
    base = {"id": "x", "trace": [], "cost": {}, "policy": {}, "model": "m",
            "model_display": "M", "usage": {}}
    store.record({**base, "id": "a1"}, tenant_id="acme", tier="cheap")
    store.record({**base, "id": "a2"}, tenant_id="acme", tier="cheap")
    assert store.list(limit=0) == []
    assert store.list(limit=-1) == []
    assert store.list(app="acme", limit=0) == []
