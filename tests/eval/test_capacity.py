"""P3c-4: the capacity engine (spec §3-§7; invariants C2-C8, C10, C14-C16,
C18-C20).

Every demand figure here comes from rows replayed through the real
`simulate()` -- the real `feasible_set` and `Router` -- never from a guess
at where a fixture routes. The exception is said where it is made:
`synthetic()` builds rows by hand for the tests that are about arithmetic on
a known arrival process (C18, C19). Shared helpers for the other P3c-4 test
modules live at the top.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import math
import os
import random
import statistics
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from app.eval.capacity import (DEMANDS, MAX_SERVICE_MS, METHOD, NOT_MODELLED, REQUESTS, SCHEMA,
                               SERVICE_MS, CapacityError, CapacityPlan, arm_load,
                               arrival_bucket, bucket_start, build_plan, capacity_block,
                               endpoint_means, load_capacity, plan_inputs, recorded_ms,
                               service_time, sets_volume, states_capacity, volume_for,
                               written_ms)
from app.eval.proposal import CAPACITY_LIMITS, assumptions_block, normalized_edits
from app.eval.simulate import ArmResult, RowResult, simulate, summarize
from tests.eval.sim_fixtures import (BASE_SETTINGS, app_entry, make_row, make_snapshot,
                                     onboarded_row)
from tests.eval.test_uncertainty import BANNED

P = "routeforge.policy-proposal/v1"
T0 = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
#: Requests arriving in each successive minute: 20 quiet minutes, 10 busy ones.
QUIET_THEN_BUSY = [2] * 20 + [12] * 10
#: One endpoint's four limits, chosen so every utilisation is a round number.
LIMITS = {"local-cheap": {"max_concurrency": 1.0, "requests_per_minute": 10.0,
                          "tokens_per_minute": 10000.0, "output_tokens_per_minute": 500.0}}
#: Words a capacity output may not use, on top of the ranking vocabulary.
CAPACITY_BANNED = BANNED + ("should", "headroom", "relax", "gpu", "buy", "upgrade")


def traffic(pattern, *, app: str = "app-a", latency_ms: float = 6000.0, prompt: int = 900,
            completion: int = 100, start: datetime = T0, **over) -> list[dict]:
    """`pattern[m]` requests ARRIVING in minute `m` after `start`, at seconds
    5, 9, 13, ... Each row's `ts` is its arrival plus `latency_ms`, because
    an outcome row is written when the request ends."""
    rows = []
    for minute, count in enumerate(pattern):
        for j in range(count):
            arrival = start + timedelta(minutes=minute, seconds=5 + 4 * j)
            written = arrival + timedelta(milliseconds=latency_ms)
            rows.append(make_row(f"{app}-{start:%d%H}-{minute:04d}-{j:02d}", app,
                                 ts=written.isoformat(), latency_ms=latency_ms,
                                 prompt_tokens=prompt, completion_tokens=completion, **over))
    return rows


def one_endpoint() -> dict:
    """One on-prem endpoint and two apps: every routed row lands on it."""
    return make_snapshot(onboarded=[onboarded_row("local-cheap", "cheap")],
                         apps=[app_entry("app-a"), app_entry("app-b")])


def three_endpoints() -> dict:
    """Three tiers; `app-b` may use the mid endpoint only."""
    return make_snapshot(apps=[app_entry("app-a"),
                               app_entry("app-b", allowed_models=["local-mid"])])


def mixed_traffic() -> list[dict]:
    """Two apps over 15 minutes, three request sizes, so the real router
    spreads `app-a` over more than one endpoint."""
    rows = []
    for i, (complexity, prompt) in enumerate(((0.05, 200), (0.6, 1200), (1.0, 3000))):
        rows += traffic([1 + i] * 15, app="app-a", latency_ms=2000.0 + 1000 * i, prompt=prompt,
                        completion=50 * (i + 1), complexity=complexity,
                        start=T0 + timedelta(hours=i))
    rows += traffic([2] * 15, app="app-b", latency_ms=4000.0, prompt=700, completion=70,
                    complexity=0.6, start=T0 + timedelta(hours=3))
    return rows


def replay(rows: list[dict], snapshot: dict, body: dict | None = None):
    """`(results, excluded)` from the real simulator for a proposal body."""
    return asyncio.run(simulate(rows, snapshot, {"schema": P, **(body or {})},
                                base=BASE_SETTINGS))


def block_for(rows, snapshot, body=None, **plan_kw) -> dict:
    results, excluded = replay(rows, snapshot, body)
    return capacity_block(results, CapacityPlan(**plan_kw), body, excluded=excluded)


def keys_of(node) -> list[str]:
    if isinstance(node, dict):
        return [str(k) for k in node] + [k for v in node.values() for k in keys_of(v)]
    if isinstance(node, list):
        return [k for v in node for k in keys_of(v)]
    return []


_DROP_MID = {"endpoints": {"remove": ["local-mid"]}}


# --- the capacity file and the plan (C15) -----------------------------------------

def _write(tmp_path, doc) -> str:
    path = tmp_path / "capacity.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def test_a_capacity_file_loads_and_is_hashed(tmp_path):
    doc = {"schema": SCHEMA, "title": "lab", "endpoints": LIMITS,
           "workload": {"volume_scale": 10, "apps": {"app-b": {"volume_scale": 3}}}}
    loaded, sha = load_capacity(_write(tmp_path, doc))
    assert loaded == doc and len(sha) == 64
    plan = build_plan(loaded, sha, one_endpoint(), bucket_seconds=30, min_buckets=4)
    assert plan.limits == LIMITS
    assert plan.volume == {"*": 10.0, "app-b": 3.0}
    assert plan_inputs(plan) == {"file_sha256": sha, "bucket_seconds": 30, "min_buckets": 4}


@pytest.mark.parametrize("doc,where", [
    ({"schema": "routeforge.capacity-assumptions/v2"}, "schema"),
    ({"endpoints": {}}, "schema"),
    ({"schema": SCHEMA, "endpoint": {}}, "endpoint"),
    ({"schema": SCHEMA, "title": 3}, "title"),
    ({"schema": SCHEMA, "endpoints": []}, "endpoints"),
    ({"schema": SCHEMA, "endpoints": {"local-cheap": {"max_concurency": 8}}},
     "endpoints.local-cheap.max_concurency"),
    ({"schema": SCHEMA, "endpoints": {"local-cheap": {"max_concurrency": 0}}},
     "endpoints.local-cheap.max_concurrency"),
    ({"schema": SCHEMA, "endpoints": {"local-cheap": {"tokens_per_minute": -1}}},
     "endpoints.local-cheap.tokens_per_minute"),
    ({"schema": SCHEMA, "endpoints": {"local-cheap": {"requests_per_minute": True}}},
     "endpoints.local-cheap.requests_per_minute"),
    ({"schema": SCHEMA, "endpoints": {"local-cheap": {"requests_per_minute": "600"}}},
     "endpoints.local-cheap.requests_per_minute"),
    ({"schema": SCHEMA, "endpoints": {"local-cheap": {"max_concurrency": float("inf")}}},
     "endpoints.local-cheap.max_concurrency"),
    ({"schema": SCHEMA, "endpoints": {"local-cheap": 8}}, "endpoints.local-cheap"),
    ({"schema": SCHEMA, "workload": {"volume_scale": 0}}, "workload.volume_scale"),
    ({"schema": SCHEMA, "workload": {"volume_scale": 1e7}}, "workload.volume_scale"),
    ({"schema": SCHEMA, "workload": {"token_scale": 2}}, "workload.token_scale"),
    ({"schema": SCHEMA, "workload": {"apps": {"app-a": {"volume_scale": "2"}}}},
     "workload.apps.app-a.volume_scale"),
])
def test_c15_a_bad_capacity_file_is_refused_naming_the_location(tmp_path, doc, where):  # C15
    with pytest.raises(CapacityError) as err:
        load_capacity(_write(tmp_path, doc))
    assert err.value.where == where


def test_c15_a_file_that_is_not_json_is_refused(tmp_path):                              # C15
    path = tmp_path / "capacity.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CapacityError, match="invalid JSON"):
        load_capacity(str(path))


def test_c15_a_slug_or_app_the_snapshot_does_not_hold_is_refused():                    # C15
    """A typo in a slug must never read as "no limit stated"."""
    snapshot = one_endpoint()
    with pytest.raises(CapacityError) as err:
        build_plan({"schema": SCHEMA, "endpoints": {"local-chep": {"max_concurrency": 8}}},
                   "0" * 64, snapshot)
    assert err.value.where == "endpoints.local-chep" and "not an onboarded" in err.value.message
    for app_id, message in (("nobody", "unknown application"), ("*", "reserved")):
        with pytest.raises(CapacityError, match=message) as err:
            build_plan({"schema": SCHEMA,
                        "workload": {"apps": {app_id: {"volume_scale": 2}}}}, None, snapshot)
        assert err.value.where == f"workload.apps.{app_id}"


def test_a_disabled_endpoint_and_a_priced_pool_model_may_carry_a_limit():
    snapshot = make_snapshot(onboarded=[onboarded_row("local-cheap", "cheap", enabled=False)])
    pool_model = sorted(snapshot["settings"]["pricing"]["models"])[0]
    plan = build_plan({"schema": SCHEMA, "endpoints": {
        "local-cheap": {"max_concurrency": 4}, pool_model: {"tokens_per_minute": 90000}}},
        None, snapshot)
    assert plan.limits == {"local-cheap": {"max_concurrency": 4.0},
                           pool_model: {"tokens_per_minute": 90000.0}}


def test_no_file_is_a_plan_with_nothing_stated():
    plan = build_plan(None, None, one_endpoint())
    assert plan == CapacityPlan() and plan.limits == {} and plan.volume == {}
    assert plan_inputs(plan) == {"file_sha256": None, "bucket_seconds": 60, "min_buckets": 10}


@pytest.mark.parametrize("kw,flag", [
    ({"bucket_seconds": 0}, "--capacity-bucket"), ({"bucket_seconds": 86401}, "--capacity-bucket"),
    ({"bucket_seconds": True}, "--capacity-bucket"), ({"bucket_seconds": 1.5}, "--capacity-bucket"),
    ({"min_buckets": 0}, "--capacity-min-buckets"), ({"min_buckets": -3}, "--capacity-min-buckets"),
])
def test_c15_a_plan_value_out_of_range_is_refused(kw, flag):                           # C15
    with pytest.raises(CapacityError) as err:
        CapacityPlan(**kw)
    assert err.value.where == flag
    assert CapacityPlan(bucket_seconds=86400, min_buckets=1).bucket_seconds == 86400


def test_what_a_proposal_states():
    assert not states_capacity(None) and not states_capacity({"capacity": None})
    assert not states_capacity({"capacity": {}}) and not states_capacity({"capacity": {"x": {}}})
    assert states_capacity({"capacity": {"x": {"max_concurrency": 2}}})
    assert not sets_volume({"workload": {"token_scale": 0.5}})
    assert sets_volume({"workload": {"apps": {"app-a": {"volume_scale": 2}}}})


# --- arrival time and buckets (C16) -------------------------------------------------

def test_c16_a_row_is_placed_by_its_estimated_arrival():                               # C16
    """`ts` is when the row was written -- the end of the request. A request
    written at 12:01:02 after 5 s arrived at 12:00:57, in the 12:00 bucket."""
    row = make_row("r", "app-a", ts="2026-09-20T12:01:02+00:00", latency_ms=5000.0)
    assert bucket_start(arrival_bucket(row, 60), 60) == "2026-09-20T12:00:00+00:00"
    on_the_edge = make_row("e", "app-a", ts="2026-09-20T12:01:06+00:00", latency_ms=6000.0)
    assert bucket_start(arrival_bucket(on_the_edge, 60), 60) == "2026-09-20T12:01:00+00:00"
    assert bucket_start(arrival_bucket(row, 3600), 3600) == "2026-09-20T12:00:00+00:00"


@pytest.mark.parametrize("latency", [None, -5.0, float("nan"), float("inf"), "6000", True,
                                     86_400_001.0, 1e12, 1e15, 1e18, 1e300])
def test_c16_an_unusable_latency_subtracts_nothing(latency):                           # C16
    """Missing, negative, non-finite, not a number -- or above 24 hours: a
    latency of 1e12 ms would place the row in 1995, and 1e15 ms raised."""
    row = make_row("r", "app-a", ts="2026-09-20T12:01:02+00:00", latency_ms=latency)
    assert recorded_ms(row) is None
    assert bucket_start(arrival_bucket(row, 60), 60) == "2026-09-20T12:01:00+00:00"


def test_c16_a_latency_of_exactly_24_hours_is_the_longest_usable_one():                # C16
    row = make_row("r", "app-a", ts="2026-09-20T12:01:02+00:00", latency_ms=86_400_000)
    assert recorded_ms(row) == MAX_SERVICE_MS == 86_400_000
    assert bucket_start(arrival_bucket(row, 60), 60) == "2026-09-19T12:01:00+00:00"


def test_c16_buckets_are_epoch_aligned_whatever_the_offset():                          # C16
    """The same instant written with three offsets is one bucket; a naive
    timestamp is read as UTC, as `--since` reads it."""
    same = ["2026-09-20T12:00:30+00:00", "2026-09-20T14:00:30+02:00", "2026-09-20T12:00:30Z",
            "2026-09-20T12:00:30", "2026-09-20T07:00:30-05:00"]
    buckets = {arrival_bucket(make_row("r", "app-a", ts=ts, latency_ms=0.0), 60) for ts in same}
    assert len(buckets) == 1
    assert bucket_start(buckets.pop(), 60) == "2026-09-20T12:00:00+00:00"
    assert arrival_bucket(make_row("r", "app-a", ts=None), 60) is None


def test_c16_a_timestamp_is_floored_to_the_millisecond_in_exact_integers():            # C16
    """A row written 400 microseconds before a minute boundary is in the
    minute it was written in. Rounding a float of seconds to the nearest
    millisecond put it in the next one."""
    late = make_row("r", "app-a", ts="2026-09-20T12:00:59.999600+00:00", latency_ms=0.0)
    assert written_ms(late) % 60_000 == 59_999
    assert bucket_start(arrival_bucket(late, 60), 60) == "2026-09-20T12:00:00+00:00"
    assert bucket_start(arrival_bucket(late, 1), 1) == "2026-09-20T12:00:59+00:00"
    # Against exact integer arithmetic on microseconds, over many instants.
    rng = random.Random(1)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    for _ in range(2000):
        instant = T0 + timedelta(microseconds=rng.randrange(10 ** 13))
        micros = (instant - epoch) // timedelta(microseconds=1)
        assert written_ms({"ts": instant.isoformat()}) == micros // 1000


def test_c16_rows_without_a_timestamp_are_counted_and_in_no_bucket():                  # C16
    rows = traffic([2] * 12) + [make_row(f"no-ts-{i}", "app-a", ts=None) for i in range(3)]
    block = block_for(rows, one_endpoint(), limits=LIMITS)
    assert block["rows"] == {"simulated": 27, "without_ts": 3, "unusable_latency": 0,
                             "excluded_not_counted": 0}
    assert block["endpoints"]["local-cheap"]["current"]["requests"] == 24.0


def test_rows_with_an_unusable_latency_are_counted_and_placed_at_ts():
    """An absurd recorded latency is not a service time: the row is placed
    where it was written, counted, and given the endpoint's mean service
    time instead of its own."""
    rows = traffic([2] * 12)
    rows[0]["latency_ms"] = 1e12
    rows[1]["latency_ms"] = None
    block = block_for(rows, one_endpoint())
    assert block["rows"]["unusable_latency"] == 2
    assert block["window"]["first_bucket"] == "2026-09-20T12:00:00+00:00"
    arm = block["endpoints"]["local-cheap"]["current"]
    assert arm["service_time_sources"] == {"recorded": 22, "endpoint_mean": 2, "replay": 0,
                                           "asserted": 0}
    assert arm["service_time_ms"] == {"mean": 6000.0, "max": 6000}


def test_identical_timestamps_are_one_bucket():
    rows = [make_row(f"same-{i}", "app-a", ts="2026-09-20T12:00:30+00:00", latency_ms=1000.0)
            for i in range(50)]
    block = block_for(rows, one_endpoint(), limits=LIMITS, min_buckets=1)
    assert block["window"] == {"first_bucket": "2026-09-20T12:00:00+00:00",
                               "last_bucket": "2026-09-20T12:00:00+00:00",
                               "buckets": 1, "arrival_buckets": 1}
    stated = block["endpoints"]["local-cheap"]["current"]["limits"]["requests_per_minute"]
    assert stated["peak"] == stated["mean_window"] == stated["mean_active"] == 5.0   # 50 against 10


# --- C14: patterns whose utilisation can be computed by hand ------------------------

def test_c14_utilisation_matches_the_closed_form():                                    # C14
    """20 minutes at 2 requests a minute, then 10 at 12. Every request takes
    6 s, arrives no later than second 49 and so ends inside its own minute,
    and is 900 prompt + 100 completion tokens. By hand, for the limits in
    `LIMITS` (1 concurrent, 10 req/min, 10,000 tok/min, 500 out tok/min):

        quiet minute: 2 x 6 s / 60 s = 0.2 in service       -> 20%
                      2 req/min                             -> 20%
                      2 x 1000 = 2,000 tok/min              -> 20%
                      2 x 100 = 200 out tok/min             -> 40%
        busy minute:  12 x 6 / 60 = 1.2 in service         -> 120%
                      12 req/min -> 120%; 12,000 tok/min -> 120%; 1,200 out -> 240%
        window:       30 minutes, every one with an arrival, so the two
                      means agree: (20 x 2 + 10 x 12) / 30 = 5.333 req/min
                      -> 53.33% (concurrency, requests, tokens), 106.67% (output)
        over 100%:    the 10 busy minutes of 30, holding 120 of 160 requests (75%);
                      for output tokens the same 10 (a quiet minute is at 40%)."""
    block = block_for(traffic(QUIET_THEN_BUSY), one_endpoint(), limits=LIMITS)
    assert block["window"] == {"first_bucket": "2026-09-20T12:00:00+00:00",
                               "last_bucket": "2026-09-20T12:29:00+00:00",
                               "buckets": 30, "arrival_buckets": 30}
    arm = block["endpoints"]["local-cheap"]["current"]
    assert arm["requests"] == 160.0 and arm["active_buckets"] == 30
    assert arm["service_time_sources"] == {"recorded": 160, "endpoint_mean": 0, "replay": 0,
                                           "asserted": 0}
    assert arm["service_time_ms"] == {"mean": 6000.0, "max": 6000}
    assert (arm["replay_share"], arm["replay_source"], arm["service_ms_after_window"]) == (
        0.0, None, 0.0)
    for name, peak, mean in (("concurrency", 1.2, 160 * 6 / 1800),
                             ("requests_per_minute", 12.0, 160 / 30),
                             ("tokens_per_minute", 12000.0, 160000 / 30),
                             ("output_tokens_per_minute", 1200.0, 16000 / 30)):
        assert arm["demand"][name] == pytest.approx(
            {"peak": peak, "mean_window": mean, "mean_active": mean}), name
    for limit, peak, mean in (("max_concurrency", 1.2, 0.5333333),
                              ("requests_per_minute", 1.2, 0.5333333),
                              ("tokens_per_minute", 1.2, 0.5333333),
                              ("output_tokens_per_minute", 2.4, 1.0666667)):
        stated = arm["limits"][limit]
        assert stated["peak"] == pytest.approx(peak), limit
        assert stated["mean_window"] == pytest.approx(mean), limit
        assert stated["mean_active"] == pytest.approx(mean), limit
        assert stated["peak_bucket"] == "2026-09-20T12:20:00+00:00", limit
        assert (stated["buckets_over"], stated["buckets_over_share"]) == (10, pytest.approx(1 / 3))
        assert stated["requests_over_share"] == pytest.approx(0.75), limit
    assert block["peak"]["current"] == {
        "endpoint": "local-cheap", "limit": "output_tokens_per_minute",
        "utilisation": pytest.approx(2.4), "bucket": "2026-09-20T12:20:00+00:00",
        "mean_window": pytest.approx(1.0666667), "buckets_over_share": pytest.approx(1 / 3),
        "bucket_seconds": 60, "buckets": 30, "service_after_window_share": 0.0}
    assert block["endpoints"]["local-cheap"]["proposed"] == arm          # nothing proposed


def test_c14_a_request_that_crosses_buckets_is_spread_over_them():                     # C14
    """The spread rule, by hand. One request a minute for 12 minutes, each
    arriving at second 30 and taking 90 s, so each is in service for 30 s of
    its own minute and the whole of the next:

        minute 0:       30 s  (its own arrival)            -> 0.5 in service
        minutes 1..11:  60 s of the one before + 30 s      -> 1.5
        minute 12:      60 s of the last one, no arrival   -> 1.0
        window:         13 minutes; 12 hold an arrival; the endpoint is
                        active in all 13
        time-average:   12 x 90 s / (13 x 60 s) = 1.3846 in service
        requests:       1 a minute in 12 minutes of 13     -> mean 0.9231

    Against `max_concurrency: 1` the 11 middle minutes are over (11 of 13),
    and they hold 11 of the 12 arrivals. Counting each request's 90 s in its
    arrival minute alone gave 1.5 in every minute and nothing in the 13th."""
    rows = [make_row(f"long-{m}", "app-a", latency_ms=90_000.0,
                     ts=(T0 + timedelta(minutes=m, seconds=120)).isoformat())
            for m in range(12)]
    block = block_for(rows, one_endpoint(), limits={"local-cheap": {"max_concurrency": 1.0}})
    assert block["window"] == {"first_bucket": "2026-09-20T12:00:00+00:00",
                               "last_bucket": "2026-09-20T12:12:00+00:00",
                               "buckets": 13, "arrival_buckets": 12}
    arm = block["endpoints"]["local-cheap"]["current"]
    load = arm_load(replay(rows, one_endpoint())[0], "current", bucket_seconds=60)["local-cheap"]
    first = min(load.cells)
    assert [load.cells[first + m]["app-a"][SERVICE_MS] for m in range(13)] == (
        [30_000] + [90_000] * 11 + [60_000])
    assert [load.cells[first + m]["app-a"][REQUESTS] for m in range(13)] == [1] * 12 + [0]
    assert arm["active_buckets"] == 13 and arm["service_time_ms"] == {"mean": 90000.0,
                                                                       "max": 90000}
    assert arm["demand"]["concurrency"] == pytest.approx(
        {"peak": 1.5, "mean_window": 12 * 90 / (13 * 60), "mean_active": 12 * 90 / (13 * 60)})
    assert arm["demand"]["requests_per_minute"] == pytest.approx(
        {"peak": 1.0, "mean_window": 12 / 13, "mean_active": 12 / 13})
    stated = arm["limits"]["max_concurrency"]
    assert stated["peak_bucket"] == "2026-09-20T12:01:00+00:00"
    assert (stated["buckets_over"], stated["buckets_over_share"]) == (11, pytest.approx(11 / 13))
    assert stated["requests_over_share"] == pytest.approx(11 / 12)
    assert arm["service_ms_after_window"] == 0.0


def test_c14_exactly_at_the_limit_is_not_over_it():                                    # C14
    """At 5x the volume a quiet minute is 10 req/min against a limit of 10:
    100%, which is not above 100%."""
    body = {"workload": {"volume_scale": 5}}
    proposed = block_for(traffic(QUIET_THEN_BUSY), one_endpoint(), body,
                         limits=LIMITS)["endpoints"]["local-cheap"]["proposed"]
    for limit in ("max_concurrency", "requests_per_minute", "tokens_per_minute"):
        assert proposed["limits"][limit]["peak"] == pytest.approx(6.0)
        assert proposed["limits"][limit]["buckets_over"] == 10, limit
    assert proposed["limits"]["output_tokens_per_minute"]["buckets_over"] == 30


# --- C3, C7: conservation; a rejected row is no demand ------------------------------

def _totals(loads: dict) -> list[int]:
    """Exact totals over endpoints, buckets and applications; service time
    includes what fell after the window."""
    totals = [sum(cell[i] for load in loads.values() for per_app in load.cells.values()
                  for cell in per_app.values()) for i in range(4)]
    totals[SERVICE_MS] += sum(ms for load in loads.values() for ms in load.after_window.values())
    return totals


@pytest.mark.parametrize("seconds", [1, 60, 3600])
def test_c3_requests_tokens_and_service_time_are_conserved(seconds):                   # C3
    """Every routed row is counted once, its tokens with it, and every
    millisecond of its service time in exactly one bucket -- exactly, in
    integers, whatever the bucket length."""
    results, _ = replay(mixed_traffic(), three_endpoints(), _DROP_MID)
    assert {r.proposed.outcome for r in results} == {"routed", "rejected"}   # not vacuous
    means = endpoint_means(results)
    for arm in ("current", "proposed"):
        routed = [r for r in results if getattr(r, arm).outcome == "routed"]
        picks = [getattr(r, arm) for r in routed]
        loads = arm_load(results, arm, bucket_seconds=seconds)
        requests, tokens, output, service = _totals(loads)
        assert requests == len(routed)
        assert tokens == sum(a.prompt_tokens + a.completion_tokens for a in picks)
        assert output == sum(a.completion_tokens for a in picks)
        assert service == sum(service_time(r, arm, means)[0] for r in routed) > 0
        assert service == sum(load.service_ms for load in loads.values())
        assert {slug: load.rows for slug, load in loads.items()} == dict(
            Counter(a.model for a in picks))
        assert sum(sum(load.sources.values()) for load in loads.values()) == len(routed)


def test_c3_service_time_after_the_window_is_kept_and_reported():                      # C3
    """An asserted 10-minute latency runs past the window's last bucket.
    What falls after it is in no bucket, and is reported, so the two add up
    to every row's service time."""
    body = {"endpoints": {"update": {"local-cheap": {"latency_ms": 600_000}}}}
    results, _ = replay(traffic([2] * 12), one_endpoint(), body)
    block = capacity_block(results, CapacityPlan(), body)
    assert block["window"]["buckets"] == 12
    proposed = block["endpoints"]["local-cheap"]["proposed"]
    in_window = proposed["demand"]["concurrency"]["mean_window"] * 12 * 60_000
    assert proposed["service_ms_after_window"] > 0
    assert in_window + proposed["service_ms_after_window"] == pytest.approx(24 * 600_000)
    assert block["endpoints"]["local-cheap"]["current"]["service_ms_after_window"] == 0.0
    # No bucket is ever fuller than the requests in service in it.
    assert proposed["demand"]["concurrency"]["peak"] <= 24


