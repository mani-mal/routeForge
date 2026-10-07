"""P3c-4: the capacity engine (spec §3-§7; invariants C2-C8, C10, C14-C16).

Every demand figure here comes from rows replayed through the real
`simulate()` -- the real `feasible_set` and `Router` -- never from a guess
at where a fixture routes. Shared helpers for the other P3c-4 test modules
live at the top.
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from app.eval.capacity import (DEMANDS, METHOD, NOT_MODELLED, SCHEMA, CapacityError,
                               CapacityPlan, arm_demand, arrival_bucket, bucket_start,
                               build_plan, capacity_block, load_capacity, plan_inputs,
                               sets_volume, states_capacity, volume_for)
from app.eval.proposal import CAPACITY_LIMITS, assumptions_block, normalized_edits
from app.eval.simulate import simulate, summarize
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


@pytest.mark.parametrize("latency", [None, -5.0, float("nan"), float("inf"), "6000", True])
def test_c16_an_unusable_latency_subtracts_nothing(latency):                           # C16
    row = make_row("r", "app-a", ts="2026-09-20T12:01:02+00:00", latency_ms=latency)
    assert bucket_start(arrival_bucket(row, 60), 60) == "2026-09-20T12:01:00+00:00"


def test_c16_buckets_are_epoch_aligned_whatever_the_offset():                          # C16
    """The same instant written with three offsets is one bucket; a naive
    timestamp is read as UTC, as `--since` reads it."""
    same = ["2026-09-20T12:00:30+00:00", "2026-09-20T14:00:30+02:00", "2026-09-20T12:00:30Z",
            "2026-09-20T12:00:30", "2026-09-20T07:00:30-05:00"]
    buckets = {arrival_bucket(make_row("r", "app-a", ts=ts, latency_ms=0.0), 60) for ts in same}
    assert len(buckets) == 1
    assert bucket_start(buckets.pop(), 60) == "2026-09-20T12:00:00+00:00"
    assert arrival_bucket(make_row("r", "app-a", ts=None), 60) is None


def test_c16_rows_without_a_timestamp_are_counted_and_in_no_bucket():                  # C16
    rows = traffic([2] * 12) + [make_row(f"no-ts-{i}", "app-a", ts=None) for i in range(3)]
    block = block_for(rows, one_endpoint(), limits=LIMITS)
    assert block["rows"] == {"simulated": 27, "without_ts": 3, "estimated_tokens": 0,
                             "excluded_not_counted": 0}
    assert block["endpoints"]["local-cheap"]["current"]["requests"] == 24.0


def test_identical_timestamps_are_one_bucket():
    rows = [make_row(f"same-{i}", "app-a", ts="2026-09-20T12:00:30+00:00", latency_ms=1000.0)
            for i in range(50)]
    block = block_for(rows, one_endpoint(), limits=LIMITS, min_buckets=1)
    assert block["window"] == {"first_bucket": "2026-09-20T12:00:00+00:00",
                               "last_bucket": "2026-09-20T12:00:00+00:00",
                               "buckets": 1, "active_buckets": 1}
    stated = block["endpoints"]["local-cheap"]["current"]["limits"]["requests_per_minute"]
    assert stated["peak"] == stated["mean"] == 5.0          # 50 a minute against 10


# --- C14: a pattern whose utilisation can be computed by hand -----------------------

def test_c14_utilisation_matches_the_closed_form():                                    # C14
    """20 minutes at 2 requests a minute, then 10 at 12. Every request takes
    6 s and is 900 prompt + 100 completion tokens. By hand, for the limits
    in `LIMITS` (1 concurrent, 10 req/min, 10,000 tok/min, 500 out tok/min):

        quiet minute: 2 x 6 s / 60 s = 0.2 concurrent      -> 20%
                      2 req/min                             -> 20%
                      2 x 1000 = 2,000 tok/min              -> 20%
                      2 x 100 = 200 out tok/min             -> 40%
        busy minute:  12 x 6 / 60 = 1.2 concurrent         -> 120%
                      12 req/min -> 120%; 12,000 tok/min -> 120%; 1,200 out -> 240%
        mean over 30 minutes: (20 x 2 + 10 x 12) / 30 = 5.333 req/min
                      -> 53.33% (concurrency, requests, tokens), 106.67% (output)
        over 100%:    the 10 busy minutes of 30, holding 120 of 160 requests (75%);
                      for output tokens the same 10 (a quiet minute is at 40%)."""
    block = block_for(traffic(QUIET_THEN_BUSY), one_endpoint(), limits=LIMITS)
    assert block["window"] == {"first_bucket": "2026-09-20T12:00:00+00:00",
                               "last_bucket": "2026-09-20T12:29:00+00:00",
                               "buckets": 30, "active_buckets": 30}
    arm = block["endpoints"]["local-cheap"]["current"]
    assert arm["requests"] == 160.0
    assert (arm["latency_ms"], arm["latency_source"]) == (6000.0, "observed")
    assert arm["demand"]["concurrency"] == pytest.approx({"peak": 1.2, "mean": 160 * 6 / 1800})
    assert arm["demand"]["requests_per_minute"] == pytest.approx({"peak": 12.0, "mean": 160 / 30})
    assert arm["demand"]["tokens_per_minute"] == pytest.approx(
        {"peak": 12000.0, "mean": 160000 / 30})
    assert arm["demand"]["output_tokens_per_minute"] == pytest.approx(
        {"peak": 1200.0, "mean": 16000 / 30})
    for limit, peak, mean in (("max_concurrency", 1.2, 0.5333333),
                              ("requests_per_minute", 1.2, 0.5333333),
                              ("tokens_per_minute", 1.2, 0.5333333),
                              ("output_tokens_per_minute", 2.4, 1.0666667)):
        stated = arm["limits"][limit]
        assert stated["peak"] == pytest.approx(peak), limit
        assert stated["mean"] == pytest.approx(mean), limit
        assert stated["peak_bucket"] == "2026-09-20T12:20:00+00:00", limit
        assert (stated["buckets_over"], stated["buckets_over_share"]) == (10, pytest.approx(1 / 3))
        assert stated["requests_over_share"] == pytest.approx(0.75), limit
    assert block["peak"]["current"] == {"endpoint": "local-cheap",
                                        "limit": "output_tokens_per_minute",
                                        "utilisation": pytest.approx(2.4),
                                        "bucket": "2026-09-20T12:20:00+00:00"}
    assert block["endpoints"]["local-cheap"]["proposed"] == arm          # nothing proposed


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

def _totals(demand: dict) -> list[float]:
    return [sum(load[i] for buckets in demand.values() for load in buckets.values())
            for i in range(4)]


def test_c3_every_routed_row_is_counted_once_and_its_tokens_with_it():                 # C3
    results, _ = replay(mixed_traffic(), three_endpoints(), _DROP_MID)
    assert {r.proposed.outcome for r in results} == {"routed", "rejected"}   # not vacuous
    for arm in ("current", "proposed"):
        routed = [getattr(r, arm) for r in results if getattr(r, arm).outcome == "routed"]
        demand = arm_demand(results, arm, bucket_seconds=60, volume=lambda app_id: 1.0)
        requests, tokens, output, busy = _totals(demand)
        assert requests == len(routed)
        assert tokens == sum(a.prompt_tokens + a.completion_tokens for a in routed)
        assert output == sum(a.completion_tokens for a in routed)
        assert busy == pytest.approx(sum(a.latency_ms for a in routed))
        assert {slug: sum(load[0] for load in buckets.values())
                for slug, buckets in demand.items()} == dict(Counter(a.model for a in routed))
    # The same totals whatever the bucket length.
    for seconds in (1, 7, 3600, 86400):
        again = arm_demand(results, "proposed", bucket_seconds=seconds,
                           volume=lambda app_id: 1.0)
        assert _totals(again)[:3] == _totals(demand)[:3]


def test_c7_a_rejected_row_adds_no_demand():                                           # C7
    results, _ = replay(mixed_traffic(), three_endpoints(), _DROP_MID)
    rejected = [r for r in results if r.proposed.outcome == "rejected"]
    assert len(rejected) == 30 and {r.row["application_id"] for r in rejected} == {"app-b"}
    block = capacity_block(results, CapacityPlan(), _DROP_MID)
    served = sum(entry["proposed"]["requests"] for entry in block["endpoints"].values())
    assert served == len(results) - len(rejected)
    # The buckets the rejected rows arrived in still count as traffic: the
    # export had requests then, whichever arm served them.
    assert block["window"]["active_buckets"] == 60


# --- C4: linear in volume, inversely linear in a limit --------------------------------

@pytest.mark.parametrize("scale", [0.25, 2, 10, 1000])
def test_c4_utilisation_is_linear_in_volume_scale(scale):                              # C4
    rows, snapshot = traffic(QUIET_THEN_BUSY), one_endpoint()
    entry = block_for(rows, snapshot, {"workload": {"volume_scale": scale}},
                      limits=LIMITS)["endpoints"]["local-cheap"]
    current, proposed = entry["current"], entry["proposed"]
    assert proposed["requests"] == pytest.approx(scale * current["requests"])
    for name in DEMANDS:
        for figure in ("peak", "mean"):
            assert proposed["demand"][name][figure] == pytest.approx(
                scale * current["demand"][name][figure])
    for limit in CAPACITY_LIMITS:
        for figure in ("peak", "mean"):
            assert proposed["limits"][limit][figure] == pytest.approx(
                scale * current["limits"][limit][figure])
        # A uniform scale moves no request to another bucket.
        assert proposed["limits"][limit]["peak_bucket"] == current["limits"][limit]["peak_bucket"]


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
        for figure in ("peak", "mean"):
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
    neither the completion, nor the request rate, nor the replayed latency."""
    entry = block_for(traffic(QUIET_THEN_BUSY), one_endpoint(),
                      {"workload": {"token_scale": 0.5}})["endpoints"]["local-cheap"]
    current, proposed = entry["current"]["demand"], entry["proposed"]["demand"]
    assert current["tokens_per_minute"]["peak"] == 12 * 1000
    assert proposed["tokens_per_minute"]["peak"] == 12 * (450 + 100)
    for name in ("concurrency", "requests_per_minute", "output_tokens_per_minute"):
        assert proposed[name] == current[name], name


