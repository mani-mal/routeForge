"""P3c-3 final-review fix F4: say what a bootstrap will cost before it runs,
and keep a report readable when an export has thousands of applications."""
from __future__ import annotations

import asyncio

import pytest

import app.eval.uncertainty as unc
from app.eval.scenarios import comparison_doc, render_comparison
from app.eval.shadow import shadow_prices
from app.eval.uncertainty import UncertaintyPlan
from tests.eval.sim_fixtures import BASE_SETTINGS, app_entry, make_row, make_snapshot
from tests.eval.test_scenarios_compare import _INPUTS, _compare, _doc
from tests.eval.test_uncertainty_golden import REJECTIONS, ROUTES, run_case
from tests.eval.test_uncertainty_strata import _APPS, _pool_snap, _small_app_rows

PLAN = UncertaintyPlan()            # 1000 replicates, min_rows 30
PREFIX = "policy_simulate: uncertainty: "


# --- the estimate ----------------------------------------------------------------------

def test_f4_the_estimate_has_a_row_term_and_a_per_application_term():
    """Cost is not simply replicates x rows: every stratum that is drawn
    costs a seeded generator and a set of figures per replicate."""
    one = unc.estimate_seconds(PLAN, [[40_000]])
    many = unc.estimate_seconds(PLAN, [[40] * 1000])
    assert one == pytest.approx(1000 * (40_000 * unc.ROW_SECONDS + 1 * unc.STRATUM_SECONDS))
    assert many == pytest.approx(1000 * (40_000 * unc.ROW_SECONDS + 1000 * unc.STRATUM_SECONDS))
    assert many > 1.5 * one
    # Linear in replicates and in passes.
    assert unc.estimate_seconds(UncertaintyPlan(replicates=2000), [[40] * 1000]) == \
        pytest.approx(2 * many)
    assert unc.estimate_seconds(PLAN, [[40] * 1000] * 3) == pytest.approx(3 * many)


def test_f4_pooled_applications_cost_one_stratum():
    """10,000 three-row apps are one pooled stratum, not 10,000 draws."""
    pooled = unc.estimate_seconds(PLAN, [[3] * 10_000])
    assert pooled == pytest.approx(1000 * (30_000 * unc.ROW_SECONDS + 1 * unc.STRATUM_SECONDS))
    # ...unless --min-rows makes each of them its own stratum.
    own = unc.estimate_seconds(UncertaintyPlan(min_rows=1), [[3] * 10_000])
    assert own == pytest.approx(1000 * (30_000 * unc.ROW_SECONDS + 10_000 * unc.STRATUM_SECONDS))


def test_f4_nothing_is_drawn_below_min_rows():
    assert unc.estimate_seconds(PLAN, [[5, 7]]) == 0.0
    assert unc.estimate_seconds(PLAN, [[5, 7], [60]]) == pytest.approx(
        1000 * (60 * unc.ROW_SECONDS + unc.STRATUM_SECONDS))


def test_f4_cost_line_wording():
    line = unc.cost_line(PLAN, [[60, 6, 45]])
    assert line.startswith(PREFIX + "1000 resamples x 111 rows x 1 pass: about ")
    assert "rough estimate" in line and "per resample per row" in line \
        and "per resample per stratum" in line
    assert unc.cost_line(PLAN, [[60, 6, 45]] * 3).startswith(
        PREFIX + "1000 resamples x 111 rows x 3 passes: about ")
    # Passes over different rows (shadow-price relaxations): the total is given.
    mixed = unc.cost_line(PLAN, [[60], [45], [60, 6, 45]], upper_bound=True)
    assert mixed.startswith(PREFIX + "1000 resamples x up to 3 passes (216 rows in all): about ")
    assert "\n" not in line and "\n" not in mixed


@pytest.mark.parametrize("seconds,text", [(0.0, "under 1 s"), (0.4, "under 1 s"), (1.0, "1 s"),
                                          (59.4, "59 s"), (119.9, "120 s"), (120.0, "2 min"),
                                          (3000.0, "50 min"), (36000.0, "600 min")])
def test_f4_duration_text(seconds, text):
    assert unc._duration(seconds) == text


# --- the CLI prints it, once per bootstrap phase, on stderr -----------------------------

def _cost_lines(err: str) -> list[str]:
    return [line for line in err.split("\n") if line.startswith(PREFIX)]