#: 30 minutes, one request every 10 s, each recorded at 6 s on `e`.
_STEADY_SIX_SECONDS = [(10 * i + 1, 6.0) for i in range(180)]


@pytest.mark.parametrize("seconds,share", [(6, 0.0), (60, 156 / 10_800), (600, 17_760 / 108_000),
                                           (1800, 161_280 / 324_000),
                                           (7200, 1_133_280 / 1_296_000)])
def test_service_time_after_the_window_is_a_share_of_the_arms_service_time(seconds, share):
    """The window ends where the last RECORDED request ended, so an asserted
    service time longer than the recorded one runs past it, and that part is
    in no bucket: the proposed arm's average (and, before the load has built
    up, its peak) is understated. The block says by how much, as a share.

    By hand: request i (0..179) arrives at 10i + 1 s and, at S seconds,
    would end at 10i + 1 + S; the window ends at 1800 s.

        S = 60:    requests 174..179 run 1, 11, ..., 51 s past it
                   = 156 s of 180 x 60 = 10,800 s              ->  1.44%
        S = 600:   requests 120..179 run 10i - 1199 s past it
                   = 17,760 s of 108,000 s                     -> 16.44%
        S = 1800:  every request runs 10i + 1 s past it
                   = 161,280 s of 324,000 s                    -> 49.78%
        S = 7200:  every request runs 10i + 5401 s past it
                   = 1,133,280 s of 1,296,000 s                -> 87.44%

    Beyond the window's length the figures stop responding to the service
    time at all."""
    rows = synthetic(_STEADY_SIX_SECONDS, proposed=arm_on("e", seconds * 1000.0, "proposal"))
    block = capacity_block(rows, CapacityPlan(limits={"e": {"max_concurrency": 4.0}}))
    assert block["window"]["buckets"] == 30
    current, proposed = block["endpoints"]["e"]["current"], block["endpoints"]["e"]["proposed"]
    assert (current["service_after_window_share"], current["service_ms_after_window"]) == (
        0.0, 0.0)
    assert proposed["service_after_window_share"] == pytest.approx(share, rel=1e-12)
    in_window = proposed["demand"]["concurrency"]["mean_window"] * 30 * 60_000
    total = 180 * seconds * 1000
    assert in_window + proposed["service_ms_after_window"] == pytest.approx(total)
    assert proposed["service_after_window_share"] == pytest.approx(
        proposed["service_ms_after_window"] / total)
    if seconds == 600:
        assert proposed["service_ms_after_window"] == 17_760_000.0
    # The headline carries its own endpoint's share.
    assert block["peak"]["proposed"]["service_after_window_share"] == (
        proposed["service_after_window_share"])
    assert block["peak"]["current"]["service_after_window_share"] == 0.0