def test_an_asserted_latency_moves_concurrency_only():
    """Offered concurrency uses the ARM's replayed latency for the endpoint:
    a proposal that asserts 12 s where 6 s was observed doubles it."""
    body = {"endpoints": {"update": {"local-cheap": {"latency_ms": 12000}}}}
    entry = block_for(traffic(QUIET_THEN_BUSY), one_endpoint(), body)["endpoints"]["local-cheap"]
    assert (entry["current"]["latency_ms"], entry["proposed"]["latency_ms"]) == (6000.0, 12000.0)
    assert (entry["current"]["latency_source"], entry["proposed"]["latency_source"]) == (
        "observed", "proposal")
    assert entry["proposed"]["demand"]["concurrency"]["peak"] == pytest.approx(2.4)
    assert entry["current"]["demand"]["concurrency"]["peak"] == pytest.approx(1.2)
    assert (entry["proposed"]["demand"]["requests_per_minute"]
            == entry["current"]["demand"]["requests_per_minute"])


# --- C5, C6: a no-op, and an endpoint that goes away -----------------------------------

def test_c5_a_noop_proposal_gives_identical_demand_in_both_arms():                     # C5
    block = block_for(mixed_traffic(), three_endpoints(), {},
                      limits={"local-mid": {"max_concurrency": 2.0}})
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
    assert (gone["proposed"]["latency_ms"], gone["proposed"]["latency_source"]) == (None, None)
    # No served row was recorded on `local-mid`, so its latency is the tier default.
    assert gone["current"]["latency_source"] == "tier_default"
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
    assert block["window_too_short"] is True and block["window"]["active_buckets"] == 9
    assert block["endpoints"] == {} and block["peak"] == {"current": None, "proposed": None}
    text = json.dumps(block)
    assert "utilisation" not in text and "buckets_over" not in text and "demand" not in text


