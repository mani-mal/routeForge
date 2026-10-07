"""P3c-4: what a report says about capacity (spec §8, §9; invariants C8-C10,
C12, C20)."""
from __future__ import annotations

import re
from datetime import timedelta

from app.eval.capacity import (AVERAGES_NOTE, CAPACITY_CAVEAT, NO_RANGE_NOTE, NO_REMEDY_NOTE,
                               NOT_MODELLED, PEAK_COLUMN_NOTE, CapacityPlan, capacity_block,
                               caveat_lines, peak_text, render_capacity_section, too_short_text)
from app.eval.outcomes import _round6
from tests.eval.sim_fixtures import make_row
from tests.eval.test_capacity import (CAPACITY_BANNED, LIMITS, QUIET_THEN_BUSY, T0, block_for,
                                      mixed_traffic, one_endpoint, replay, synthetic,
                                      three_endpoints, traffic)


def _block(body=None, **plan_kw) -> dict:
    plan_kw.setdefault("limits", LIMITS)
    return _round6(block_for(traffic(QUIET_THEN_BUSY), one_endpoint(), body, **plan_kw))


def test_c12_the_caveat_is_the_spec_text():                                            # C12
    """Pinned word for word: it is the statement of what a capacity figure
    is. Editing it means editing spec §9.1 too."""
    assert CAPACITY_CAVEAT == (
        "**Stated capacity against replayed demand.** Every limit below is a figure the operator\n"
        "stated; RouteForge has not measured it and does not manage capacity. Demand is the replayed\n"
        "route of each recorded request. A request arrives at its estimated arrival time (the time\n"
        "the record was written minus its recorded latency) and is in service from then for its\n"
        "service time: requests and tokens are counted in the bucket of arrival, service time in\n"
        "every bucket it runs through. Service time is the request's own recorded latency where\n"
        "replay serves it on the endpoint that served it alone; otherwise that endpoint's mean\n"
        "recorded latency; otherwise replay's latency for the endpoint. A latency the proposal\n"
        "asserts replaces all three. A recorded latency includes the time RouteForge spent before\n"
        "and after the backend call, and for a request that failed over it cannot be attributed to\n"
        "one endpoint, so it is not used. A peak is a maximum over buckets: it grows with a longer\n"
        "window and with a shorter bucket even for a steady stream, so every peak is printed with\n"
        "the average over the whole window. Nothing here models queueing, batching, autoscaling, the\n"
        "serving engine's real concurrency behaviour, load that reaches an endpoint without passing\n"
        "through RouteForge, bursts shorter than a bucket, or the load of failover attempts.\n"
        "RouteForge does not know what an endpoint does above a stated limit (queue, shed or slow\n"
        "down), so no rejection and no delay is predicted. An export of a few buckets says little."
    )
    assert len(caveat_lines()) == 17 and all(line.startswith("> ") for line in caveat_lines())
    assert NOT_MODELLED == ("queueing", "batching", "autoscaling", "engine_concurrency",
                            "external_load", "sub_bucket_bursts", "failover_load",
                            "moved_request_latency", "overload_behaviour")


def test_c12_the_section_opens_with_the_caveat_and_the_no_remedy_sentence():           # C12
    text = render_capacity_section(_block())
    lines = text.split("\n")
    assert lines[0] == "## Capacity" and lines[1] == ""
    assert lines[2:19] == caveat_lines() and lines[19] == ""
    assert lines[20] == NO_REMEDY_NOTE
    assert ("Buckets of 60 s. The window is 30 bucket(s), from the first estimated arrival to "
            "the end of the last recorded request (2026-09-20T12:00:00+00:00 to "
            "2026-09-20T12:29:00+00:00); a request arrived in 30 of them; 160 simulated "
            "row(s).") in text
    assert AVERAGES_NOTE in text
    assert text.endswith("|\n") and not text.endswith("\n\n")


