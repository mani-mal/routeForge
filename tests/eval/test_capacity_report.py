"""P3c-4: what a report says about capacity (spec §8, §9; invariants C8-C10,
C12, C20)."""
from __future__ import annotations

import re
from datetime import timedelta

from app.eval.capacity import (AVERAGES_NOTE, CAPACITY_CAVEAT, NO_RANGE_NOTE, NO_REMEDY_NOTE,
                               NOT_MODELLED, PEAK_COLUMN_NOTE, VOLUME_NOTE, CapacityPlan,
                               capacity_block, caveat_lines, peak_text, render_capacity_section,
                               too_short_text)
from app.eval.outcomes import _round6
from tests.eval.sim_fixtures import make_row
from tests.eval.test_capacity import (CAPACITY_BANNED, LIMITS, QUIET_THEN_BUSY, T0, _poisson,
                                      arm_on, block_for, mixed_traffic, one_endpoint, replay,
                                      synthetic, three_endpoints, traffic)


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
    assert ("Peak load: current peak 240.0% / avg 106.7%, over 100% in 33.3% of 30 buckets "
            "(local-cheap, output_tokens_per_minute, 60 s buckets); proposed peak 480.0% / "
            "avg 213.3%, over 100% in 33.3% of 30 buckets (local-cheap, "
            "output_tokens_per_minute, 60 s buckets).") in text
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
    assert ("Peak load: current peak 100.0% / avg 2.1%, over 100% in 0.0% of 1323 buckets "
            "(e, max_concurrency, 60 s buckets); proposed peak 100.0% / avg 2.1%, over 100% "
            "in 0.0% of 1323 buckets (e, max_concurrency, 60 s buckets).") in text
    assert "The window is 1323 bucket(s)" in text and "a request arrived in 12 of them" in text
    assert "current 36 of 1323, proposed 36 of 1323." in text
    assert "| max_concurrency | current | 1 | 100.0% | 2.1% | 76.7% | 0 of 1323 | 0.0% |" in text
    assert "230" not in text


def test_a_base_volume_scale_is_stated_once_for_both_arms():
    text = render_capacity_section(_block(volume={"*": 10.0, "app-b": 3.0}))
    assert "Volume scale stated for both arms: *=10, app-b=3." in text
    assert "assumed by the proposal" not in text


def test_a_volume_scale_is_said_to_be_a_count_not_an_arrival_model():
    """Pinned: whenever any volume scale is not 1, the section says once
    what a scale does and what it does not."""
    assert VOLUME_NOTE == (
        "A volume scale counts each recorded request that many times, at its recorded time; "
        "it does not model how more traffic would arrive.")
    base = {"volume": {"*": 10.0, "app-b": 3.0}}
    proposed = {"workload": {"volume_scale": 2}}
    for text in (render_capacity_section(_block(proposed)),
                 render_capacity_section(_block(**base)),
                 render_capacity_section(_block(proposed, **base)),
                 render_capacity_section(_block({"workload": {"apps": {
                     "app-a": {"volume_scale": 0.5}}}})),
                 render_capacity_section(_block(proposed), ranged=True)):
        assert text.count(VOLUME_NOTE) == 1
    for text in (render_capacity_section(_block()),
                 render_capacity_section(_block({"workload": {"volume_scale": 1}})),
                 render_capacity_section(_block({"workload": {"token_scale": 0.5}})),
                 render_capacity_section(_block(volume={"*": 1.0}))):
        assert VOLUME_NOTE not in text


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
    assert ("Peak load: current not stated; proposed peak 60.0% / avg 26.7%, over 100% in "
            "0.0% of 30 buckets (local-cheap, max_concurrency, 60 s buckets).") in text


def test_an_endpoint_no_row_reaches_prints_zero_and_no_peak_bucket():
    block = _round6(block_for(mixed_traffic(), three_endpoints(),
                              {"endpoints": {"remove": ["local-mid"]}},
                              limits={"local-mid": {"max_concurrency": 2.0}}))
    text = render_capacity_section(block)
    assert ("| max_concurrency | proposed | 2 | 0.0% | 0.0% | 0.0% | 0 of 195 | n/a | n/a |"
            in text)
    assert "proposed none (no row routed there)." in text
    # The current arm's rows there have only a tier default for a service time.
    assert ("| max_concurrency | current | 2 | n/a (no service time: no recorded request or "
            "latency signal for local-mid) | n/a | n/a | n/a | n/a | n/a |") in text
    assert ("**Note:** the current arm's service time on this endpoint is a tier default: no "
            "simulated row was served by it alone and the snapshot has no latency signal for "
            "it. A tier default is a routing constant, not a service time, so no concurrency "
            "figure is computed for this endpoint in this arm. Requests and tokens are "
            "counted as usual.") in text
    assert "the proposed arm's service time on this endpoint is" not in (
        text.split("### local-mid")[1])