def test_c8_the_minimum_is_inclusive_and_can_be_lowered():                             # C8
    at = block_for(traffic([3] * 10), one_endpoint(), limits=LIMITS)
    assert at["window_too_short"] is False and at["peak"]["current"] is not None
    one = block_for(traffic([3]), one_endpoint(), limits=LIMITS, min_buckets=1)
    stated = one["endpoints"]["local-cheap"]["current"]["limits"]["requests_per_minute"]
    assert one["window_too_short"] is False and stated["peak"] == stated["mean"] == 0.3


def test_c8_no_rows_at_all():                                                          # C8
    block = capacity_block([], CapacityPlan(limits=LIMITS))
    assert block["window_too_short"] is True and block["endpoints"] == {}
    assert block["window"] == {"first_bucket": None, "last_bucket": None, "buckets": 0,
                               "active_buckets": 0}
    assert block["rows"] == {"simulated": 0, "without_ts": 0, "estimated_tokens": 0,
                             "excluded_not_counted": 0}


def test_a_gap_of_days_does_not_dilute_the_mean():
    """Two sessions a week apart: the window spans 10,090 buckets, 20 of
    them hold traffic, and a mean is over those 20."""
    rows = traffic([6] * 10) + traffic([6] * 10, start=T0 + timedelta(days=7))
    block = block_for(rows, one_endpoint(), limits=LIMITS)
    assert block["window"]["buckets"] == 7 * 24 * 60 + 10
    assert block["window"]["active_buckets"] == 20
    stated = block["endpoints"]["local-cheap"]["current"]["limits"]["requests_per_minute"]
    assert stated["mean"] == pytest.approx(0.6) and stated["peak"] == pytest.approx(0.6)