def test_an_endpoint_with_no_service_time_has_no_share_after_the_window():
    """No row, or only a tier default: there is no service time to take a
    share of."""
    rows = synthetic(_STEADY_SIX_SECONDS, proposed=arm_on("new", 200.0, "tier_default"))
    block = capacity_block(rows, CapacityPlan(limits={"e": {"max_concurrency": 4.0}}))
    assert block["endpoints"]["e"]["proposed"]["service_after_window_share"] is None
    assert block["endpoints"]["new"]["proposed"]["service_after_window_share"] is None
    assert block["endpoints"]["new"]["current"]["service_after_window_share"] is None


def test_c7_a_rejected_row_adds_no_demand():                                           # C7
    results, _ = replay(mixed_traffic(), three_endpoints(), _DROP_MID)
    rejected = [r for r in results if r.proposed.outcome == "rejected"]
    assert len(rejected) == 30 and {r.row["application_id"] for r in rejected} == {"app-b"}
    block = capacity_block(results, CapacityPlan(), _DROP_MID)
    served = sum(entry["proposed"]["requests"] for entry in block["endpoints"].values())
    assert served == len(results) - len(rejected)
    # The buckets the rejected rows arrived in still count as traffic: the
    # export had requests then, whichever arm served them.
    assert block["window"]["arrival_buckets"] == 60


# --- C4: linear in volume, inversely linear in a limit --------------------------------

FIGURES = ("peak", "mean_window", "mean_active")


@pytest.mark.parametrize("scale", [0.25, 0.7, 2, 10, 1000])
def test_c4_utilisation_is_linear_in_volume_scale(scale):                              # C4
    rows, snapshot = traffic(QUIET_THEN_BUSY), one_endpoint()
    entry = block_for(rows, snapshot, {"workload": {"volume_scale": scale}},
                      limits=LIMITS)["endpoints"]["local-cheap"]
    current, proposed = entry["current"], entry["proposed"]
    assert proposed["requests"] == pytest.approx(scale * current["requests"])
    for name in DEMANDS:
        for figure in FIGURES:
            assert proposed["demand"][name][figure] == pytest.approx(
                scale * current["demand"][name][figure])
    for limit in CAPACITY_LIMITS:
        for figure in FIGURES:
            assert proposed["limits"][limit][figure] == pytest.approx(
                scale * current["limits"][limit][figure])
        # A uniform scale moves no request to another bucket, and a stated
        # limit is printed as stated.
        assert proposed["limits"][limit]["peak_bucket"] == current["limits"][limit]["peak_bucket"]
        assert proposed["limits"][limit]["limit"] == current["limits"][limit]["limit"]
    # Volume changes how many requests there are, not how long one takes.
    for key in ("service_time_sources", "service_time_ms", "active_buckets"):
        assert proposed[key] == current[key]