def _moved_to_a_tier_default(limits: dict, body: dict | None = None) -> dict:
    rows = synthetic([(10 * i + 1, 10.0) for i in range(180)], endpoint_ms=10_000.0,
                     proposed=arm_on("new", 200.0, "tier_default"))
    return _round6(capacity_block(rows, CapacityPlan(limits=limits), body))


def test_a_tier_default_prints_a_state_and_never_a_concurrency_percentage():
    """Traffic moved to an endpoint whose only latency is a tier default:
    the section, the headline and the comparison cell (both are
    `peak_text`) print no concurrency percentage for it -- the limit row
    names the input that is missing, and the headline says which endpoint
    it leaves out."""
    block = _moved_to_a_tier_default({"e": {"max_concurrency": 1.0},
                                      "new": {"max_concurrency": 1.0}})
    text = render_capacity_section(block)
    section = text.split("### new")[1]
    assert ("| max_concurrency | proposed | 1 | n/a (no service time: state "
            "endpoints.new.latency_ms) | n/a | n/a | n/a | n/a | n/a |") in section
    assert "| max_concurrency | current | 1 | 0.0% | 0.0% | 0.0% |" in section   # no row there
    assert "| concurrency | proposed | n/a | n/a | n/a |" in section
    assert "| requests_per_minute | proposed | 6 | " in section
    assert ("**Note:** the proposed arm's service time on this endpoint is a tier default: no "
            "simulated row was served by it alone and the snapshot has no latency signal for "
            "it. A tier default is a routing constant, not a service time, so no concurrency "
            "figure is computed for this endpoint in this arm. Requests and tokens are "
            "counted as usual.") in section
    assert not [line for line in section.split("\n") if "proposed" in line and "%" in line]
    headline = peak_text(block, "proposed")
    assert headline.endswith("; concurrency not computed for new")
    assert "(new," not in headline and "2.0%" not in text and "1.9%" not in text
    assert f"proposed {headline}." in text
    assert unpaired_peaks(text) == []


def test_a_tier_default_alone_leaves_no_headline_and_says_so():
    block = _moved_to_a_tier_default({}, {"capacity": {"new": {"max_concurrency": 1}}})
    assert peak_text(block, "proposed") == "n/a; concurrency not computed for new"
    assert peak_text(block, "current") == "not stated"
    text = render_capacity_section(block)
    assert ("Peak load: current not stated; proposed n/a; concurrency not computed for "
            "new.") in text
    # No percentage at all for `new`, bar the limits table's own header.
    assert "%" not in text.split("### new")[1].replace("| buckets over 100% |", "")


def test_the_current_arm_cannot_be_given_a_latency_so_its_state_names_no_proposal_key():
    """A proposal's `latency_ms` is the proposed arm's. In the current arm
    the missing input is a recorded request or a latency signal; the row
    says that, and does not point at a key that would change nothing."""
    block = _round6(block_for(mixed_traffic(), three_endpoints(), {},
                              limits={"local-mid": {"max_concurrency": 2.0}}))
    section = render_capacity_section(block).split("### local-mid")[1]
    assert ("| max_concurrency | current | 2 | n/a (no service time: no recorded request or "
            "latency signal for local-mid) | n/a | n/a | n/a | n/a | n/a |") in section
    assert ("| max_concurrency | proposed | 2 | n/a (no service time: state "
            "endpoints.local-mid.latency_ms) | n/a | n/a | n/a | n/a | n/a |") in section
    assert section.count("is a tier default: no simulated row was served by it alone") == 2


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
    """As a share of the arm's service time and in seconds -- never a raw
    millisecond count -- and with what it does to the figures."""
    body = {"endpoints": {"update": {"local-cheap": {"latency_ms": 600_000}}}}
    results, _ = replay(traffic([2] * 12), one_endpoint(), body)
    block = _round6(capacity_block(results, CapacityPlan(), body))
    proposed = block["endpoints"]["local-cheap"]["proposed"]
    # Two requests a minute, at seconds 5 and 9, for 12 minutes; the window
    # ends at 720 s. From minute 2 on a pair runs 120m - 226 s past it:
    # 5,540 s of 24 x 600 = 14,400 s.
    assert proposed["service_ms_after_window"] == 5_540_000.0
    assert proposed["service_after_window_share"] == 0.384722
    text = render_capacity_section(block)
    assert text.count("falls after the window's last bucket") == 1
    assert ("**Note:** 38.5% of the proposed arm's service time on this endpoint (5,540 s) "
            "falls after the window's last bucket and is in no bucket, so this arm's average "
            "on this endpoint is understated and its peak may be.") in text
    assert "(24 asserted)" in text and "(24 recorded)" in text
    assert " ms of the proposed arm" not in text