def test_the_averages_note_names_both_denominators():
    assert AVERAGES_NOTE == (
        "`avg (window)` is over every bucket of the window, empty ones included: the "
        "time-average. `avg (active)` is over the buckets in which the endpoint had an arrival "
        "or a request in service; that count is printed with it.")


def test_the_section_shows_each_stated_limit_per_arm_and_the_demand():
    text = render_capacity_section(_block({"workload": {"volume_scale": 2}}))
    assert ("Volume scale assumed by the proposal (proposed arm only, multiplying any stated "
            "for both arms): *=2.") in text
    assert ("Peak load: current peak 240.0% / avg 106.7% (local-cheap, "
            "output_tokens_per_minute, 60 s buckets); proposed peak 480.0% / avg 213.3% "
            "(local-cheap, output_tokens_per_minute, 60 s buckets).") in text
    assert "### local-cheap" in text
    assert ("Requests: current 160, proposed 320. Active buckets (an arrival or a request in "
            "service): current 30 of 30, proposed 30 of 30.") in text
    assert ("Service time: current mean 6000 ms, longest 6000 ms over 160 row(s) (160 "
            "recorded); proposed mean 6000 ms, longest 6000 ms over 160 row(s) (160 "
            "recorded).") in text
    assert "tier default" not in text and "**Note:**" not in text
    assert ("| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | "
            "requests arriving in those buckets | peak bucket |") in text
    assert ("| max_concurrency | current | 1 | 120.0% | 53.3% | 53.3% | 10 of 30 | 75.0% | "
            "2026-09-20T12:20:00+00:00 |") in text
    assert ("| max_concurrency | proposed | 1 | 240.0% | 106.7% | 106.7% | 10 of 30 | 75.0% | "
            "2026-09-20T12:20:00+00:00 |") in text
    assert "| demand | arm | peak | avg (window) | avg (active) |" in text
    assert "| concurrency | current | 1.2 | 0.533333 | 0.533333 |" in text
    assert "| concurrency | proposed | 2.4 | 1.06667 | 1.06667 |" in text
    assert "| tokens_per_minute | proposed | 24000 | 10666.7 | 10666.7 |" in text
    order = [text.index(f"| {limit} | current |") for limit in LIMITS["local-cheap"]]
    assert order == sorted(order)


def test_a_sparse_export_reads_as_sparse():
    """The review's example, as a reader sees it: 12 requests two hours
    apart, each 138 s, one concurrent request allowed. The peak is 100% (one
    request in service for a whole minute) and the average over the window
    is 2.1% -- not "230%, every bucket over"."""
    requests = [(7200 * i + 10, 138.0) for i in range(12)]
    block = _round6(capacity_block(synthetic(requests), CapacityPlan(
        limits={"e": {"max_concurrency": 1.0}})))
    text = render_capacity_section(block)
    assert ("Peak load: current peak 100.0% / avg 2.1% (e, max_concurrency, 60 s buckets); "
            "proposed peak 100.0% / avg 2.1% (e, max_concurrency, 60 s buckets).") in text
    assert "The window is 1323 bucket(s)" in text and "a request arrived in 12 of them" in text
    assert "current 36 of 1323, proposed 36 of 1323." in text
    assert "| max_concurrency | current | 1 | 100.0% | 2.1% | 76.7% | 0 of 1323 | 0.0% |" in text
    assert "230" not in text


def test_a_base_volume_scale_is_stated_once_for_both_arms():
    text = render_capacity_section(_block(volume={"*": 10.0, "app-b": 3.0}))
    assert "Volume scale stated for both arms: *=10, app-b=3." in text
    assert "assumed by the proposal" not in text


def test_c8_a_short_window_prints_the_state_and_no_figure():                           # C8
    block = _round6(block_for(traffic([3] * 4), one_endpoint(), limits=LIMITS))
    assert too_short_text(block) == "window too short (4 of the 10 buckets needed)"
    assert peak_text(block, "proposed") == peak_text(block, "current") == too_short_text(block)
    text = render_capacity_section(block)
    assert ("No utilisation figure: window too short (4 of the 10 buckets needed). A bucket "
            "counts when a request arrived in it; `--capacity-min-buckets` sets the number "
            "needed.") in text
    assert "%" not in text and "| limit |" not in text and "###" not in text
    assert "Peak load:" not in text