@pytest.mark.parametrize("scale", [0.1, 0.7, 3, 1_000_000])
def test_c4_a_uniform_scale_over_several_applications_moves_no_peak_bucket(scale):    # C4
    """Three applications share one endpoint and one volume scale. A uniform
    scale multiplies every bucket by the same number, so the largest bucket
    is the largest bucket at any scale. Scaling each application's integer
    separately and adding the three products put the sum one unit in the
    last place off (0.7 x a + 0.7 x b + 0.7 x c is not 0.7 x (a + b + c) in
    floating point), and at 0.7 the peak moved from 12:24 to 12:34: so the
    integers of applications that share a factor are added first."""
    rng = random.Random(11)
    rows = synthetic([(rng.uniform(0, 3600), rng.choice([0.4, 7.3, 95.0, 410.0]))
                      for _ in range(1500)])
    for i, r in enumerate(rows):
        r.row["application_id"] = f"app-{'abc'[i % 3]}"
    limits = {"e": {"max_concurrency": 3.0, "requests_per_minute": 20.0,
                    "tokens_per_minute": 30000.0, "output_tokens_per_minute": 900.0}}
    plain = capacity_block(rows, CapacityPlan(limits=limits))["endpoints"]["e"]["current"]
    in_the_file = capacity_block(rows, CapacityPlan(limits=limits, volume={"*": float(scale)}))
    in_the_proposal = capacity_block(rows, CapacityPlan(limits=limits),
                                     {"workload": {"volume_scale": scale}})
    for arm in (in_the_file["endpoints"]["e"]["current"],
                in_the_proposal["endpoints"]["e"]["proposed"]):
        for limit in CAPACITY_LIMITS:
            assert arm["limits"][limit]["peak_bucket"] == plain["limits"][limit]["peak_bucket"], (
                limit)
            assert arm["limits"][limit]["peak"] == pytest.approx(
                scale * plain["limits"][limit]["peak"], rel=1e-12)
            assert arm["limits"][limit]["limit"] == plain["limits"][limit]["limit"]
    assert plain["limits"]["requests_per_minute"]["peak_bucket"] == "2026-09-20T12:24:00+00:00"


def test_c4_volume_changes_no_route_and_no_cost():                                     # C4
    rows, snapshot = mixed_traffic(), three_endpoints()
    plain, _ = replay(rows, snapshot)
    scaled, _ = replay(rows, snapshot, {"workload": {"volume_scale": 40,
                                                     "apps": {"app-b": {"volume_scale": 7}}}})
    assert [(r.current, r.proposed) for r in plain] == [(r.current, r.proposed) for r in scaled]


@pytest.mark.parametrize("factor", [0.5, 2, 8])
def test_c4_utilisation_is_inversely_linear_in_a_stated_limit(factor):                 # C4
    rows, snapshot = traffic(QUIET_THEN_BUSY), one_endpoint()
    stated = block_for(rows, snapshot, limits=LIMITS)["endpoints"]["local-cheap"]["current"]
    wider = {"local-cheap": {key: value * factor for key, value in LIMITS["local-cheap"].items()}}
    moved = block_for(rows, snapshot, limits=wider)["endpoints"]["local-cheap"]["current"]
    assert moved["demand"] == stated["demand"]                  # a limit moves no demand
    for limit in CAPACITY_LIMITS:
        assert moved["limits"][limit]["limit"] == LIMITS["local-cheap"][limit] * factor
        for figure in FIGURES:
            assert moved["limits"][limit][figure] == pytest.approx(
                stated["limits"][limit][figure] / factor)


def test_volume_scales_multiply_and_the_proposals_is_the_proposed_arm_only():
    """The file's scale is the real volume (both arms); the proposal's is a
    what-if on top of it (proposed arm). Per app: its own value, else the
    global one, on each side."""
    rows = traffic([2] * 12, app="app-a") + traffic([3] * 12, app="app-b")
    results, _ = replay(rows, one_endpoint())
    plan = CapacityPlan(volume={"*": 10.0, "app-b": 3.0})
    body = {"workload": {"volume_scale": 2, "apps": {"app-b": {"volume_scale": 0.5}}}}
    block = capacity_block(results, plan, body)
    assert block["volume_scale"] == {"base": {"*": 10.0, "app-b": 3.0},
                                     "proposal": {"*": 2.0, "app-b": 0.5}}
    entry = block["endpoints"]["local-cheap"]
    assert entry["current"]["requests"] == pytest.approx(24 * 10 + 36 * 3)
    assert entry["proposed"]["requests"] == pytest.approx(24 * 10 * 2 + 36 * 3 * 0.5)
    assert volume_for({"*": 10.0, "app-b": 3.0}, "app-b") == 3.0
    assert volume_for({}, "anyone") == 1.0


def test_token_scale_moves_token_demand_and_nothing_else():
    """`workload.token_scale` shrinks the prompt replay prices; it changes
    neither the completion, nor the request rate, nor the service time."""
    entry = block_for(traffic(QUIET_THEN_BUSY), one_endpoint(),
                      {"workload": {"token_scale": 0.5}})["endpoints"]["local-cheap"]
    current, proposed = entry["current"]["demand"], entry["proposed"]["demand"]
    assert current["tokens_per_minute"]["peak"] == 12 * 1000
    assert proposed["tokens_per_minute"]["peak"] == 12 * (450 + 100)
    for name in ("concurrency", "requests_per_minute", "output_tokens_per_minute"):
        assert proposed[name] == current[name], name


# --- service time per row, per arm (spec §5.4) ------------------------------------------

def test_a_row_served_where_it_was_recorded_keeps_its_own_service_time():
    """Two requests a minute, one of 2 s and one of 10 s. Replay's one
    latency for the endpoint is their MEDIAN; offered load is rate times
    the MEAN, and here each row simply keeps its own."""
    rows = []
    for minute in range(12):
        for j, latency in enumerate((2000.0, 10000.0)):
            written = T0 + timedelta(minutes=minute, seconds=5 + 20 * j, milliseconds=latency)
            rows.append(make_row(f"own-{minute}-{j}", "app-a", ts=written.isoformat(),
                                 latency_ms=latency))
    arm = block_for(rows, one_endpoint())["endpoints"]["local-cheap"]["current"]
    assert arm["service_time_sources"] == {"recorded": 24, "endpoint_mean": 0, "replay": 0,
                                           "asserted": 0}
    assert arm["service_time_ms"] == {"mean": 6000.0, "max": 10000}
    assert arm["demand"]["concurrency"] == pytest.approx(
        {"peak": 0.2, "mean_window": 0.2, "mean_active": 0.2})


def test_a_moved_row_gets_the_endpoints_mean_recorded_latency_not_the_median():
    """`app-b` may use `local-mid` only and its rows were recorded there:
    4 s, 4 s and 16 s a minute -- median 4 s, mean 8 s. `app-a`'s rows were
    recorded on `local-cheap`; removing it moves them to `local-mid`, an
    endpoint they never ran on, where each gets the mean 8 s."""
    snapshot = make_snapshot(apps=[app_entry("app-a"),
                                   app_entry("app-b", allowed_models=["local-mid"])])
    rows = traffic([2] * 12, app="app-a", latency_ms=1000.0)
    for minute in range(12):
        for j, latency in enumerate((4000.0, 4000.0, 16000.0)):
            written = T0 + timedelta(minutes=minute, seconds=3 + 17 * j, milliseconds=latency)
            rows.append(make_row(f"mid-{minute}-{j}", "app-b", ts=written.isoformat(),
                                 latency_ms=latency, complexity=0.6, attempts=["local-mid"],
                                 chosen="local-mid"))
    body = {"endpoints": {"remove": ["local-cheap"]}}
    results, _ = replay(rows, snapshot, body)
    assert endpoint_means(results) == {"local-cheap": 1000, "local-mid": 8000}
    moved = [r for r in results if r.row["application_id"] == "app-a"]
    assert {r.proposed.model for r in moved} == {"local-mid"}               # not vacuous
    assert {r.proposed.latency_ms for r in moved} == {4000.0}               # replay: the median
    assert {service_time(r, "proposed", endpoint_means(results)) for r in moved} == {
        (8000, "endpoint_mean")}
    mid = capacity_block(results, CapacityPlan(), body)["endpoints"]["local-mid"]
    assert mid["current"]["service_time_sources"] == {"recorded": 36, "endpoint_mean": 0,
                                                      "replay": 0, "asserted": 0}
    assert mid["proposed"]["service_time_sources"] == {"recorded": 36, "endpoint_mean": 24,
                                                       "replay": 0, "asserted": 0}
    assert mid["proposed"]["service_time_ms"]["mean"] == 8000.0
    assert mid["proposed"]["replay_share"] == 0.0


def test_an_endpoint_no_row_was_recorded_on_falls_back_to_replays_latency():
    """Every row was recorded on `local-cheap`; replay sends some to
    `local-mid`, which has no recorded request at all. Its service time is
    replay's latency for the endpoint -- here the tier default -- and the
    block says how much of the endpoint's service time rests on that."""
    results, _ = replay(mixed_traffic(), three_endpoints())
    assert "local-mid" not in endpoint_means(results)
    block = capacity_block(results, CapacityPlan())
    mid = block["endpoints"]["local-mid"]["current"]
    routed = [r.current for r in results if r.current.model == "local-mid"]
    assert routed and {a.latency_source for a in routed} == {"tier_default"}
    assert mid["service_time_sources"] == {"recorded": 0, "endpoint_mean": 0,
                                           "replay": len(routed), "asserted": 0}
    assert (mid["replay_share"], mid["replay_source"]) == (1.0, "tier_default")
    assert mid["service_time_ms"]["mean"] == routed[0].latency_ms


def test_an_endpoint_only_excluded_rows_ran_on_says_its_latency_is_replays():
    """Replay's median is over every record in the window, excluded ones
    too; the capacity engine sees only the simulated rows. An endpoint whose
    only served rows are excluded (here, an application the snapshot does
    not hold) has no mean of its own: its service time is replay's latency,
    and the block says that latency was an observed one."""
    rows = mixed_traffic() + traffic([1] * 5, app="app-gone", latency_ms=7000.0,
                                     attempts=["local-mid"], chosen="local-mid")
    results, excluded = replay(rows, three_endpoints())
    assert excluded["unknown_app"] == 5 and "local-mid" not in endpoint_means(results)
    mid = capacity_block(results, CapacityPlan(), excluded=excluded)["endpoints"]["local-mid"][
        "current"]
    assert mid["service_time_sources"]["replay"] == mid["requests"] > 0
    assert (mid["replay_share"], mid["replay_source"]) == (1.0, "observed")
    assert mid["service_time_ms"] == {"mean": 7000.0, "max": 7000}


def test_a_failed_over_row_never_lends_its_latency():
    """A row that took two attempts carries both attempts' time. It is not
    the service time of the endpoint that finally served it, and it does
    not feed that endpoint's mean."""
    rows = traffic([2] * 12)
    for row in rows[:6]:
        row.update(attempts=["local-gone", "local-cheap"], chosen="local-cheap",
                   latency_ms=60_000.0,
                   ts=(T0 + timedelta(minutes=20)).isoformat())
    results, _ = replay(rows, one_endpoint())
    assert endpoint_means(results) == {"local-cheap": 6000}
    arm = capacity_block(results, CapacityPlan())["endpoints"]["local-cheap"]["current"]
    assert arm["service_time_sources"] == {"recorded": 18, "endpoint_mean": 6, "replay": 0,
                                           "asserted": 0}
    assert arm["service_time_ms"] == {"mean": 6000.0, "max": 6000}


