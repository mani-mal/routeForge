"""P3c-4: what a report says about capacity (spec §8, §9; invariants C8-C10, C12)."""
from __future__ import annotations

from app.eval.capacity import (CAPACITY_CAVEAT, NO_RANGE_NOTE, NO_REMEDY_NOTE, NOT_MODELLED,
                               PEAK_COLUMN_NOTE, caveat_lines, peak_text,
                               render_capacity_section, too_short_text)
from app.eval.outcomes import _round6
from tests.eval.test_capacity import (CAPACITY_BANNED, LIMITS, QUIET_THEN_BUSY, block_for,
                                      mixed_traffic, one_endpoint, three_endpoints, traffic)


def _block(body=None, **plan_kw) -> dict:
    plan_kw.setdefault("limits", LIMITS)
    return _round6(block_for(traffic(QUIET_THEN_BUSY), one_endpoint(), body, **plan_kw))


def test_c12_the_caveat_is_the_spec_text():                                            # C12
    """Pinned word for word: it is the statement of what a capacity figure
    is. Editing it means editing spec §9.1 too."""
    assert CAPACITY_CAVEAT == (
        "**Stated capacity against replayed demand.** Every limit below is a figure the operator\n"
        "stated; RouteForge has not measured it and does not manage capacity. Demand is the replayed\n"
        "route of each recorded request, grouped into buckets by estimated arrival time (the time\n"
        "the record was written minus its recorded latency). Nothing here models queueing, batching,\n"
        "autoscaling, the serving engine's real concurrency behaviour, load that reaches an endpoint\n"
        "without passing through RouteForge, bursts shorter than a bucket, or the load of failover\n"
        "attempts. Replay gives each endpoint one latency, so offered concurrency is the request\n"
        "rate times that latency. RouteForge does not know what an endpoint does above a stated\n"
        "limit (queue, shed or slow down), so no rejection and no delay is predicted. An export of\n"
        "a few buckets says little."
    )
    assert len(caveat_lines()) == 10 and all(line.startswith("> ") for line in caveat_lines())
    assert NOT_MODELLED == ("queueing", "batching", "autoscaling", "engine_concurrency",
                            "external_load", "sub_bucket_bursts", "failover_load",
                            "per_request_latency", "overload_behaviour")


def test_c12_the_section_opens_with_the_caveat_and_the_no_remedy_sentence():           # C12
    text = render_capacity_section(_block())
    lines = text.split("\n")
    assert lines[0] == "## Capacity" and lines[1] == ""
    assert lines[2:12] == caveat_lines() and lines[12] == ""
    assert lines[13] == NO_REMEDY_NOTE
    assert ("Buckets of 60 s by estimated arrival time. 30 of 30 bucket(s) in the window hold "
            "traffic (2026-09-20T12:00:00+00:00 to 2026-09-20T12:29:00+00:00); 160 simulated "
            "row(s). Means are over the buckets that hold traffic.") in text
    assert text.endswith("|\n") and not text.endswith("\n\n")


def test_the_section_shows_each_stated_limit_per_arm_and_the_demand():
    text = render_capacity_section(_block({"workload": {"volume_scale": 2}}))
    assert ("Volume scale assumed by the proposal (proposed arm only, multiplying any stated "
            "for both arms): *=2.") in text
    assert ("Peak load: current 240.0% (local-cheap, output_tokens_per_minute); proposed "
            "480.0% (local-cheap, output_tokens_per_minute).") in text
    assert "### local-cheap" in text
    assert ("Requests: current 160, proposed 320. Replayed latency: current 6000 ms (observed), "
            "proposed 6000 ms (observed).") in text
    assert "tier default" not in text
    assert ("| max_concurrency | current | 1 | 120.0% | 53.3% | 10 of 30 | 75.0% | "
            "2026-09-20T12:20:00+00:00 |") in text
    assert ("| max_concurrency | proposed | 2 | 240.0% | 106.7% | 10 of 30 | 75.0% | "
            "2026-09-20T12:20:00+00:00 |") in text
    assert "| concurrency | 1.2 | 0.533333 | 2.4 | 1.06667 |" in text
    assert "| tokens_per_minute | 12000 | 5333.33 | 24000 | 10666.7 |" in text
    order = [text.index(f"| {limit} | current |") for limit in LIMITS["local-cheap"]]
    assert order == sorted(order)


def test_a_base_volume_scale_is_stated_once_for_both_arms():
    text = render_capacity_section(_block(volume={"*": 10.0, "app-b": 3.0}))
    assert "Volume scale stated for both arms: *=10, app-b=3." in text
    assert "assumed by the proposal" not in text