def test_a_mean_is_over_every_bucket_with_traffic_not_only_the_endpoints_own():
    """`local-mid` is busy in some of the 60 buckets that hold traffic. Its
    mean is over all 60 -- the same denominator for every endpoint and both
    arms -- so an endpoint used for one hour in four does not read as busy
    on average."""
    results, _ = replay(mixed_traffic(), three_endpoints())
    own = arm_demand(results, "current", bucket_seconds=60,
                     volume=lambda app_id: 1.0)["local-mid"]
    requests = sum(load[0] for load in own.values())
    assert 0 < len(own) < 60
    block = capacity_block(results, CapacityPlan(
        limits={"local-mid": {"requests_per_minute": 10.0}}))
    stated = block["endpoints"]["local-mid"]["current"]["limits"]["requests_per_minute"]
    assert block["window"]["active_buckets"] == 60
    assert stated["mean"] == pytest.approx(requests / 60 / 10)
    assert stated["buckets_over_share"] == stated["buckets_over"] / 60


def test_a_longer_bucket_averages_a_burst_away():
    """The same rows in 60 s and in 600 s buckets: the peak falls, the
    total does not. The block says which bucket it used."""
    rows = traffic(QUIET_THEN_BUSY)
    fine = block_for(rows, one_endpoint(), limits=LIMITS, min_buckets=1)
    coarse = block_for(rows, one_endpoint(), limits=LIMITS, min_buckets=1, bucket_seconds=600)
    assert coarse["bucket_seconds"] == 600 and coarse["window"]["active_buckets"] == 3
    f = fine["endpoints"]["local-cheap"]["current"]
    c = coarse["endpoints"]["local-cheap"]["current"]
    assert f["requests"] == c["requests"] == 160.0
    assert f["limits"]["requests_per_minute"]["peak"] == pytest.approx(1.2)
    assert c["limits"]["requests_per_minute"]["peak"] == pytest.approx(1.2)   # a whole busy block
    mixed = block_for(rows, one_endpoint(), limits=LIMITS, min_buckets=1, bucket_seconds=1800)
    assert mixed["endpoints"]["local-cheap"]["current"]["limits"]["requests_per_minute"][
        "peak"] == pytest.approx(160 / 30 / 10)


# --- endpoints with nothing stated, or nothing routed ------------------------------------