def test_an_asserted_latency_replaces_every_other_source_in_the_proposed_arm():
    """A proposal that asserts 12 s where 6 s was recorded: every row on the
    endpoint is 12 s long in the proposed arm, and still its own recorded
    6 s in the current one. It moves concurrency and nothing else."""
    body = {"endpoints": {"update": {"local-cheap": {"latency_ms": 12000}}}}
    entry = block_for(traffic(QUIET_THEN_BUSY), one_endpoint(), body)["endpoints"]["local-cheap"]
    assert entry["current"]["service_time_sources"]["recorded"] == 160
    assert entry["proposed"]["service_time_sources"] == {"recorded": 0, "endpoint_mean": 0,
                                                         "replay": 0, "asserted": 160}
    assert entry["current"]["service_time_ms"] == {"mean": 6000.0, "max": 6000}
    assert entry["proposed"]["service_time_ms"] == {"mean": 12000.0, "max": 12000}
    # 12 requests of 12 s arriving at seconds 5..49: the last runs 1 s into
    # the next minute, so a middle busy minute holds 143 s + 1 s = 144 s.
    assert entry["proposed"]["demand"]["concurrency"]["peak"] == pytest.approx(2.4)
    assert entry["current"]["demand"]["concurrency"]["peak"] == pytest.approx(1.2)
    # Twice the service time: all of it in the window, bar the last second
    # of the last request, which runs past the window's last bucket.
    in_window = entry["proposed"]["demand"]["concurrency"]["mean_window"] * 30 * 60_000
    assert entry["proposed"]["service_ms_after_window"] == 1000.0
    assert in_window + 1000 == pytest.approx(160 * 12_000)
    assert entry["current"]["demand"]["concurrency"]["mean_window"] * 30 * 60_000 == (
        pytest.approx(160 * 6_000))
    assert (entry["proposed"]["demand"]["requests_per_minute"]
            == entry["current"]["demand"]["requests_per_minute"])


#: 30 minutes, one request every 10 s, each really 10 s: 1.0 in service.
_STEADY_TEN_SECONDS = [(10 * i + 1, 10.0) for i in range(180)]
_NOT_COMPUTED = {"limit": 1.0, "not_computed": "tier_default_service_time", "peak": None,
                 "peak_bucket": None, "mean_window": None, "mean_active": None,
                 "buckets_over": None, "buckets_over_share": None, "requests_over_share": None}


def test_a_tier_default_is_not_a_service_time_so_no_concurrency_is_computed():
    """The proposal moves every request to `new`, an endpoint with no
    recorded request, no latency signal and no asserted latency. Replay's
    latency for it is the tier default (200 ms): a routing-score constant.
    Placing 10 s requests at 200 ms each read "peak 2.0% / avg 1.9%" against
    the same limit the current arm fills (100.0% / 96.8%) -- a fifty-fold
    drop that is nothing but the constant. So it is a state, not a number:
    no concurrency demand and no `max_concurrency` utilisation for that
    endpoint in that arm, and the headline does not rest on it. Requests and
    tokens are counted as before."""
    rows = synthetic(_STEADY_TEN_SECONDS, endpoint_ms=10_000.0,
                     proposed=arm_on("new", 200.0, "tier_default"))
    block = capacity_block(rows, CapacityPlan(limits={
        "e": {"max_concurrency": 1.0},
        "new": {"max_concurrency": 1.0, "requests_per_minute": 10.0}}))
    current, new = block["endpoints"]["e"]["current"], block["endpoints"]["new"]["proposed"]
    assert current["limits"]["max_concurrency"]["peak"] == 1.0
    assert current["limits"]["max_concurrency"]["mean_window"] == pytest.approx(0.968, abs=0.001)
    assert current["limits"]["max_concurrency"]["not_computed"] is None
    assert (new["replay_share"], new["replay_source"]) == (1.0, "tier_default")
    assert new["service_time_sources"] == {"recorded": 0, "endpoint_mean": 0, "replay": 180,
                                           "asserted": 0}
    assert new["limits"]["max_concurrency"] == _NOT_COMPUTED
    assert new["demand"]["concurrency"] == {"peak": None, "mean_window": None,
                                            "mean_active": None}
    # A rate limit counts arrivals: no service time is in it.
    assert new["requests"] == 180.0
    assert new["limits"]["requests_per_minute"]["peak"] == pytest.approx(0.6)
    assert new["limits"]["requests_per_minute"]["not_computed"] is None
    assert new["demand"]["requests_per_minute"]["peak"] == 6.0
    assert new["demand"]["tokens_per_minute"]["peak"] == 6000.0
    assert block["concurrency_not_computed"] == {"current": [], "proposed": ["new"]}
    # The headline is the largest COMPUTED utilisation.
    assert (block["peak"]["proposed"]["endpoint"], block["peak"]["proposed"]["limit"]) == (
        "new", "requests_per_minute")
    assert (block["peak"]["current"]["endpoint"], block["peak"]["current"]["utilisation"]) == (
        "e", 1.0)


def test_a_concurrency_limit_that_cannot_be_computed_is_never_the_headline():
    """With `max_concurrency` the only limit stated for `new`, the proposed
    arm's headline is not `new` at 2%: it is what can be computed (`e`, which
    the proposal emptied), and the block names the endpoint left out. With
    no other limit at all there is no headline, and the block still says
    why."""
    rows = synthetic(_STEADY_TEN_SECONDS, endpoint_ms=10_000.0,
                     proposed=arm_on("new", 200.0, "tier_default"))
    both = capacity_block(rows, CapacityPlan(limits={"e": {"max_concurrency": 1.0},
                                                     "new": {"max_concurrency": 1.0}}))
    assert (both["peak"]["proposed"]["endpoint"], both["peak"]["proposed"]["utilisation"]) == (
        "e", 0.0)
    assert both["concurrency_not_computed"]["proposed"] == ["new"]
    # Stated by the proposal, so for the proposed arm only.
    alone = capacity_block(rows, CapacityPlan(), {"capacity": {"new": {"max_concurrency": 1}}})
    assert alone["peak"] == {"current": None, "proposed": None}
    assert alone["concurrency_not_computed"] == {"current": [], "proposed": ["new"]}
    # Not stated is not "not computed".
    bare = capacity_block(rows, CapacityPlan(limits={"e": {"max_concurrency": 1.0}}))
    assert bare["concurrency_not_computed"] == {"current": [], "proposed": []}
    assert bare["endpoints"]["new"]["proposed"]["demand"]["concurrency"]["peak"] is None


@pytest.mark.parametrize("source,latency", [("signal", 9000.0), ("observed", 9000.0),
                                            ("proposal", 9000.0)])
def test_a_measured_or_asserted_latency_is_a_service_time(source, latency):
    """The state is for a tier default only. The snapshot's latency signal,
    replay's observed median and a latency the proposal asserts are each a
    figure about the endpoint, and concurrency is computed from them."""
    rows = synthetic(_STEADY_TEN_SECONDS, endpoint_ms=10_000.0,
                     proposed=arm_on("new", latency, source))
    block = capacity_block(rows, CapacityPlan(limits={"new": {"max_concurrency": 1.0}}))
    stated = block["endpoints"]["new"]["proposed"]["limits"]["max_concurrency"]
    assert stated["not_computed"] is None and stated["peak"] == pytest.approx(0.9)
    assert block["concurrency_not_computed"] == {"current": [], "proposed": []}
    assert block["peak"]["proposed"]["endpoint"] == "new"


def test_the_real_replay_marks_an_endpoint_no_row_was_recorded_on():
    """Through the real `simulate()`: every row was recorded on
    `local-cheap`, replay routes some to `local-mid`, and the snapshot has
    no signal for it -- in both arms of a no-op proposal."""
    block = block_for(mixed_traffic(), three_endpoints(), {}, limits={
        "local-mid": {"max_concurrency": 2.0, "tokens_per_minute": 90000.0}})
    for arm in ("current", "proposed"):
        mid = block["endpoints"]["local-mid"][arm]
        assert mid["replay_source"] == "tier_default" and mid["requests"] > 0
        assert mid["limits"]["max_concurrency"] == {**_NOT_COMPUTED, "limit": 2.0}
        assert mid["limits"]["tokens_per_minute"]["peak"] > 0
        assert block["peak"][arm]["limit"] == "tokens_per_minute"
    assert block["concurrency_not_computed"] == {"current": ["local-mid"],
                                                 "proposed": ["local-mid"]}


@pytest.mark.parametrize("latency", [86_400_001, 1e12, 1e18])
def test_c15_an_asserted_latency_too_long_to_place_is_refused(latency):                # C15
    """An operator's claim is never silently ignored, and a service time of
    more than 24 hours cannot be placed."""
    body = {"endpoints": {"update": {"local-cheap": {"latency_ms": latency}}}}
    results, _ = replay(traffic([2] * 12), one_endpoint(), body)
    with pytest.raises(CapacityError) as err:
        capacity_block(results, CapacityPlan(), body)
    assert err.value.where == "endpoints.local-cheap.latency_ms"
    assert "24 hours" in err.value.message


@pytest.mark.parametrize("source,where,what", [
    ("signal", "snapshot.signals.new.latency_ms", "the snapshot's latency signal"),
    ("observed", "export.latency_ms (rows served by new)", "the median recorded latency"),
    ("tier_default", "snapshot.settings.routing.signals.tier_latency_ms",
     "the tier default latency"),
])
def test_c15_a_replayed_latency_too_long_to_place_names_where_it_came_from(source, where, what):
    """A service time of more than 24 hours cannot be placed, whoever
    supplied it. When it is replay's own latency for the endpoint the error
    names the real source -- the snapshot's signal, the export's rows or the
    tier default -- and not `endpoints.<slug>.latency_ms`, a key the
    operator never wrote."""
    rows = synthetic([(10 * i + 1, 6.0) for i in range(60)],
                     proposed=arm_on("new", 1e12, source))
    with pytest.raises(CapacityError) as err:
        capacity_block(rows, CapacityPlan())
    assert err.value.where == where
    assert what in err.value.message and "24 hours" in err.value.message
    assert "asserted" not in err.value.message


# --- C18, C19: one request is one request; the mean is the time-average ---------------

def arm_on(model: str, latency_ms: float, source: str) -> ArmResult:
    """A routed `ArmResult` on `model`, with the ONE latency replay would
    report for that endpoint and the rung it came from."""
    return ArmResult("routed", model, "onprem", "cheap", (model,), (), 0.0, latency_ms, source,
                     None, None, prompt_tokens=900, completion_tokens=100)