def test_c8_a_short_window_prints_the_state_and_no_figure():                           # C8
    block = _round6(block_for(traffic([3] * 4), one_endpoint(), limits=LIMITS))
    assert too_short_text(block) == "window too short (4 of the 10 buckets needed)"
    assert peak_text(block, "proposed") == peak_text(block, "current") == too_short_text(block)
    text = render_capacity_section(block)
    assert ("No utilisation figure: window too short (4 of the 10 buckets needed). "
            "`--capacity-min-buckets` sets the number needed.") in text
    assert "%" not in text and "| limit |" not in text and "###" not in text
    assert "Peak load:" not in text


def test_an_endpoint_with_no_stated_limit_prints_demand_only():
    block = _round6(block_for(traffic(QUIET_THEN_BUSY), one_endpoint()))
    text = render_capacity_section(block)
    assert "Peak load: current not stated; proposed not stated." in text
    assert "No limit is stated for this endpoint; demand only." in text
    assert "| limit |" not in text
    assert "| requests_per_minute | 12 | 5.33333 | 12 | 5.33333 |" in text


def test_a_limit_stated_in_one_arm_only_says_so_in_the_other():
    body = {"capacity": {"local-cheap": {"max_concurrency": 2}}}
    block = _round6(block_for(traffic(QUIET_THEN_BUSY), one_endpoint(), body))
    text = render_capacity_section(block)
    assert "| max_concurrency | current | not stated | n/a | n/a | n/a | n/a | n/a |" in text
    assert "| max_concurrency | proposed | 2 | 60.0% |" in text
    assert "Peak load: current not stated; proposed 60.0% (local-cheap, max_concurrency)." in text


def test_an_endpoint_no_row_reaches_prints_zero_and_no_peak_bucket():
    block = _round6(block_for(mixed_traffic(), three_endpoints(),
                              {"endpoints": {"remove": ["local-mid"]}},
                              limits={"local-mid": {"max_concurrency": 2.0}}))
    text = render_capacity_section(block)
    assert "| max_concurrency | proposed | 2 | 0.0% | 0.0% | 0 of 60 | n/a | n/a |" in text
    assert "proposed none (no row routed there)." in text
    # Nothing was recorded on `local-mid`: its latency is a default, and the report says so.
    assert ("**Note:** the current arm's latency for this endpoint is a tier default, not a "
            "measurement: its offered concurrency rests on that default alone.") in text


def test_notes_for_rows_that_are_not_in_the_demand():
    block = _block()
    block["rows"].update(without_ts=3, excluded_not_counted=4, estimated_tokens=5)
    text = render_capacity_section(block)
    assert "**Note:** 3 simulated row(s) have no timestamp and are in no bucket" in text
    assert "**Note:** 4 excluded row(s) (see \"Excluded rows\") may have loaded an endpoint" in text
    assert "**Note:** 5 row(s) have no backend token count" in text
    assert "**Note:**" not in render_capacity_section(_block())


def test_a_latency_longer_than_the_bucket_is_said():
    block = _round6(block_for(traffic([2] * 12, latency_ms=90_000.0), one_endpoint()))
    text = render_capacity_section(block)
    assert text.count("replayed latency is longer than a bucket") == 2       # both arms
    assert "A longer `--capacity-bucket` avoids that." in text


def test_c9_with_ranges_in_the_same_report_capacity_says_why_it_has_none():            # C9
    plain = render_capacity_section(_block())
    ranged = render_capacity_section(_block(), ranged=True)
    assert NO_RANGE_NOTE not in plain and NO_RANGE_NOTE in ranged
    assert ranged.replace(NO_RANGE_NOTE + "\n\n", "") == plain
    assert "[" not in ranged.replace("[lo, hi]", "")          # no bracketed range anywhere
    assert NO_RANGE_NOTE.startswith("Capacity figures are points and carry no range.")


def test_c10_no_sentence_ranks_recommends_or_prescribes():                             # C10
    """No remedy, no ordering, and never a protection named as the thing to
    change because capacity is short."""
    texts = [render_capacity_section(_block({"workload": {"volume_scale": 5}}), ranged=True),
             render_capacity_section(_round6(block_for(traffic([3] * 4), one_endpoint()))),
             CAPACITY_CAVEAT, NO_REMEDY_NOTE, NO_RANGE_NOTE, PEAK_COLUMN_NOTE]
    for text in texts:
        assert not [word for word in CAPACITY_BANNED if word in text.lower()], text[:60]
    assert NO_REMEDY_NOTE == ("This section reports load against stated limits and proposes no "
                              "remedy: not more capacity, and not a change to any policy or "
                              "protection.")
    assert "does not make a scenario the one to choose" in PEAK_COLUMN_NOTE