def test_an_endpoint_with_a_limit_and_no_rows_is_shown_at_zero():
    block = block_for(traffic([2] * 12), make_snapshot(), limits={
        "local-big": {"max_concurrency": 4.0}})
    arm = block["endpoints"]["local-big"]["current"]
    assert arm["requests"] == 0.0 and arm["latency_ms"] is None
    assert arm["limits"]["max_concurrency"] == {
        "limit": 4.0, "peak": 0.0, "mean": 0.0, "peak_bucket": None, "buckets_over": 0,
        "buckets_over_share": 0.0, "requests_over_share": None}
    assert block["peak"]["current"] == {"endpoint": "local-big", "limit": "max_concurrency",
                                        "utilisation": 0.0, "bucket": None}


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
    assert added["proposed"]["latency_ms"] == 3000.0


def test_a_latency_longer_than_the_bucket_is_flagged():
    rows = traffic([2] * 12, latency_ms=90_000.0)
    short = block_for(rows, one_endpoint())["endpoints"]["local-cheap"]["current"]
    long = block_for(rows, one_endpoint(), bucket_seconds=120,
                     min_buckets=1)["endpoints"]["local-cheap"]["current"]
    assert short["latency_exceeds_bucket"] is True and long["latency_exceeds_bucket"] is False


def test_rows_sized_from_an_estimate_and_rows_left_out_are_counted():
    rows = traffic([2] * 12)
    for row in rows[:5]:                 # served, but the backend reported no usage
        row.update(prompt_tokens=0, completion_tokens=None, routing_tokens=800, max_tokens=64)
    results, _ = replay(rows, one_endpoint())
    block = capacity_block(results, CapacityPlan(), excluded={
        "cache_hit": 3, "before_since": 2, "missing_features": 4, "unknown_app": 1,
        "unknown_status": 0, "missing_routing_tokens": 0})
    assert block["rows"]["estimated_tokens"] == 5 and block["rows"]["excluded_not_counted"] == 5
    estimated = [r.current for r in results if r.row.get("routing_tokens") == 800]
    assert {(a.prompt_tokens, a.completion_tokens) for a in estimated} == {(800, 64)}


# --- C2, C10: deterministic; nothing ranks or prescribes ---------------------------------

def test_c2_the_block_is_deterministic_and_independent_of_row_order():                 # C2
    results, excluded = replay(mixed_traffic(), three_endpoints(), _DROP_MID)
    plan = CapacityPlan(limits={"local-mid": {"max_concurrency": 2.0}},
                        volume={"*": 3.0})
    first = capacity_block(results, plan, _DROP_MID, excluded=excluded)
    again = capacity_block(results, plan, _DROP_MID, excluded=excluded)
    assert json.dumps(first, sort_keys=True) == json.dumps(again, sort_keys=True)
    assert capacity_block(results[::-1], plan, _DROP_MID, excluded=excluded) == first


def test_c10_no_key_ranks_recommends_or_prescribes():                                  # C10
    block = block_for(traffic(QUIET_THEN_BUSY), one_endpoint(),
                      {"workload": {"volume_scale": 2}}, limits=LIMITS)
    assert block["method"] == METHOD == "bucketed_offered_load"
    assert block["not_modelled"] == list(NOT_MODELLED)
    words = keys_of(block) + list(NOT_MODELLED)
    assert not [w for w in words if any(b in w.lower() for b in CAPACITY_BANNED)]
    # Exactly these keys: there is no overflow, rejection or remedy field.
    assert set(block) == {"method", "bucket_seconds", "min_buckets", "window", "rows",
                          "volume_scale", "not_modelled", "window_too_short", "endpoints",
                          "peak"}
    assert set(block["endpoints"]["local-cheap"]["current"]) == {
        "requests", "latency_ms", "latency_source", "latency_exceeds_bucket", "demand",
        "limits"}
    assert set(block["endpoints"]["local-cheap"]["current"]["limits"]["max_concurrency"]) == {
        "limit", "peak", "mean", "peak_bucket", "buckets_over", "buckets_over_share",
        "requests_over_share"}
