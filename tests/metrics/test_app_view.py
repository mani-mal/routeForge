from datetime import datetime, timedelta, timezone

import pytest

from app.metrics.compute import FeedbackRow, OutcomeRow, UsageRow, app_view

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)   # 14.5 elapsed days, 30-day month
T0 = datetime(2026, 11, 10, tzinfo=timezone.utc)


def usage(rid, cost=1.0, model="m", app="a", ts=T0):
    return UsageRow(rid, app, ts, model, "onprem", "ok", cost, 100, None, None, None, None, None)


def outcome(rid, status="ok", workload=None, traj=None, reason=None, chosen="m",
            attempts=(), ts=T0, latency=10.0):
    return OutcomeRow(rid, "a", ts, status, chosen, workload, traj, latency, reason,
                      list(attempts))


def view(usage_rows=(), outcomes=(), feedback=(), month=(), budget=None, now=NOW):
    return app_view(list(usage_rows), list(outcomes), list(feedback),
                    month_usage=list(month), monthly_budget_usd=budget, now=now)


def test_forecast_mid_month():
    b = view(month=[usage("r", 15.0)], budget=100.0)["budget"]
    assert b["month_to_date_usd"] == 15.0
    assert b["forecast_usd"] == pytest.approx(15.0 / 14.5 * 30)
    assert b["month"] == "2026-11" and b["monthly_budget_usd"] == 100.0


def test_forecast_first_day_uses_one_day():
    now = datetime(2026, 11, 1, 0, 30, tzinfo=timezone.utc)   # 0.02 days elapsed
    b = view(month=[usage("r", 2.0, ts=now)], budget=100.0, now=now)["budget"]
    assert b["forecast_usd"] == pytest.approx(2.0 * 30)       # not 2 / 0.02 * 30


def test_forecast_uses_days_in_month():
    now = datetime(2028, 2, 15, 12, tzinfo=timezone.utc)      # leap February
    b = view(month=[usage("r", 14.5)], now=now)["budget"]
    assert b["forecast_usd"] == pytest.approx(14.5 / 14.5 * 29)


@pytest.mark.parametrize("forecast_pct,risk", [(0.89, "low"), (0.90, "watch"), (1.0, "watch"),
                                                (1.10, "watch"), (1.11, "high")])
def test_risk_bands(forecast_pct, risk):
    # budget 100, elapsed 14.5 of 30 days: MTD that forecasts forecast_pct * 100
    mtd = forecast_pct * 100 * 14.5 / 30
    assert view(month=[usage("r", mtd)], budget=100.0)["budget"]["risk"] == risk


def test_no_budget_risk_null():
    b = view(month=[usage("r", 500.0)])["budget"]
    assert b["risk"] is None and b["monthly_budget_usd"] is None
    assert b["forecast_usd"] > 0


def test_no_traffic():
    v = view(budget=150.0)
    assert v["budget"]["month_to_date_usd"] == 0.0
    assert v["budget"]["forecast_usd"] == 0.0 and v["budget"]["risk"] == "low"
    assert v["spend_by_workload"] == [] and v["models_used"] == [] and v["route_reasons"] == []
    assert v["retries_and_failovers_usd"] == 0.0
    assert v["successful_reported"] is None
    assert v["completed"]["insufficient"] is True
    assert v["p95_task_time_ms"]["insufficient"] is True
    assert v["cost_per_task"]["attempted"] is None


def test_spend_by_workload_and_untagged():
    us = [usage("r1", 1.0), usage("r2", 5.0), usage("r3", 2.0), usage("r4", 0.5)]
    os = [outcome("r1", workload="small"), outcome("r2", workload="big"),
          outcome("r3", workload=None)]            # r4 has no outcome row: untagged too
    got = view(us, os)["spend_by_workload"]
    assert got == [{"workload": "big", "usd": 5.0}, {"workload": None, "usd": 2.5},
                   {"workload": "small", "usd": 1.0}]