def test_an_endpoint_with_no_stated_limit_prints_demand_only():
    block = _round6(block_for(traffic(QUIET_THEN_BUSY), one_endpoint()))
    text = render_capacity_section(block)
    assert "Peak load: current not stated; proposed not stated." in text
    assert "No limit is stated for this endpoint; demand only." in text
    assert "| limit |" not in text
    assert "| requests_per_minute | current | 12 | 5.33333 | 5.33333 |" in text
    assert "| requests_per_minute | proposed | 12 | 5.33333 | 5.33333 |" in text


def test_a_limit_stated_in_one_arm_only_says_so_in_the_other():
    body = {"capacity": {"local-cheap": {"max_concurrency": 2}}}
    block = _round6(block_for(traffic(QUIET_THEN_BUSY), one_endpoint(), body))
    text = render_capacity_section(block)
    assert ("| max_concurrency | current | not stated | n/a | n/a | n/a | n/a | n/a | n/a |"
            in text)
    assert "| max_concurrency | proposed | 2 | 60.0% | 26.7% | 26.7% |" in text
    assert ("Peak load: current not stated; proposed peak 60.0% / avg 26.7% (local-cheap, "
            "max_concurrency, 60 s buckets).") in text


def test_an_endpoint_no_row_reaches_prints_zero_and_no_peak_bucket():
    block = _round6(block_for(mixed_traffic(), three_endpoints(),
                              {"endpoints": {"remove": ["local-mid"]}},
                              limits={"local-mid": {"max_concurrency": 2.0}}))
    text = render_capacity_section(block)
    assert ("| max_concurrency | proposed | 2 | 0.0% | 0.0% | 0.0% | 0 of 195 | n/a | n/a |"
            in text)
    assert "proposed none (no row routed there)." in text
    assert ("**Note:** 100.0% of the current arm's service time on this endpoint is replay's "
            "latency for the endpoint (tier_default), not a recorded request's: no simulated "
            "row was served by it alone. A tier default is not a measurement: that part of "
            "the offered concurrency rests on the default alone.") in text
    assert "of the proposed arm's service time on this endpoint is replay's" not in (
        text.split("### local-mid")[1])


def test_a_small_share_from_replays_latency_gets_no_sentence():
    """The sentence is for a material share (a tenth or more). An endpoint
    whose service time is recorded, or its own mean, prints none."""
    rows = traffic([2] * 12)
    for row in rows[:6]:                         # failed over: the endpoint's mean instead
        row.update(attempts=["local-gone", "local-cheap"], chosen="local-cheap")
    text = render_capacity_section(_round6(block_for(rows, one_endpoint())))
    assert "(18 recorded, 6 endpoint_mean)" in text
    assert "replay's latency for the endpoint" not in text.split(NO_REMEDY_NOTE)[1]


def test_notes_for_rows_that_are_not_in_the_demand():
    block = _block()
    block["rows"].update(without_ts=3, excluded_not_counted=4, unusable_latency=2)
    block["endpoints"]["local-cheap"]["proposed"]["estimated_token_rows"] = 5
    text = render_capacity_section(block)
    assert "**Note:** 3 simulated row(s) have no timestamp and are in no bucket" in text
    assert "**Note:** 4 excluded row(s) (see \"Excluded rows\") may have loaded an endpoint" in text
    assert ("**Note:** 2 simulated row(s) have no usable recorded latency (missing, negative, "
            "or above 24 hours): each is placed at the time it was written, and its service "
            "time is not its own.") in text
    assert ("**Note:** 5 row(s) routed here in the proposed arm have no backend token count; "
            "their token demand is RouteForge's own estimate.") in text
    assert "routed here in the current arm" not in text
    assert "**Note:**" not in render_capacity_section(_block())


