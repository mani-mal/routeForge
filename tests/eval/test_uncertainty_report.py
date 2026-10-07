"""P3c-3: what a report says about a range (spec §8, §10; invariants U9, U11)."""
from __future__ import annotations

from app.eval.uncertainty import (NOT_COVERED, UNCERTAINTY_CAVEAT, ZERO_COUNT_NOTE,
                                  UncertaintyPlan, bucket_lines, caveat_lines,
                                  insufficient_text, level_text, method_line, outside_line,
                                  plan_block, quality_line, range_text,
                                  render_uncertainty_section, results_block, with_range,
                                  zero_count_note)
from tests.eval.test_uncertainty import BANNED, REJECTED, _arm, _result, _synthetic


def _block(results=None, *, days=3.0, **plan_kw) -> dict:
    plan = UncertaintyPlan(**{"replicates": 100, **plan_kw})
    return {**plan_block(plan),
            **results_block(_synthetic() if results is None else results, plan, days=days)}


def test_u9_caveat_is_the_spec_text():                                     # U9
    """The caveat is pinned word for word: it is the statement of what a
    range is. Editing it means editing spec §8 too."""
    assert UNCERTAINTY_CAVEAT == (
        "**Ranges show sampling variability only.** Each range is a percentile interval from\n"
        "resampling the recorded rows with replacement, within each application (applications with\n"
        "fewer than `--min-rows` rows are resampled together for the totals), under this replay\n"
        "model. It does not include replay model error (first-decision replay, frozen signals,\n"
        "predicted overflow), the error of the admission estimate for rows with no backend token\n"
        "count, inputs that were not recorded, or future traffic shift. Replay gives each endpoint\n"
        "one latency, so a latency range reflects only which endpoints the resampled rows used. A\n"
        "narrow range on a biased replay is still biased."
    )
    assert all(line.startswith("> ") for line in caveat_lines())
    assert len(caveat_lines()) == 8


def test_u9_not_covered_names_every_excluded_source():                     # U9
    assert NOT_COVERED == ("replay_model_error", "admission_estimate_error",
                           "unrecorded_inputs", "traffic_shift", "per_request_latency")
    assert plan_block(UncertaintyPlan())["not_covered"] == list(NOT_COVERED)


def test_u9_section_opens_with_the_caveat_and_says_how_ranges_were_made():  # U9
    text = render_uncertainty_section(_block(replicates=100, seed=5))
    lines = text.split("\n")
    assert lines[0] == "## Uncertainty ranges" and lines[1] == ""
    assert lines[2:10] == caveat_lines()
    assert ("90% percentile intervals from 100 bootstrap resamples of the simulated rows, "
            "drawn with replacement within each application (applications with fewer than "
            "30 rows are resampled together for the totals) (seed 5).") in text
    assert "A bucket with fewer than 30 rows gets no range." in text
    assert text.endswith("\n") and not text.endswith("\n\n\n")


def test_section_structure_totals_per_day_and_apps():
    text = render_uncertainty_section(_block())
    order = [text.index(h) for h in ("### Totals (n = 240)", "### Per day", "### By app",
                                     "#### app-0 (n = 80)", "#### app-1 (n = 80)",
                                     "#### app-2 (n = 80)")]
    assert order == sorted(order)
    assert "| figure | point | 90% range |" in text
    assert "| cost_usd delta | " in text and "| newly_rejected_share | " in text
    assert "| cost_usd/day (both routed) delta | " in text
    assert "Quality uncertainty covers 0 of 240 routed rows in the current arm" in text
    assert "the rest use a point prior." in text


def test_no_per_day_table_without_a_window_of_a_day():
    assert "### Per day" not in render_uncertainty_section(_block(days=None))