def synthetic(requests, *, endpoint_ms: float = 6000.0,
              proposed: ArmResult | None = None) -> list[RowResult]:
    """`RowResult`s for `(arrival seconds after T0, service seconds)` pairs,
    each recorded as served alone by endpoint `e` with its own service time,
    and routed there by both arms. Built by hand, not replayed: these tests
    are about arithmetic on a known arrival process, and need streams of
    thousands of requests with service times the fixtures do not have.
    `endpoint_ms` is the ONE latency replay would report for the endpoint.
    `proposed` replaces the proposed arm (`arm_on`): a proposal that moves
    every row somewhere else, or asserts a latency."""
    arm = arm_on("e", endpoint_ms, "observed")
    out = []
    for i, (arrival, service) in enumerate(requests):
        written = T0 + timedelta(milliseconds=round(arrival * 1000) + round(service * 1000))
        row = make_row(f"s{i}", "app-a", ts=written.isoformat(), latency_ms=service * 1000.0,
                       attempts=["e"], chosen="e")
        out.append(RowResult(row, arm, proposed or arm))
    return out


def _poisson(rate: float, hours: float, seed: int, service) -> list[tuple[float, float]]:
    rng, t, out = random.Random(seed), 0.0, []
    while True:
        t += rng.expovariate(rate)
        if t >= hours * 3600:
            return out
        out.append((round(t * 1000) / 1000, service(rng)))


def _in_service(requests) -> tuple[int, float]:
    """`(the largest number in service at one instant, request-seconds in
    service)` from an event sweep over the raw pairs -- nothing bucketed."""
    events = sorted([(a, 1) for a, s in requests] + [(a + s, -1) for a, s in requests])
    now, top, area, last = 0, 0, 0.0, events[0][0]
    for at, step in events:
        area += now * (at - last)
        now, last = now + step, at
        top = max(top, now)
    return top, area


def _one(requests, *, limit: float = 1.0, **plan_kw) -> tuple[dict, dict]:
    plan_kw.setdefault("min_buckets", 1)
    block = capacity_block(synthetic(requests), CapacityPlan(
        limits={"e": {"max_concurrency": limit}}, **plan_kw))
    return block, block["endpoints"]["e"]["current"]["limits"]["max_concurrency"]


def test_c18_twelve_requests_two_hours_apart_never_exceed_one_in_service():            # C18
    """The review's example: 12 requests, one every two hours, each 138 s,
    against `max_concurrency: 1`. There is never more than one request in
    service. Counting 138 s in the 60 s arrival bucket read 230% at the
    peak, 230% on average, every bucket and every request over.

    By hand: a request arriving at second 10 fills 50 s of its own minute,
    all of the next, and 28 s of the third. The window is 22 h of arrivals
    plus those 3 minutes = 1323 minutes; 12 hold an arrival; 36 are active.
    Time-average: 12 x 138 s / (1323 x 60 s) = 0.0209 in service."""
    requests = [(7200 * i + 10, 138.0) for i in range(12)]
    assert _in_service(requests)[0] == 1
    block, stated = _one(requests)
    assert block["window"]["buckets"] == 1323 and block["window"]["arrival_buckets"] == 12
    assert block["endpoints"]["e"]["current"]["active_buckets"] == 36
    assert stated["peak"] == 1.0                           # the one full minute of each
    assert stated["mean_window"] == pytest.approx(12 * 138 / (1323 * 60))
    assert stated["mean_window"] == pytest.approx(0.02, abs=0.002)
    assert stated["mean_active"] == pytest.approx(12 * 138 / (36 * 60))
    assert (stated["buckets_over"], stated["buckets_over_share"],
            stated["requests_over_share"]) == (0, 0.0, 0.0)
    assert block["peak"]["current"]["utilisation"] == 1.0
    assert block["peak"]["current"]["mean_window"] == stated["mean_window"]


def test_c18_one_lone_five_minute_request_is_one_request():                            # C18
    block, stated = _one([(10, 300.0)])
    assert stated["peak"] == 1.0 and block["window"]["buckets"] == 6
    assert stated["mean_window"] == pytest.approx(300 / 360)
    assert stated["buckets_over"] == 0


@pytest.mark.parametrize("seconds", [1, 7, 60, 3600])
def test_c18_a_single_request_never_fills_more_than_a_bucket(seconds):                 # C18
    """Whatever the arrival, the service time and the bucket: one request
    puts at most a bucket's worth of milliseconds into any bucket, and all
    of its service time into some bucket."""
    rng = random.Random(seconds)
    for _ in range(300):
        request = (rng.uniform(0, 7200), rng.choice([0.0, 0.001, 0.5, 59.999, 60.0, 61.0, 138.0,
                                                    rng.uniform(0, 5000)]))
        load = arm_load(synthetic([request]), "current", bucket_seconds=seconds)["e"]
        placed = [per_app["app-a"][SERVICE_MS] for per_app in load.cells.values()]
        assert max(placed) <= seconds * 1000
        assert sum(placed) == round(request[1] * 1000) == load.service_ms


def test_c19_the_window_mean_is_the_time_average_under_skewed_service_times():         # C19
    """Poisson arrivals at 1/3 per second for six hours; lognormal service
    times, median 6 s, sigma 1 -- so the mean is 6 x e^0.5 = 9.89 s and
    offered load is 3.30. Replay's one latency for the endpoint is the
    MEDIAN: rate x median reads 2.0, a 38% understatement. With each row
    served where it was recorded, the window mean is the true time-average
    number in service (an event sweep over the raw pairs, nothing
    bucketed)."""
    requests = _poisson(1 / 3, 6, 9, lambda rng: rng.lognormvariate(math.log(6), 1.0))
    median = statistics.median(s for _, s in requests)
    assert len(requests) > 7000
    rows = synthetic(requests, endpoint_ms=median * 1000)
    block = capacity_block(rows, CapacityPlan(limits={"e": {"max_concurrency": 1.0}}))
    stated = block["endpoints"]["e"]["current"]["limits"]["max_concurrency"]
    _top, area = _in_service(requests)
    true_average = area / (block["window"]["buckets"] * 60)
    assert stated["mean_window"] == pytest.approx(true_average, rel=0.001)
    offered = (1 / 3) * 6 * math.exp(0.5)                       # lambda x E[S]
    assert stated["mean_window"] == pytest.approx(offered, rel=0.05)
    # What one latency per endpoint gave, for the record: far outside 5%.
    assert len(requests) / (6 * 3600) * median < 0.65 * true_average
    assert block["endpoints"]["e"]["current"]["service_time_sources"]["recorded"] == len(rows)
    # And no bucket claims more requests in service than there ever were.
    assert stated["peak"] <= _top


def test_c19_the_window_mean_is_steady_and_the_peak_depends_on_window_and_bucket():     # C19
    """A steady Poisson stream (0.5/s, 4 s each: 2.0 in service). The window
    mean stays at 2.0 for an hour or a day, in 10 s or 60 s buckets. The
    peak is a maximum over buckets: a longer window can only raise it and a
    shorter bucket raises it -- to three times the mean here -- which is why
    no peak is reported alone."""
    stream = _poisson(0.5, 24, 3, lambda rng: 4.0)
    seen = {}
    for hours in (1, 24):
        for seconds in (10, 60):
            _block, stated = _one([r for r in stream if r[0] < hours * 3600],
                                  bucket_seconds=seconds)
            assert stated["mean_window"] == pytest.approx(2.0, rel=0.03), (hours, seconds)
            seen[hours, seconds] = stated["peak"]
    assert seen[24, 10] >= seen[1, 10] > seen[1, 60] and seen[24, 60] >= seen[1, 60]
    assert seen[24, 10] > seen[24, 60] and seen[24, 10] > 3 * 2.0 > seen[24, 60] > 1.5 * 2.0


# --- C5, C6: a no-op, and an endpoint that goes away -----------------------------------

def test_c5_a_noop_proposal_gives_identical_demand_in_both_arms():                     # C5
    block = block_for(mixed_traffic(), three_endpoints(), {},
                      limits={"local-mid": {"max_concurrency": 2.0,
                                            "requests_per_minute": 5.0}})
    assert len(block["endpoints"]) >= 2                                   # not vacuous
    for slug, entry in block["endpoints"].items():
        assert entry["current"] == entry["proposed"], slug
    assert block["peak"]["current"] == block["peak"]["proposed"] is not None


def test_c6_a_removed_endpoints_demand_goes_where_replay_routes_it():                  # C6
    """Tied to the real router: the expected per-endpoint demand is counted
    from the replayed picks themselves, and the endpoints that gained are
    the `to` side of the summary's own moves away from the removed one."""
    rows, snapshot = mixed_traffic(), three_endpoints()
    results, excluded = replay(rows, snapshot, _DROP_MID)
    block = capacity_block(results, CapacityPlan(), _DROP_MID, excluded=excluded)
    for arm in ("current", "proposed"):
        picks = Counter(getattr(r, arm).model for r in results
                        if getattr(r, arm).outcome == "routed")
        assert {slug: entry[arm]["requests"] for slug, entry in block["endpoints"].items()
                if entry[arm]["requests"]} == dict(picks), arm
    gone = block["endpoints"]["local-mid"]
    assert gone["current"]["requests"] > 0 and gone["proposed"]["requests"] == 0.0
    assert gone["proposed"]["service_time_ms"] == {"mean": None, "max": None}
    assert sum(gone["proposed"]["service_time_sources"].values()) == 0
    assert gone["proposed"]["active_buckets"] == 0
    # No served row was recorded on `local-mid`, so its service time is the tier default.
    assert gone["current"]["replay_source"] == "tier_default"
    sim = summarize(results, excluded, inputs={}, edits=normalized_edits(_DROP_MID),
                    assumptions=assumptions_block(_DROP_MID))
    gained = {move["to"]: move["count"] for move in sim["moves"]
              if move["from"] == "local-mid" and move["to"] != "(rejected)"}
    assert gained                                                          # not vacuous
    for slug, count in gained.items():
        entry = block["endpoints"][slug]
        assert entry["proposed"]["requests"] - entry["current"]["requests"] == count
    total = {arm: sum(entry[arm]["requests"] for entry in block["endpoints"].values())
             for arm in ("current", "proposed")}
    assert total["proposed"] == (total["current"] - sim["totals"]["newly_rejected"]
                                 + sim["totals"]["newly_served"])


# --- C8: a window too short to say anything -------------------------------------------

def test_c8_fewer_buckets_than_the_minimum_is_a_state_not_a_number():                  # C8
    block = block_for(traffic([3] * 9), one_endpoint(), limits=LIMITS)
    assert block["window_too_short"] is True and block["window"]["arrival_buckets"] == 9
    assert block["endpoints"] == {} and block["peak"] == {"current": None, "proposed": None}
    text = json.dumps(block)
    assert "utilisation" not in text and "buckets_over" not in text and "demand" not in text


