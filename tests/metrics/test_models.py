from datetime import date, datetime, timedelta, timezone

from app.config.schema import PricingCfg, PriceCfg
from app.metrics.compute import (FeedbackRow, ObservationRow, OutcomeRow, UsageRow,
                                 model_rows, page_alerts)

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def batch(prefix, model, n, cost=0.1, latency=100.0, cached=0, prompt=100, traj=None):
    us = [UsageRow(f"{prefix}{i}", "a", T0, model, "onprem", "ok", cost, prompt, cached,
                   None, 0.0, 0.0, 0.0) for i in range(n)]
    outs = [OutcomeRow(f"{prefix}{i}", "a", T0, "ok", model, None, traj, latency, None, [])
            for i in range(n)]
    return us, outs


def rows(cur, prev=([], []), obs=(), fb=()):
    return model_rows(cur[0], cur[1], list(fb), prev[0], prev[1], list(obs),
                      placement_of=lambda m: "onprem")


def one(result, model="m"):
    return next(r for r in result if r["model"] == model)


def test_status_cost_anomaly():
    r = one(rows(batch("c", "m", 40, cost=0.21), batch("p", "m", 40, cost=0.10)))
    assert r["status"] == "cost_anomaly"
    assert "spend per request" in r["status_reasons"][0]


def test_status_worst_wins():
    r = one(rows(batch("c", "m", 40, cost=0.21, cached=0), batch("p", "m", 40, cost=0.10, cached=80)))
    assert r["status"] == "cost_anomaly"
    assert len(r["status_reasons"]) == 2


def test_missing_previous_window_skips_checks():
    r = one(rows(batch("c", "m", 40, cost=5.0)))
    assert r["status"] == "healthy"
    r = one(rows(batch("c", "m", 40, cost=5.0), batch("p", "m", 5, cost=0.01)))
    assert r["status"] == "healthy"


def test_insufficient_data_below_30():
    r = one(rows(batch("c", "m", 10)))
    assert r["status"] == "insufficient_data" and r["requests"] == 10
    assert r["completed"]["insufficient"] is True


def test_performance_degradation():
    r = one(rows(batch("c", "m", 40, latency=200.0), batch("p", "m", 40, latency=100.0)))
    assert r["status"] == "performance_degradation"
    assert r["p95_latency_ms"] == 200.0


def _obs(model="m", **kw):
    d = dict(ts=T0, model=model, outcome="ok", latency_ms=1000.0, streamed=False,
             ttft_ms=None, completion_tokens=100)
    d.update(kw)
    return ObservationRow(**d)


def test_speed_uses_non_streamed_with_tokens():
    obs = [_obs(completion_tokens=50), _obs(completion_tokens=100), _obs(completion_tokens=200),
           _obs(streamed=True), _obs(completion_tokens=None)]
    s = one(rows(batch("c", "m", 3), obs=obs))["speed_tps"]
    assert s["n"] == 3 and s["value"] is None and s["insufficient"] is True
    assert s["scope"] == "all_applications"
    obs = [_obs(completion_tokens=50 + i % 3 * 50) for i in range(30)] + [_obs(streamed=True)]
    s = one(rows(batch("c", "m", 3), obs=obs))["speed_tps"]
    assert s["n"] == 30 and s["insufficient"] is False and s["value"] == 100.0


def test_speed_median_of_three_known_values_when_gate_met(monkeypatch):
    import app.metrics.compute as c
    monkeypatch.setattr(c, "MIN_N_SPEED", 3)
    obs = [_obs(completion_tokens=50), _obs(completion_tokens=100), _obs(completion_tokens=200),
           _obs(streamed=True), _obs(completion_tokens=None)]
    s = one(rows(batch("c", "m", 3), obs=obs))["speed_tps"]
    assert s["value"] == 100.0 and s["n"] == 3