def _asserted(seconds: float, limits: dict | None = None) -> dict:
    """30 minutes, a request every 10 s recorded at 6 s; the proposal
    asserts `seconds` for the endpoint."""
    rows = synthetic([(10 * i + 1, 6.0) for i in range(180)],
                     proposed=arm_on("e", seconds * 1000.0, "proposal"))
    return _round6(capacity_block(rows, CapacityPlan(
        limits={"e": {"max_concurrency": 4.0}} if limits is None else limits)))


def test_a_material_share_after_the_window_is_in_the_headline():
    """A tenth or more of an arm's service time past the window means its
    concurrency is understated by about that much: the headline and the
    comparison cell (both are `peak_text`) say so, for every endpoint with a
    stated `max_concurrency` -- an understated figure can lose the maximum
    to another limit, and must not leave it silently."""
    block = _asserted(600)
    assert peak_text(block, "proposed").endswith(
        "(e, max_concurrency, 60 s buckets); 16.4% of service time on e falls after the window")
    assert "falls after the window" not in peak_text(block, "current")
    assert "falls after the window" not in peak_text(_asserted(60), "proposed")
    # 1.4% is a note under the endpoint, not a clause of the headline.
    assert ("**Note:** 1.4% of the proposed arm's service time on this endpoint (156 s) falls "
            "after the window's last bucket") in render_capacity_section(_asserted(60))
    # The headline is another limit; the understated concurrency is still named.
    other = _asserted(1800, {"e": {"max_concurrency": 4000.0, "requests_per_minute": 6.0}})
    assert other["peak"]["proposed"]["limit"] == "requests_per_minute"
    assert peak_text(other, "proposed").endswith(
        "(e, requests_per_minute, 60 s buckets); 49.8% of service time on e falls after the "
        "window")
    # With no concurrency limit stated nothing printed rests on service time.
    rate_only = _asserted(1800, {"e": {"requests_per_minute": 6.0}})
    assert "falls after the window" not in peak_text(rate_only, "proposed")


def test_no_figure_is_printed_as_a_raw_millisecond_count():
    """A day of asserted service time on 180 requests is 1.5e10 ms. The
    report prints seconds and a share."""
    text = render_capacity_section(_asserted(86_400))
    assert not re.search(r"\de[+-]\d", text)
    assert "of the proposed arm's service time on this endpoint (15,3" in text
    tiny = render_capacity_section(_round6(block_for(
        traffic(QUIET_THEN_BUSY), one_endpoint(),
        {"endpoints": {"update": {"local-cheap": {"latency_ms": 12000}}}})))
    assert ("**Note:** 0.1% of the proposed arm's service time on this endpoint (1 s) falls "
            "after the window's last bucket") in tiny


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
             CAPACITY_CAVEAT, NO_REMEDY_NOTE, NO_RANGE_NOTE, PEAK_COLUMN_NOTE, AVERAGES_NOTE,
             VOLUME_NOTE,
             render_capacity_section(_moved_to_a_tier_default(
                 {"e": {"max_concurrency": 1.0}, "new": {"max_concurrency": 1.0}})),
             render_capacity_section(_asserted(1800))]
    for text in texts:
        assert not [word for word in CAPACITY_BANNED if word in text.lower()], text[:60]
    assert NO_REMEDY_NOTE == ("This section reports load against stated limits and proposes no "
                              "remedy: not more capacity, and not a change to any policy or "
                              "protection.")
    assert "does not make a scenario the one to choose" in PEAK_COLUMN_NOTE
    assert "read it with the average" in PEAK_COLUMN_NOTE