def test_c8_the_minimum_counts_buckets_with_an_arrival_not_the_window():               # C8
    """Nine long requests keep the endpoint busy for 18 minutes. The window
    is 18 buckets; a request ARRIVED in 9 of them, and 9 is what the
    minimum is compared with: the conservative count, which a long service
    time cannot inflate."""
    rows = [make_row(f"long-{m}", "app-a", latency_ms=590_000.0,
                     ts=(T0 + timedelta(minutes=m, seconds=5, milliseconds=590_000)).isoformat())
            for m in range(9)]
    block = block_for(rows, one_endpoint(), limits=LIMITS)
    assert block["window"]["buckets"] == 18 and block["window"]["arrival_buckets"] == 9
    assert block["window_too_short"] is True and block["endpoints"] == {}


def test_c8_the_minimum_is_inclusive_and_can_be_lowered():                             # C8
    at = block_for(traffic([3] * 10), one_endpoint(), limits=LIMITS)
    assert at["window_too_short"] is False and at["peak"]["current"] is not None
    one = block_for(traffic([3]), one_endpoint(), limits=LIMITS, min_buckets=1)
    stated = one["endpoints"]["local-cheap"]["current"]["limits"]["requests_per_minute"]
    assert one["window_too_short"] is False
    assert stated["peak"] == stated["mean_window"] == stated["mean_active"] == 0.3


def test_c8_no_rows_at_all():                                                          # C8
    block = capacity_block([], CapacityPlan(limits=LIMITS))
    assert block["window_too_short"] is True and block["endpoints"] == {}
    assert block["window"] == {"first_bucket": None, "last_bucket": None, "buckets": 0,
                               "arrival_buckets": 0}
    assert block["rows"] == {"simulated": 0, "without_ts": 0, "unusable_latency": 0,
                             "excluded_not_counted": 0}


def test_a_gap_of_days_shows_in_the_window_mean_and_not_in_the_active_one():
    """Two sessions a week apart: the window spans 10,090 buckets and the
    endpoint is active in 20 of them. Over the window it is all but idle
    (the time-average); while it is active it is at 60%. Both are printed,
    each with its denominator."""
    rows = traffic([6] * 10) + traffic([6] * 10, start=T0 + timedelta(days=7))
    block = block_for(rows, one_endpoint(), limits=LIMITS)
    assert block["window"]["buckets"] == 7 * 24 * 60 + 10
    assert block["window"]["arrival_buckets"] == 20
    arm = block["endpoints"]["local-cheap"]["current"]
    stated = arm["limits"]["requests_per_minute"]
    assert arm["active_buckets"] == 20
    assert stated["peak"] == pytest.approx(0.6) and stated["mean_active"] == pytest.approx(0.6)
    assert stated["mean_window"] == pytest.approx(0.6 * 20 / 10090)


def test_each_mean_has_its_own_stated_denominator():
    """`local-mid` is busy in some of the window's 195 buckets. `mean_window`
    is over all 195 -- the same denominator for every endpoint and both arms
    -- and `mean_active` over the endpoint's own `active_buckets`. The share
    of buckets over a limit is of the window."""
    results, _ = replay(mixed_traffic(), three_endpoints())
    own = arm_load(results, "current", bucket_seconds=60)["local-mid"]
    requests = sum(cell[REQUESTS] for per_app in own.cells.values() for cell in per_app.values())
    block = capacity_block(results, CapacityPlan(
        limits={"local-mid": {"requests_per_minute": 1.5}}))
    assert block["window"]["buckets"] == 195 and block["window"]["arrival_buckets"] == 60
    arm = block["endpoints"]["local-mid"]["current"]
    stated = arm["limits"]["requests_per_minute"]
    assert 0 < arm["active_buckets"] == len(own.cells) < 195
    assert stated["mean_window"] == pytest.approx(requests / 195 / 1.5)
    assert stated["mean_active"] == pytest.approx(requests / len(own.cells) / 1.5)
    assert stated["buckets_over"] > 0
    assert stated["buckets_over_share"] == stated["buckets_over"] / 195


def test_the_peak_depends_on_the_bucket_and_the_window_mean_does_not():
    """The same rows in 60 s, 600 s and 1800 s buckets. The total, and so
    the mean over the window, is the same. A 600 s bucket lies exactly on
    the busy ten minutes, so the peak is still the busy rate; an 1800 s
    bucket mixes quiet and busy and the peak falls to the mean. The block
    says which bucket it used."""
    rows = traffic(QUIET_THEN_BUSY)
    peaks = {}
    for seconds in (60, 600, 1800):
        block = block_for(rows, one_endpoint(), limits=LIMITS, min_buckets=1,
                          bucket_seconds=seconds)
        arm = block["endpoints"]["local-cheap"]["current"]
        stated = arm["limits"]["requests_per_minute"]
        assert block["bucket_seconds"] == seconds
        assert block["window"]["buckets"] == 1800 // seconds
        assert arm["requests"] == 160.0
        assert stated["mean_window"] == pytest.approx(160 / 30 / 10), seconds
        peaks[seconds] = stated["peak"]
    assert peaks == pytest.approx({60: 1.2, 600: 1.2, 1800: 160 / 30 / 10})


# --- endpoints with nothing stated, or nothing routed ------------------------------------

def test_an_endpoint_with_a_limit_and_no_rows_is_shown_at_zero():
    block = block_for(traffic([2] * 12), make_snapshot(), limits={
        "local-big": {"max_concurrency": 4.0}})
    arm = block["endpoints"]["local-big"]["current"]
    assert arm["requests"] == 0.0 and arm["active_buckets"] == 0
    assert arm["service_time_ms"] == {"mean": None, "max": None}
    assert (arm["replay_share"], arm["replay_source"]) == (None, None)
    assert arm["limits"]["max_concurrency"] == {
        "limit": 4.0, "not_computed": None, "peak": 0.0, "peak_bucket": None,
        "mean_window": 0.0, "mean_active": 0.0, "buckets_over": 0, "buckets_over_share": 0.0,
        "requests_over_share": None}
    assert block["peak"]["current"] == {
        "endpoint": "local-big", "limit": "max_concurrency", "utilisation": 0.0, "bucket": None,
        "mean_window": 0.0, "buckets_over_share": 0.0, "bucket_seconds": 60, "buckets": 12,
        "service_after_window_share": None}


def test_an_endpoint_with_demand_and_no_limit_is_shown_without_a_utilisation():
    """Traffic that lands on an endpoint nobody stated a capacity for must
    not be invisible -- and must not be given a utilisation."""
    block = block_for(traffic([2] * 12), one_endpoint())
    arm = block["endpoints"]["local-cheap"]["current"]
    assert arm["limits"] == {} and arm["demand"]["requests_per_minute"]["peak"] == 2.0
    assert block["peak"] == {"current": None, "proposed": None}


def test_a_proposal_overrides_a_limit_key_by_key_in_the_proposed_arm_only():
    base = {"local-cheap": {"max_concurrency": 1.0, "requests_per_minute": 10.0}}
    body = {"capacity": {"local-cheap": {"max_concurrency": 2}}}
    entry = block_for(traffic(QUIET_THEN_BUSY), one_endpoint(), body,
                      limits=base)["endpoints"]["local-cheap"]
    assert {k: v["limit"] for k, v in entry["current"]["limits"].items()} == base["local-cheap"]
    assert {k: v["limit"] for k, v in entry["proposed"]["limits"].items()} == {
        "max_concurrency": 2.0, "requests_per_minute": 10.0}
    assert entry["proposed"]["limits"]["max_concurrency"]["peak"] == pytest.approx(0.6)
    assert entry["current"]["limits"]["max_concurrency"]["peak"] == pytest.approx(1.2)


def test_an_added_endpoint_carries_its_capacity_in_the_proposed_arm():
    body = {"endpoints": {"add": [{"slug": "local-cheap-2", "provider": "onprem",
                                   "tier": "cheap", "latency_ms": 3000,
                                   "price": {"input": 0.01, "output": 0.02,
                                             "cached_input": None}}]},
            "capacity": {"local-cheap-2": {"max_concurrency": 4}}}
    block = block_for(traffic(QUIET_THEN_BUSY), one_endpoint(), body, limits=LIMITS)
    added = block["endpoints"]["local-cheap-2"]
    assert added["current"]["limits"] == {} and added["current"]["requests"] == 0.0
    assert added["proposed"]["limits"]["max_concurrency"]["limit"] == 4.0
    assert added["proposed"]["requests"] > 0                 # the cheaper endpoint takes rows
    assert added["proposed"]["service_time_ms"] == {"mean": 3000.0, "max": 3000}
    assert added["proposed"]["service_time_sources"]["asserted"] == added["proposed"]["requests"]


def test_a_response_longer_than_a_bucket_is_spread_and_needs_no_flag():
    """A 90 s response against 60 s buckets used to be counted whole in the
    arrival bucket and flagged (`latency_exceeds_bucket`). It is now in the
    buckets it runs through, so the flag is gone: the same rows give the
    same time-average in 60 s and in 120 s buckets."""
    rows = traffic([2] * 12, latency_ms=90_000.0)
    short = block_for(rows, one_endpoint())
    long = block_for(rows, one_endpoint(), bucket_seconds=120, min_buckets=1)
    for block in (short, long):
        arm = block["endpoints"]["local-cheap"]["current"]
        assert "latency_exceeds_bucket" not in arm
        assert arm["service_time_ms"] == {"mean": 90000.0, "max": 90000}
        in_window = arm["demand"]["concurrency"]["mean_window"] * block["window"]["buckets"] * (
            block["bucket_seconds"])
        assert in_window == pytest.approx(24 * 90)             # request-seconds, all placed
    assert short["endpoints"]["local-cheap"]["current"]["demand"]["concurrency"]["peak"] <= 4.0


def test_rows_sized_from_an_estimate_and_rows_left_out_are_counted():
    rows = traffic([2] * 12)
    for row in rows[:5]:                 # served, but the backend reported no usage
        row.update(prompt_tokens=0, completion_tokens=None, routing_tokens=800, max_tokens=64)
    results, _ = replay(rows, one_endpoint())
    block = capacity_block(results, CapacityPlan(), excluded={
        "cache_hit": 3, "before_since": 2, "missing_features": 4, "unknown_app": 1,
        "unknown_status": 0, "missing_routing_tokens": 0})
    assert block["rows"]["excluded_not_counted"] == 5
    assert block["endpoints"]["local-cheap"]["current"]["estimated_token_rows"] == 5
    estimated = [r.current for r in results if r.row.get("routing_tokens") == 800]
    assert {(a.prompt_tokens, a.completion_tokens) for a in estimated} == {(800, 64)}