def test_failover_counted_in_models_and_retries():
    us = [usage("r1", 0.3, model="cheap"), usage("r1", 0.7, model="strong"), usage("r2", 1.0)]
    os = [outcome("r1", chosen="strong", attempts=["cheap", "strong"], reason="failover"),
          outcome("r2", chosen="m", reason="length_tier")]
    v = view(us, os)
    assert v["retries_and_failovers_usd"] == pytest.approx(1.0)       # both attempts of r1
    assert {m["model"]: m["requests"] for m in v["models_used"]} == {"strong": 1, "m": 1}
    assert sum(m["requests"] for m in v["models_used"]) == 2           # r1 counted once
    assert v["spend_by_workload"] == [{"workload": None, "usd": pytest.approx(2.0)}]


def test_escalation_counts_as_retry_spend():
    v = view([usage("r1", 2.0), usage("r2", 1.0)],
             [outcome("r1", reason="escalation"), outcome("r2", reason="risk_floor")])
    assert v["retries_and_failovers_usd"] == pytest.approx(2.0)


def test_route_reason_shares():
    os = ([outcome(f"a{i}", reason="length_tier") for i in range(3)]
          + [outcome("b", reason="failover")] + [outcome("c", reason=None)])
    got = view([], os)["route_reasons"]
    assert got == [{"reason": "length_tier", "count": 3, "share": 0.75},
                   {"reason": "failover", "count": 1, "share": 0.25}]
    assert view([], os)["models_used"][0]["share"] == 1.0


def test_unrouted_outcomes_are_not_models():
    v = view([], [outcome("r1", "no_eligible", chosen=None), outcome("r2")])
    assert v["models_used"] == [{"model": "m", "requests": 1, "share": 1.0}]


def test_per_request_unit_uses_request_latency():
    os = [outcome(f"r{i}", latency=float(i + 1), ts=T0 + timedelta(hours=i)) for i in range(40)]
    v = view([usage(o.request_id) for o in os], os)
    assert v["cost_per_task"]["task_unit"] == "request"
    p = v["p95_task_time_ms"]
    assert p["task_unit"] == "request" and p["n"] == 40 and p["insufficient"] is False
    assert p["value"] == 38.0       # nearest-rank p95 of 1..40, not hours-wide spans


def test_p95_task_time():
    os = []
    for t in range(20):                       # 20 tasks of 2 requests, task t spans t+1 s
        start = T0 + timedelta(days=t)
        os.append(outcome(f"t{t}a", traj=f"k{t}", ts=start, latency=100.0))
        os.append(outcome(f"t{t}b", traj=f"k{t}", ts=start + timedelta(seconds=t + 1),
                          latency=250.0))
    os.append(outcome("fail", "backend_failed", traj="kf", chosen=None, attempts=["m"]))
    p = view([], os)["p95_task_time_ms"]
    # ts is request END time: duration = span + FIRST request's latency (100), not the last's (250);
    # kf has no ok request and is excluded
    assert p["n"] == 20 and p["task_unit"] == "task" and p["insufficient"] is False
    assert p["value"] == 1000 * 19 + 100     # idx round(0.95*19)=18 -> t=18 -> 19000+100
    one = view([], os[:2])["p95_task_time_ms"]
    assert one["n"] == 1 and one["insufficient"] is True and one["value"] is None


def test_task_metrics_follow_slice_two():
    os = [outcome(f"r{i}", traj=f"k{i}") for i in range(20)]
    fb = [FeedbackRow(f"r{i}", T0, "task_success", 1 if i < 15 else 0) for i in range(20)]
    v = view([usage(f"r{i}", 1.0) for i in range(20)], os, fb)
    assert v["cost_per_task"]["successful"] == pytest.approx(20 / 15)
    assert v["successful_reported"]["value"] == 0.75 and v["successful_reported"]["n"] == 20


def test_no_failover_is_not_a_route_reason_share():
    os = [outcome("a", reason="failover"),
          outcome("b", status="backend_failed", reason="no_failover", chosen=None,
                  attempts=("m",))]
    assert [r["reason"] for r in view([], os)["route_reasons"]] == ["failover"]