def test_u7_report_prints_insufficient_data_instead_of_a_range():          # U7
    results = _synthetic() + [_result(5000 + i, "small", _arm("local-mid", 0.01), REJECTED)
                              for i in range(5)]
    text = render_uncertainty_section(_block(results))
    section = text[text.index("#### small (n = 5)"):]
    assert section.strip().split("\n")[-1] == "insufficient data (n = 5)"
    assert "| figure |" not in section
    assert insufficient_text({"insufficient_data": True, "n": 5}) == "insufficient data (n = 5)"
    # Too few rows in all: no table anywhere, and no per-day section.
    tiny = render_uncertainty_section(_block(_synthetic(12)))
    assert "### Totals (n = 12)\n\ninsufficient data (n = 12)\n" in tiny
    assert "| figure |" not in tiny and "### Per day" not in tiny


def test_zero_newly_rejected_is_never_presented_as_no_risk():
    block = _block()
    app0 = block["by_app"]["app-0"]
    note = zero_count_note(app0, 0.90)
    assert note == ZERO_COUNT_NOTE.format(n=80, level="90%", bound="2.84%", one_in="35")
    assert "That is not zero risk" in note and "independent draws" in note
    assert note in bucket_lines(app0, 0.90)
    assert zero_count_note(block["by_app"]["app-1"], 0.90) is None        # it has rejections
    assert zero_count_note({"insufficient_data": True, "n": 3}, 0.90) is None
    text = render_uncertainty_section(block)
    assert text.count("That is not zero risk") == 1


def test_range_text_and_with_range():
    assert range_text({"point": 1.5, "lo": 1.25, "hi": 2.0}) == "1.25 to 2"
    assert range_text({"point": 9.0, "lo": 1.0, "hi": 2.0, "point_outside": True}) == \
        "1 to 2 (point outside range)"
    assert range_text({"point": 800.0, "lo": None, "hi": None, "undefined_replicates": 7}) == \
        "n/a (undefined in 7 resamples)"
    assert with_range("-0.39", {"point": -0.39, "lo": -0.52, "hi": -0.21}) == \
        "-0.39 [-0.52, -0.21]"
    assert with_range("3", {"point": 3, "lo": 0, "hi": 6}) == "3 [0, 6]"
    assert with_range("n/a", {"point": None, "lo": None, "hi": None,
                              "undefined_replicates": 100}) == "n/a [n/a]"
    assert with_range("5", None) == "5"
    assert level_text(0.9) == "90%" and level_text(0.95) == "95%" and level_text(0.5) == "50%"


def test_point_outside_is_said_in_the_report():
    block = _block(replicates=1)
    assert block["points_outside_interval"] > 0
    text = render_uncertainty_section(block)
    assert outside_line(block["points_outside_interval"]) in text
    assert "(point outside range)" in text
    assert outside_line(0) is None
    assert "outside their own range" not in render_uncertainty_section(_block())


def test_quality_line_names_calibrated_rows_without_a_posterior():
    full = {"routed": 60, "calibrated": 60, "posterior": 60}
    assert quality_line({"current": full, "proposed": full}) == (
        "Quality uncertainty covers 60 of 60 routed rows in the current arm and 60 of 60 in "
        "the proposed arm; the rest use a point prior.")
    partial = {"routed": 60, "calibrated": 40, "posterior": 10}
    text = quality_line({"current": partial, "proposed": full})
    assert "covers 10 of 60 routed rows in the current arm" in text
    assert "Up to 30 row(s) per arm have a calibrated quality with no posterior loaded" in text
    assert "--quality-posteriors" in text


def test_a_block_with_no_figures_renders_the_caveat_and_method_only():
    """`--shadow-prices` without `--proposal`: there are no totals."""
    text = render_uncertainty_section(plan_block(UncertaintyPlan(replicates=50)))
    assert text.split("\n")[2:10] == caveat_lines()
    assert method_line(plan_block(UncertaintyPlan(replicates=50))) in text
    assert "### Totals" not in text and "Quality uncertainty" not in text


def test_u11_report_text_does_not_rank_or_recommend():                     # U11
    results = _synthetic() + [_result(5000 + i, "small", _arm("local-mid", 0.01), REJECTED)
                              for i in range(5)]
    partial = {"routed": 60, "calibrated": 40, "posterior": 10}
    text = (render_uncertainty_section(_block(results, replicates=1))
            + quality_line({"current": partial, "proposed": partial})).lower()
    assert not [word for word in BANNED if word in text]