def test_ttft_from_streamed_only():
    obs = [_obs(streamed=True, ttft_ms=100.0 + i) for i in range(10)] + [_obs(ttft_ms=5.0)]
    t = one(rows(batch("c", "m", 3), obs=obs))["ttft_ms"]
    assert t["n"] == 10 and t["insufficient"] is False
    assert 100.0 <= t["p50"] <= 109.0 and t["p95"] == 109.0
    t = one(rows(batch("c", "m", 3), obs=obs[:5]))["ttft_ms"]
    assert t["insufficient"] is True and t["p50"] is None


def test_task_credited_to_primary_model():
    # one task (trajectory t) with two requests: cheap model $1, big model $5
    cheap = UsageRow("r1", "a", T0, "cheap", "onprem", "ok", 1.0, 10, 0, None, 0.0, 0.0, 0.0)
    big = UsageRow("r2", "a", T0, "big", "cloud", "ok", 5.0, 10, 0, None, 0.0, 0.0, 0.0)
    outs = [OutcomeRow("r1", "a", T0, "ok", "cheap", None, "t", 1.0, None, []),
            OutcomeRow("r2", "a", T0, "ok", "big", None, "t", 1.0, None, [])]
    fb = [FeedbackRow("r2", T0, "task_success", 1.0)]
    res = rows(([cheap, big], outs), fb=fb)
    assert one(res, "big")["cost_per_task"]["n_tasks"] == 1
    assert one(res, "big")["cost_per_task"]["successful"] == 6.0
    assert one(res, "cheap")["cost_per_task"]["n_tasks"] == 0
    assert one(res, "cheap")["cost_per_successful_task"]["value"] is None
    assert one(res, "big")["cost_per_successful_task"]["n"] == 1
    assert [r["model"] for r in res] == ["big", "cheap"]  # spend descending


def test_cache_hit_rate_and_failovers():
    us, outs = batch("c", "m", 4, cached=50, prompt=100)
    outs[0] = OutcomeRow("c0", "a", T0, "backend_failed", "m", None, None, 1.0, "failover", [])
    r = one(rows((us, outs)))
    assert r["cache_hit_rate"] == 0.5 and r["failovers"] == 1 and r["errors"] == 1


def _pricing(**kw):
    models = {"a": PriceCfg(input=1, output=1, tier="cheap", provider="onprem",
                            as_of=date(2026, 10, 30)),
              "b": PriceCfg(input=1, output=1, tier="cheap", provider="onprem")}
    return PricingCfg(models=models, baseline_model="a", **kw)


def test_page_alerts_undated_and_stale():
    today = date(2026, 11, 4)
    p = _pricing(baseline_as_of=today - timedelta(days=40), pricing_stale_days=30)
    kinds = {a["kind"] for a in page_alerts(p, today=today, excluded_rows=0, models=[])}
    assert kinds == {"prices_undated", "prices_stale"}


def test_page_alerts_clean():
    today = date(2026, 11, 4)
    p = _pricing(baseline_as_of=today)
    p.models["b"].as_of = today
    assert page_alerts(p, today=today, excluded_rows=0, models=[]) == []


def test_savings_partial_alert():
    today = date(2026, 11, 4)
    p = _pricing(baseline_as_of=today)
    p.models["b"].as_of = today
    al = page_alerts(p, today=today, excluded_rows=7, models=[])
    assert [a["kind"] for a in al] == ["savings_partial"] and al[0]["severity"] == "info"
    assert "7" in al[0]["message"]


def test_alert_order_page_first_then_worst_model():
    today = date(2026, 11, 4)
    p = _pricing()
    models = [
        {"model": "x", "status": "cache_regression", "status_reasons": ["cache down"]},
        {"model": "y", "status": "cost_anomaly", "status_reasons": ["spend up"]},
        {"model": "z", "status": "healthy", "status_reasons": []},
        {"model": "w", "status": "insufficient_data", "status_reasons": []},
    ]
    al = page_alerts(p, today=today, excluded_rows=1, models=models)
    kinds = [(a["kind"], a["model"]) for a in al]
    assert kinds == [("prices_undated", None), ("savings_partial", None),
                     ("cost_anomaly", "y"), ("cache_regression", "x")]