def test_service_time_past_the_window_is_said():
    body = {"endpoints": {"update": {"local-cheap": {"latency_ms": 600_000}}}}
    results, _ = replay(traffic([2] * 12), one_endpoint(), body)
    block = _round6(capacity_block(results, CapacityPlan(), body))
    after = block["endpoints"]["local-cheap"]["proposed"]["service_ms_after_window"]
    text = render_capacity_section(block)
    assert after > 0 and text.count("falls after the window's last bucket") == 1
    assert (f"**Note:** {after:.6g} ms of the proposed arm's service time on this endpoint "
            "falls after the window's last bucket and is in no bucket.") in text
    assert "(24 asserted)" in text and "(24 recorded)" in text


def test_a_response_longer_than_a_bucket_gets_no_warning():
    """It is spread over the buckets it runs through; there is nothing to
    warn about, and no advice to change the bucket."""
    rows = [make_row(f"long-{m}", "app-a", latency_ms=90_000.0,
                     ts=(T0 + timedelta(minutes=m, seconds=120)).isoformat())
            for m in range(12)]
    text = render_capacity_section(_round6(block_for(rows, one_endpoint())))
    assert "longer than a bucket" not in text and "**Note:**" not in text
    assert "mean 90000 ms, longest 90000 ms over 12 row(s) (12 recorded)" in text
    assert "| concurrency | current | 1.5 | 1.38461 | 1.38461 |" in text


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
             render_capacity_section(_round6(block_for(
                 mixed_traffic(), three_endpoints(), {"endpoints": {"remove": ["local-mid"]}},
                 limits={"local-mid": {"max_concurrency": 2.0}}))),
             CAPACITY_CAVEAT, NO_REMEDY_NOTE, NO_RANGE_NOTE, PEAK_COLUMN_NOTE, AVERAGES_NOTE]
    for text in texts:
        assert not [word for word in CAPACITY_BANNED if word in text.lower()], text[:60]
    assert NO_REMEDY_NOTE == ("This section reports load against stated limits and proposes no "
                              "remedy: not more capacity, and not a change to any policy or "
                              "protection.")
    assert "does not make a scenario the one to choose" in PEAK_COLUMN_NOTE
    assert "read it with the average" in PEAK_COLUMN_NOTE


def unpaired_peaks(text: str) -> list[str]:
    """Every line of a report that prints a peak utilisation -- a
    percentage in a "peak" sentence, cell or limits-table row -- without an
    average beside it. Shared with the comparison report's test."""
    bad = []
    for line in text.split("\n"):
        if "%" not in line:
            continue
        if line.startswith("| ") and re.match(r"\| \w+ \| (current|proposed) \| ", line):
            # A limits-table row: stated | peak | avg (window) | avg (active) | ...
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if not (cells[3].endswith("%") and cells[4].endswith("%")
                    and cells[5].endswith("%")):
                bad.append(line)
        elif "peak" in line.lower():
            for found in re.finditer(r"peak (\d[\d.]*%)", line):
                if not line[found.end():].startswith(" / avg "):
                    bad.append(line)
    return bad


def test_c20_no_sentence_prints_a_peak_without_the_average():                          # C20
    blocks = [_block({"workload": {"volume_scale": 5}}),
              _round6(block_for(mixed_traffic(), three_endpoints(),
                                {"endpoints": {"remove": ["local-mid"]}},
                                limits={"local-mid": {"max_concurrency": 2.0},
                                        "local-big": {"requests_per_minute": 5.0}}))]
    for block in blocks:
        text = render_capacity_section(block)
        assert text.count(" / avg ") >= 2 and unpaired_peaks(text) == []
        for arm in ("current", "proposed"):
            assert re.fullmatch(r"peak [\d.]+% / avg [\d.]+% \(.+, \w+, 60 s buckets\)",
                                peak_text(block, arm))
    # The detector is not vacuous.
    assert unpaired_peaks("Peak load: current peak 230.0% (e, max_concurrency).") != []
    assert unpaired_peaks("| max_concurrency | current | 1 | 230.0% | n/a | n/a | 1 of 2 |") != []