@pytest.mark.parametrize("mode,starts", [
    ("proposal", ["60 resamples x 111 rows x 1 pass: "]),
    ("scenarios", ["60 resamples x 111 rows x 3 passes: "]),
    # Two relaxations per app (big, few: allowed_tiers and the cost cap; safe: residency
    # and the cost cap), each over that app's rows: 2 x (60 + 6 + 45).
    ("shadow", ["60 resamples x up to 6 passes (222 rows in all): "]),
    ("proposal_shadow", ["60 resamples x 111 rows x 1 pass: ",
                         "60 resamples x up to 6 passes (222 rows in all): "]),
])
def test_f4_the_cli_says_what_each_bootstrap_will_cost(mode, starts, tmp_path, capsys):
    assert run_case(ROUTES, mode, tmp_path / "out", "--uncertainty", "--bootstrap", "60") == 0
    captured = capsys.readouterr()
    lines = _cost_lines(captured.err)
    assert [line[len(PREFIX):len(PREFIX) + len(start)] for line, start in zip(lines, starts)] \
        == starts and len(lines) == len(starts)
    assert PREFIX not in captured.out           # stdout stays the two output paths


@pytest.mark.parametrize("mode", ["proposal", "scenarios", "shadow"])
def test_f4_no_cost_line_without_the_flag_or_when_the_gate_refuses(mode, tmp_path, capsys):
    assert run_case(ROUTES, mode, tmp_path / "plain") == 0
    assert _cost_lines(capsys.readouterr().err) == []
    assert run_case(ROUTES, mode, tmp_path / "gated", "--uncertainty", "--bootstrap", "60",
                    "--min-agreement", "0.99") == 4
    assert _cost_lines(capsys.readouterr().err) == []


def test_f4_the_shadow_estimate_is_one_pass_per_relaxation_over_its_rows(tmp_path, capsys):
    """The rejections case has no endpoint allow-list: seven app-scoped
    relaxations, 40 rows each."""
    assert run_case(REJECTIONS, "shadow", tmp_path / "out", "--uncertainty",
                    "--bootstrap", "60") == 0
    (line,) = _cost_lines(capsys.readouterr().err)
    assert line.startswith(PREFIX + "60 resamples x up to 7 passes (280 rows in all): ")


def test_f4_an_endpoint_relaxation_is_a_pass_over_every_row():
    """Five apps (89 rows), each with the cost-cap relaxation over its own
    rows, plus one endpoint allow-list relaxation over all 89."""
    lines: list[str] = []
    block = asyncio.run(shadow_prices(
        _small_app_rows(_APPS), _pool_snap(allowed_app_ids=["chat"]), base=BASE_SETTINGS,
        uncertainty=UncertaintyPlan(replicates=30), notify=lines.append))
    assert block["evaluated"] == 6
    assert len(lines) == 1 and lines[0].startswith(
        PREFIX + "30 resamples x up to 6 passes (178 rows in all): ")
    # Without a plan nothing is said.
    quiet: list[str] = []
    asyncio.run(shadow_prices(_small_app_rows(_APPS), _pool_snap(), base=BASE_SETTINGS,
                              notify=quiet.append))
    assert quiet == []


# --- F4d: a report with many small applications -----------------------------------------

def _many_apps(small: int) -> tuple[list[dict], dict]:
    apps = {"main": 40, **{f"tiny-{i:02d}": 2 for i in range(small)}}
    rows = [make_row(f"{app}-{i:03d}", app, prompt_tokens=200 + 9 * i + len(app),
                     ts=f"2026-09-{20 + i % 3:02d}T12:{i % 60:02d}:00+00:00")
            for app, n in apps.items() for i in range(n)]
    return rows, make_snapshot(apps=[app_entry(app) for app in apps])


def _small_line(small: int) -> str:
    rows, snap = _many_apps(small)
    doc = comparison_doc(_compare(rows, snap, _doc(("half", {"workload": {"token_scale": 0.5}})),
                                  uncertainty=UncertaintyPlan(replicates=20)), inputs=_INPUTS)
    report = render_comparison(doc)
    return next(l for l in report.split("\n") if l.startswith("Per-app ranges are in"))


def test_f4_many_insufficient_apps_are_counted_and_the_first_ten_named():
    line = _small_line(15)
    assert line == ("Per-app ranges are in `comparison.json`. Insufficient data: 15 app(s), "
                    "the first 10: " + ", ".join(f"tiny-{i:02d} (n = 2)" for i in range(10))
                    + ".")
    assert "tiny-10" not in line


def test_f4_up_to_ten_insufficient_apps_are_all_named():
    assert _small_line(10) == (
        "Per-app ranges are in `comparison.json`. Insufficient data: 10 app(s): "
        + ", ".join(f"tiny-{i:02d} (n = 2)" for i in range(10)) + ".")
    assert _small_line(0) == "Per-app ranges are in `comparison.json`."