#: What must follow every printed peak utilisation: the average over the
#: window, then the share of the window's buckets above 100% and how many
#: buckets that is a share of.
_PEAK_CONTEXT = re.compile(r" / avg [\d.]+%, over 100% in [\d.]+% of \d+ buckets ")


def unpaired_peaks(text: str) -> list[str]:
    """Every line of a report that prints a peak utilisation -- a
    percentage in a "peak" sentence, cell or limits-table row -- without
    its context beside it: the average over the window AND how much of the
    window is above 100%. A limits-table row carries the latter as a count
    (`10 of 30`). Shared with the comparison report's test."""
    bad = []
    for line in text.split("\n"):
        if "%" not in line:
            continue
        if line.startswith("| ") and re.match(r"\| \w+ \| (current|proposed) \| ", line):
            # A limits-table row: stated | peak | avg (window) | avg (active) | buckets over |
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if cells[3].endswith("%") and not (
                    cells[4].endswith("%") and cells[5].endswith("%")
                    and len(cells) > 6 and re.fullmatch(r"\d+ of \d+", cells[6])):
                bad.append(line)
        elif "peak" in line.lower():
            for found in re.finditer(r"peak (\d[\d.]*%)", line):
                if not _PEAK_CONTEXT.match(line, found.end()):
                    bad.append(line)
    return bad


def test_c20_no_sentence_prints_a_peak_without_the_average_and_the_share():           # C20
    blocks = [_block({"workload": {"volume_scale": 5}}),
              _round6(block_for(mixed_traffic(), three_endpoints(),
                                {"endpoints": {"remove": ["local-mid"]}},
                                limits={"local-mid": {"max_concurrency": 2.0},
                                        "local-big": {"requests_per_minute": 5.0}}))]
    for block in blocks:
        text = render_capacity_section(block)
        assert text.count(" / avg ") >= 2 and unpaired_peaks(text) == []
        for arm in ("current", "proposed"):
            assert re.fullmatch(
                r"peak [\d.]+% / avg [\d.]+%, over 100% in [\d.]+% of \d+ buckets "
                r"\([\w-]+, \w+, 60 s buckets\)(; concurrency not computed for [\w-]+)*",
                peak_text(block, arm))
    # The detector is not vacuous: a bare peak, a peak with the average and
    # no share, and a table row missing either.
    assert unpaired_peaks("Peak load: current peak 230.0% (e, max_concurrency).") != []
    assert unpaired_peaks("Peak load: current peak 355.2% / avg 59.3% (e, max_concurrency, "
                          "60 s buckets).") != []
    assert unpaired_peaks("| max_concurrency | current | 1 | 230.0% | n/a | n/a | 1 of 2 |") != []
    assert unpaired_peaks("| max_concurrency | current | 1 | 230.0% | 9.0% | 9.0% | n/a |") != []
    assert unpaired_peaks("Peak load: current peak 355.2% / avg 59.3%, over 100% in 17.6% of "
                          "10082 buckets (e, max_concurrency, 60 s buckets).") == []


def test_the_headline_tells_often_over_the_limit_from_rarely():
    """A stationary low-rate week (Poisson, 0.02 requests a second, 30 s
    each: 0.6 in service on average). Against one concurrent request it is
    over the limit in 17.6% of the minutes; against two, in 1.2% of them.
    Peak and average alone read "355.2% / 59.3%" and "177.6% / 29.6%" --
    the same shape -- and the ratio of peak to average grows with the
    length of the export while the share of buckets over stays put. So the
    share is printed with them."""
    week = synthetic(_poisson(0.02, 24 * 7, 5, lambda rng: 30.0))
    seen = {}
    for limit in (1.0, 2.0):
        block = _round6(capacity_block(week, CapacityPlan(
            limits={"e": {"max_concurrency": limit}})))
        buckets = block["window"]["buckets"]
        seen[limit] = peak_text(block, "current")
        stated = block["endpoints"]["e"]["current"]["limits"]["max_concurrency"]
        assert block["peak"]["current"]["buckets_over_share"] == stated["buckets_over_share"]
        assert stated["buckets_over_share"] == round(stated["buckets_over"] / buckets, 6)
    assert seen[1.0] == (f"peak 355.2% / avg 59.3%, over 100% in 17.6% of {buckets} buckets "
                         "(e, max_concurrency, 60 s buckets)")
    assert seen[2.0] == (f"peak 177.6% / avg 29.6%, over 100% in 1.2% of {buckets} buckets "
                         "(e, max_concurrency, 60 s buckets)")
