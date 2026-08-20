import pytest

from app.api.history import summarize, stats_over, fleet_over, RequestSummary


def _resp(rid, model="cheap-1", usd=0.01, base=0.10):
    return {"id": rid, "model": model, "model_display": model,
            "policy": {"sensitivity": "public", "onprem_only": False, "detected_entities": []},
            "usage": {"total_tokens": 10},
            "cost": {"total_usd": usd, "baseline_usd": base,
                     "routing_saved_usd": base - usd, "compression_saved_usd": 0.0},
            "quality": {"sum": 0.0, "chosen": None, "source": None},
            "trace": [{"step": "router", "latency_ms": 5.0},
                      {"step": "verify", "latency_ms": 2.0}]}


def test_summarize_maps_fields_and_counts_one_hop():
    s = summarize(_resp("r1"), tenant_id="acme", tier="cheap", provider="onprem")
    assert isinstance(s, RequestSummary)
    assert s.id == "r1" and s.tenant_id == "acme" and s.tier == "cheap"
    assert s.provider == "onprem" and s.latency_ms == 7.0    # 5 + 2
    assert s.hops == 1                                        # only the "router" record
    assert s.routing_saved_usd == pytest.approx(0.09)
    assert s.created_seq == 0


def test_stats_and_fleet_over_summaries():
    ss = [summarize(_resp("r1"), "acme", "cheap", "onprem"),
          summarize(_resp("r2", usd=0.5, base=0.5), "beta", "frontier", "azure")]
    st = stats_over(ss)
    assert st["count"] == 2
    assert st["tier_distribution"] == {"cheap": 1, "frontier": 1}
    fl = fleet_over(ss)
    assert {r["app"] for r in fl} == {"acme", "beta"}