def test_a_row_no_arm_routes_is_not_counted_as_estimated_demand():
    """`app-b` may use `local-mid` only, and the snapshot has no such
    endpoint: its rows are rejected in both arms and load nothing. Their
    token counts are estimates, and they are in no endpoint's demand -- so
    they are in no endpoint's count of estimated rows either."""
    snapshot = make_snapshot(onboarded=[onboarded_row("local-cheap", "cheap")],
                             apps=[app_entry("app-a"),
                                   app_entry("app-b", allowed_models=["local-mid"])])
    refused = traffic([1] * 12, app="app-b", status="no_eligible", attempts=[], chosen=None)
    for row in refused:
        row.update(prompt_tokens=0, completion_tokens=None, routing_tokens=800, max_tokens=64)
    rows = traffic([2] * 12) + refused
    results, _ = replay(rows, snapshot)
    rejected = [r for r in results if r.row["application_id"] == "app-b"]
    assert len(rejected) == 12
    assert {(r.current.outcome, r.proposed.outcome) for r in rejected} == {
        ("rejected", "rejected")}
    block = capacity_block(results, CapacityPlan())
    assert set(block["endpoints"]) == {"local-cheap"}
    for arm in ("current", "proposed"):
        assert block["endpoints"]["local-cheap"][arm]["estimated_token_rows"] == 0
    assert "estimated_tokens" not in block["rows"]


# --- C2, C10: deterministic; nothing ranks or prescribes ---------------------------------

#: Volume scales with no exact binary form: summing their products in row
#: order gave two different blocks for one set of rows.
_AWKWARD = {"app-a": 0.1, "app-b": 0.7, "app-c": 1.3}


def test_c2_the_block_is_byte_identical_whatever_the_row_order():                      # C2
    """Three applications at volume scales 0.1 / 0.7 / 1.3 on shared
    endpoints, rows shuffled: the same bytes every time, before any
    rounding."""
    snapshot = make_snapshot(apps=[app_entry("app-a"), app_entry("app-b"), app_entry("app-c")])
    rng = random.Random(4)
    rows = []
    for i in range(600):
        app = rng.choice(sorted(_AWKWARD))
        latency = rng.choice([123.4, 3217.3, 41234.7, 50000.0])
        written = T0 + timedelta(milliseconds=rng.randrange(3_600_000) + round(latency))
        rows.append(make_row(f"mix-{i}", app, ts=written.isoformat(), latency_ms=latency,
                             prompt_tokens=rng.randrange(1, 5000),
                             completion_tokens=rng.randrange(0, 900),
                             complexity=rng.choice([0.05, 0.6, 1.0])))
    results, excluded = replay(rows, snapshot, _DROP_MID)
    plan = CapacityPlan(limits={"local-cheap": dict(LIMITS["local-cheap"]),
                                "local-big": {"max_concurrency": 3.0}}, volume=_AWKWARD)
    body = {**_DROP_MID, "workload": {"volume_scale": 2.5,
                                      "apps": {"app-a": {"volume_scale": 0.3}}}}
    first = json.dumps(capacity_block(results, plan, body, excluded=excluded), sort_keys=True)
    assert len({r.current.model for r in results if r.current.outcome == "routed"}) >= 2
    for seed in range(25):
        shuffled = results[:]
        random.Random(seed).shuffle(shuffled)
        again = json.dumps(capacity_block(shuffled, plan, body, excluded=excluded),
                           sort_keys=True)
        assert again == first, seed
    assert json.dumps(capacity_block(results[::-1], plan, body, excluded=excluded),
                      sort_keys=True) == first


def test_c2_a_tie_for_the_peak_goes_to_the_earliest_bucket_whatever_the_order():       # C2
    """Two buckets hold the same requests of the same applications. Their
    demand is equal, so the peak bucket is the earlier one -- however the
    rows are ordered. Summed in row order, 0.1 + 0.7 + 1.3 + ... came to
    5.5 in one order and 5.500000000000001 in another, and the peak moved."""
    apps = ["app-a", "app-b", "app-c", "app-a", "app-b", "app-c", "app-c"]
    snapshot = make_snapshot(onboarded=[onboarded_row("local-cheap", "cheap")],
                             apps=[app_entry(app) for app in sorted(_AWKWARD)])
    rows = [make_row(f"tie-{minute}-{j}", app, latency_ms=0.0,
                     ts=(T0 + timedelta(minutes=minute, seconds=1 + j)).isoformat())
            for minute in (0, 1) for j, app in enumerate(apps)]
    rows += [make_row(f"pad-{m}", "app-a", latency_ms=0.0,
                      ts=(T0 + timedelta(minutes=m, seconds=1)).isoformat())
             for m in range(2, 12)]
    results, _ = replay(rows, snapshot)
    plan = CapacityPlan(limits={"local-cheap": {"requests_per_minute": 1.0}}, volume=_AWKWARD)
    seen = set()
    for order in itertools.islice(itertools.permutations(range(14)), 0, 400_000, 1999):
        shuffled = [results[i] for i in order] + results[14:]
        stated = capacity_block(shuffled, plan)["endpoints"]["local-cheap"]["current"]["limits"][
            "requests_per_minute"]
        seen.add((stated["peak_bucket"], stated["peak"]))
    assert len(seen) == 1
    bucket, peak = seen.pop()
    assert bucket == "2026-09-20T12:00:00+00:00" and peak == pytest.approx(5.5)


def test_c2_many_applications_in_one_bucket_sum_the_same_in_any_order():               # C2
    """Eight applications at volume scales with no exact binary form share
    every bucket. The order their rows come in is the order a plain float
    sum would add them in; the block is the same bytes in every order,
    because each application's exact integer is scaled once and the eight
    products are added in sorted order."""
    volume = {f"app-{c}": 0.1 + 0.6 * i for i, c in enumerate("abcdefgh")}
    rows = synthetic([(37 * i % 1800, 1 + i % 90) for i in range(400)])
    for i, r in enumerate(rows):
        r.row["application_id"] = f"app-{'abcdefgh'[i * 5 % 8]}"
    plan = CapacityPlan(limits={"e": {"max_concurrency": 3.0, "requests_per_minute": 9.0,
                                      "tokens_per_minute": 7000.0}}, volume=volume)
    first = json.dumps(capacity_block(rows, plan), sort_keys=True)
    for seed in range(40):
        shuffled = rows[:]
        random.Random(seed).shuffle(shuffled)
        assert json.dumps(capacity_block(shuffled, plan), sort_keys=True) == first, seed


def test_c2_the_block_does_not_depend_on_hash_randomisation():                         # C2
    """The same block from interpreters started with different hash seeds:
    no set or dict order reaches the output."""
    program = (
        "import json, os\n"
        "os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'\n"
        "from app.eval.capacity import CapacityPlan, capacity_block\n"
        "from tests.eval.test_capacity import synthetic\n"
        "rows = synthetic([(37 * i % 1800, 1 + i % 90) for i in range(400)])\n"
        "for i, r in enumerate(rows):\n"
        "    r.row['application_id'] = 'app-' + 'abcdefgh'[i % 8]\n"
        "plan = CapacityPlan(limits={'e': {'max_concurrency': 3.0}, 'z': {'max_concurrency': 1.0},"
        " 'a': {'requests_per_minute': 9.0}},"
        " volume={'app-' + c: 0.1 + i * 0.6 for i, c in enumerate('abcdefgh')})\n"
        "body = {'capacity': {'q': {'max_concurrency': 2}, 'b': {'tokens_per_minute': 5}}}\n"
        "print(json.dumps(capacity_block(rows, plan, body)))\n")
    outputs = set()
    for seed in ("0", "1", "4242"):
        done = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True,
                              env={**os.environ, "PYTHONHASHSEED": seed}, check=True)
        outputs.add(done.stdout)
    assert len(outputs) == 1 and '"endpoints"' in outputs.pop()


def test_c10_no_key_ranks_recommends_or_prescribes():                                  # C10
    block = block_for(traffic(QUIET_THEN_BUSY), one_endpoint(),
                      {"workload": {"volume_scale": 2}}, limits=LIMITS)
    # Not the first model's id ("bucketed_offered_load"), which counted a
    # request's whole service time in its arrival bucket: a block of that
    # model can never be read as one of this.
    assert block["method"] == METHOD == "spread_offered_load"
    assert block["not_modelled"] == list(NOT_MODELLED)
    words = keys_of(block) + list(NOT_MODELLED)
    assert not [w for w in words if any(b in w.lower() for b in CAPACITY_BANNED)]
    # Exactly these keys: there is no overflow, rejection or remedy field.
    assert set(block) == {"method", "bucket_seconds", "min_buckets", "window", "rows",
                          "volume_scale", "not_modelled", "window_too_short", "endpoints",
                          "peak", "concurrency_not_computed"}
    assert set(block["endpoints"]["local-cheap"]["current"]) == {
        "requests", "active_buckets", "service_time_sources", "service_time_ms", "replay_share",
        "replay_source", "service_ms_after_window", "service_after_window_share",
        "estimated_token_rows", "demand", "limits"}
    assert set(block["endpoints"]["local-cheap"]["current"]["limits"]["max_concurrency"]) == {
        "limit", "not_computed", "peak", "peak_bucket", "mean_window", "mean_active",
        "buckets_over", "buckets_over_share", "requests_over_share"}
    assert set(block["peak"]["current"]) == {
        "endpoint", "limit", "utilisation", "bucket", "mean_window", "buckets_over_share",
        "bucket_seconds", "buckets", "service_after_window_share"}


def _peaks_without_context(node, path: str = "") -> list[str]:
    """Every dict that carries a peak figure without its context beside it:
    the window mean, and -- for a utilisation (the headline, or a stated
    limit's entry) -- the share of the window's buckets over the limit."""
    bare = []
    if isinstance(node, dict):
        figure = ("peak" in node or "utilisation" in node) and not isinstance(
            node.get("peak"), dict)
        utilisation = "utilisation" in node or ("peak" in node and "limit" in node)
        if (figure and "mean_window" not in node) or (
                utilisation and "buckets_over_share" not in node):
            bare.append(path)
        for key, value in node.items():
            bare += _peaks_without_context(value, f"{path}/{key}")
    return bare


def test_c20_no_peak_is_reported_without_the_window_mean_and_the_share_beside_it():    # C20
    """In the block: every object with a `peak` (or the headline's
    `utilisation`) has `mean_window` next to it, and every utilisation has
    `buckets_over_share` too."""
    block = block_for(mixed_traffic(), three_endpoints(), _DROP_MID,
                      limits={"local-mid": {"max_concurrency": 2.0},
                              "local-big": {"requests_per_minute": 5.0}})
    assert block["peak"]["proposed"] is not None
    assert _peaks_without_context(block) == []
    # The detector is not vacuous: strip one of either and it fires.
    stated = block["endpoints"]["local-big"]["proposed"]["limits"]["requests_per_minute"]
    del stated["mean_window"]
    assert _peaks_without_context(block) == [
        "/endpoints/local-big/proposed/limits/requests_per_minute"]
    stated["mean_window"] = 0.0
    del block["peak"]["proposed"]["buckets_over_share"]
    assert _peaks_without_context(block) == ["/peak/proposed"]
