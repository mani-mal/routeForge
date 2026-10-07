"""P3c-3: quality-label uncertainty (spec §6, invariants U4, U8).

A calibrated quality's Beta posterior is read from the P2b-3
`recommendation.json` the set was applied from. The fixtures build that file
with the real estimator (`calibration.estimate`) and replay real snapshots,
so the set id, the mean and the resolver's order are the product's own.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.eval.calibration import (_beta_ppf, build_recommendation, dump_recommendation,
                                  estimate, recommendation_set_id)
from app.eval.simulate import simulate
from app.eval.uncertainty import (Posterior, UncertaintyError, UncertaintyPlan, draw_quality,
                                  load_posteriors, plan_block, plan_inputs, posterior_key,
                                  quality_coverage, results_block)
from tests.eval.sim_fixtures import BASE_SETTINGS, make_row, make_snapshot

P = "routeforge.policy-proposal/v1"


def _labelled(model: str, workload: str | None, good: int, bad: int, prior: float) -> list[dict]:
    """Outcome records the estimator accepts: `good` rows labelled
    `route_correct: 1` and `bad` labelled 0, all served by `model`."""
    scoring = {"candidates": [{"name": model, "quality": {"v": prior, "src": "tier_default"}}]}
    return [{"request_id": f"{model}-{workload}-{i}", "ts": "2026-09-20T00:00:00+00:00",
             "status": "ok", "endpoint_registry_version": "test", "workload": workload,
             "chosen": model, "cache_hit": False, "scoring": scoring,
             "feedback": [{"score_name": "route_correct", "value": 1 if i < good else 0,
                           "source": "admin", "ts": "2026-09-20T00:00:00+00:00"}]}
            for i in range(good + bad)]


def _recommendation(records: list[dict]) -> dict:
    return build_recommendation(estimate(records, max_ci_width=1.0), export_sha256="e" * 64)


def _write(tmp_path, rec: dict, name: str = "recommendation.json") -> str:
    path = tmp_path / name
    path.write_text(dump_recommendation(rec), encoding="utf-8")
    return str(path)


def _cheap_rec() -> dict:
    """`local-cheap`, model-wide: prior 0.5 (k = 10), 36 good and 4 bad
    labels -> Beta(41, 9), mean 0.82."""
    return _recommendation(_labelled("local-cheap", None, 36, 4, 0.5))


def _unit(rec: dict, model: str, workload: str | None) -> dict:
    return next(u for u in rec["units"] if u["model"] == model and u["workload"] == workload)


def _snap(rec: dict, *entries: tuple[str, str | None]) -> dict:
    """A snapshot whose `quality.calibrated` holds `rec`'s unit for each
    `(model, workload)` -- what `GET /admin/config-snapshot` exports after
    the set is applied."""
    calibrated = [{"model": m, "workload": w, "score": _unit(rec, m, w)["mean"],
                   "set_id": rec["set_id"]} for m, w in entries]
    return make_snapshot(quality={"customer": [], "calibrated": calibrated})


def _rows(n: int = 60, **over) -> list[dict]:
    return [make_row(f"r{i:03d}", "app-a", prompt_tokens=200 + 11 * i,
                     ts=f"2026-09-2{i % 3}T12:{i % 60:02d}:00+00:00", **over)
            for i in range(n)]


def _replay(rows, snap, proposal=None):
    results, _excluded = asyncio.run(
        simulate(rows, snap, proposal or {"schema": P}, base=BASE_SETTINGS))
    return results


def _plan(tmp_path, rec, **kw) -> UncertaintyPlan:
    posteriors, files = load_posteriors([_write(tmp_path, rec)])
    return UncertaintyPlan(posteriors=posteriors, posterior_files=files, **kw)


# --- loading --------------------------------------------------------------------

def test_a_real_recommendation_file_loads(tmp_path):
    rec = _cheap_rec()
    unit = _unit(rec, "local-cheap", None)
    assert (unit["status"], unit["alpha"], unit["beta"], unit["mean"]) == (
        "recommended", 41.0, 9.0, 0.82)
    path = _write(tmp_path, rec)
    posteriors, files = load_posteriors([path, path])          # the same file twice: once
    assert posteriors == {(rec["set_id"], "local-cheap", None): Posterior(41.0, 9.0, 0.82)}
    assert len(files) == 1 and files[0][0] == rec["set_id"] and len(files[0][1]) == 64
    assert load_posteriors([]) == ({}, ())


def test_units_that_were_not_recommended_are_skipped(tmp_path):
    """Three labels: `insufficient`. It was never applied, so it is no one's
    posterior."""
    rec = _recommendation(_labelled("local-cheap", None, 36, 4, 0.5)
                          + _labelled("local-mid", None, 2, 1, 0.75))
    assert _unit(rec, "local-mid", None)["status"] == "insufficient"
    posteriors, _files = load_posteriors([_write(tmp_path, rec)])
    assert set(posteriors) == {(rec["set_id"], "local-cheap", None)}


def _resealed(rec: dict, mutate) -> dict:
    """`rec` with `mutate` applied and its `set_id` recomputed, so the file
    passes the content-hash check and reaches the unit validation."""
    out = json.loads(json.dumps(rec))
    mutate(out)
    out["set_id"] = recommendation_set_id(out)
    return out


@pytest.mark.parametrize("mutate,where", [
    (lambda r: r.update(schema="routeforge.quality-recommendation/v2"), "schema"),
    (lambda r: r.pop("schema"), "schema"),
    (lambda r: r.update(units={}), "units"),
    (lambda r: r["units"].__setitem__(0, "x"), "units[0]"),
    (lambda r: r["units"][0].update(model=""), "units[0].model"),
    (lambda r: r["units"][0].update(workload=7), "units[0].workload"),
    (lambda r: r["units"][0].update(alpha=0), "units[0].alpha"),
    (lambda r: r["units"][0].update(beta=None), "units[0].beta"),
    (lambda r: r["units"][0].update(alpha=True), "units[0].alpha"),
    (lambda r: r["units"][0].update(mean=1.5), "units[0].mean"),
])
def test_a_malformed_file_is_refused_naming_the_location(tmp_path, mutate, where):
    path = _write(tmp_path, _resealed(_cheap_rec(), mutate))
    with pytest.raises(UncertaintyError) as err:
        load_posteriors([path])
    assert where in str(err.value) and path in str(err.value)


def test_an_edited_file_is_refused(tmp_path):
    """The `set_id` is a hash of the content. A file edited after it was
    written (here: a narrower posterior) no longer matches its own id."""
    rec = _cheap_rec()
    rec["units"][0]["alpha"] = 4100.0
    with pytest.raises(UncertaintyError, match="set_id"):
        load_posteriors([_write(tmp_path, rec)])


@pytest.mark.parametrize("text", ["not json", "[1, 2]"])
def test_a_file_that_is_not_a_json_object_is_refused(tmp_path, text):
    path = tmp_path / "bad.json"
    path.write_text(text)
    with pytest.raises(UncertaintyError):
        load_posteriors([str(path)])


# --- which posterior belongs to an arm ----------------------------------------------

def test_the_arm_carries_the_calibration_set_id(tmp_path):
    rec = _cheap_rec()
    results = _replay(_rows(6), _snap(rec, ("local-cheap", None)))
    for r in results:
        assert (r.current.model, r.current.quality_source) == ("local-cheap", "calibrated")
        assert r.current.quality == 0.82 and r.current.quality_set_id == rec["set_id"]
    plain = _replay(_rows(3), make_snapshot())
    assert all(r.current.quality_set_id is None and r.current.quality_source == "tier_default"
               for r in plain)


def test_posterior_key_follows_the_resolver_order(tmp_path):
    """A workload-scoped unit wins for its workload; every other row of the
    model uses the `(model, *)` unit -- `QualityResolver.resolve`'s order."""
    rec = _recommendation(_labelled("local-cheap", "claims", 30, 10, 0.5)
                          + _labelled("local-cheap", None, 5, 0, 0.5))
    wide = _unit(rec, "local-cheap", None)
    assert _unit(rec, "local-cheap", "claims")["status"] == wide["status"] == "recommended"
    snap = _snap(rec, ("local-cheap", "claims"), ("local-cheap", None))
    posteriors, _files = load_posteriors([_write(tmp_path, rec)])
    claims, chat = _replay([make_row("c", "app-a", workload="claims"),
                            make_row("d", "app-a", workload="chat")], snap)
    sid = rec["set_id"]
    assert posterior_key(claims.current, claims.row, posteriors) == (sid, "local-cheap", "claims")
    assert posterior_key(chat.current, chat.row, posteriors) == (sid, "local-cheap", None)
    assert posterior_key(chat.current, chat.row, {}) is None


def test_a_file_never_attaches_to_a_score_it_did_not_produce(tmp_path):
    rec = _cheap_rec()
    posteriors, _files = load_posteriors([_write(tmp_path, rec)])
    snap = _snap(rec, ("local-cheap", None))
    # The snapshot's score for that set was edited by hand: not this file's mean.
    snap["quality"]["calibrated"][0]["score"] = 0.6
    (edited,) = _replay([make_row("r1", "app-a")], snap)
    assert edited.current.quality == 0.6 and edited.current.quality_set_id == rec["set_id"]
    assert posterior_key(edited.current, edited.row, posteriors) is None
    # A different set's score for the same model.
    snap = _snap(rec, ("local-cheap", None))
    snap["quality"]["calibrated"][0]["set_id"] = "0" * 16
    (other,) = _replay([make_row("r1", "app-a")], snap)
    assert posterior_key(other.current, other.row, posteriors) is None
    # A quality that is not calibrated at all.
    (plain,) = _replay([make_row("r1", "app-a")], make_snapshot())
    assert posterior_key(plain.current, plain.row, posteriors) is None


# --- U8: the quality range carries label uncertainty, and only where there is one ----

def test_u8_one_calibrated_unit_gives_the_posteriors_own_interval(tmp_path):   # U8
    """Every row is served by one calibrated unit, so resampling rows cannot
    move the quality mean at all: the whole range is the posterior's. It
    must be the Beta(41, 9) 5% and 95% quantiles."""
    rec = _cheap_rec()
    results = _replay(_rows(60), _snap(rec, ("local-cheap", None)))
    block = results_block(results, _plan(tmp_path, rec, replicates=4000))
    quality = block["totals"]["quality_mean"]["current"]
    assert quality["point"] == 0.82
    assert quality["lo"] == pytest.approx(_beta_ppf(0.05, 41.0, 9.0), abs=0.01)
    assert quality["hi"] == pytest.approx(_beta_ppf(0.95, 41.0, 9.0), abs=0.01)
    assert block["quality_posterior"]["current"] == {
        "routed": 60, "calibrated": 60, "posterior": 60}


def test_u8_without_the_file_a_calibrated_quality_stays_a_point(tmp_path):     # U8
    """No `--quality-posteriors` file: the score is used as a point, the
    range has no width, and the block says the rows are calibrated but
    carry no posterior. No uncertainty is invented."""
    rec = _cheap_rec()
    results = _replay(_rows(60), _snap(rec, ("local-cheap", None)))
    block = results_block(results, UncertaintyPlan(replicates=200))
    assert block["totals"]["quality_mean"]["current"] == {"point": 0.82, "lo": 0.82, "hi": 0.82}
    assert block["quality_posterior"]["current"] == {
        "routed": 60, "calibrated": 60, "posterior": 0}


def test_u8_a_tier_default_quality_has_no_label_uncertainty(tmp_path):         # U8
    rec = _cheap_rec()
    results = _replay(_rows(60), make_snapshot())
    block = results_block(results, _plan(tmp_path, rec, replicates=200))
    quality = block["totals"]["quality_mean"]["current"]
    assert quality["lo"] == quality["hi"] == quality["point"] == 0.5
    assert block["quality_posterior"] == {
        "current": {"routed": 60, "calibrated": 0, "posterior": 0},
        "proposed": {"routed": 60, "calibrated": 0, "posterior": 0}}


def test_u8_coverage_counts_a_mixed_export(tmp_path):                         # U8
    """Half the rows route to the calibrated cheap model, half (high
    complexity) to a model with a tier-default quality."""
    rec = _cheap_rec()
    rows = _rows(30) + [make_row(f"big{i:02d}", "app-a", complexity=1.0, prompt_tokens=3000)
                        for i in range(30)]
    results = _replay(rows, _snap(rec, ("local-cheap", None)))
    cheap = sum(1 for r in results if r.current.model == "local-cheap")
    assert 0 < cheap < 60
    plan = _plan(tmp_path, rec, replicates=300)
    assert quality_coverage(results, plan)["current"] == {
        "routed": 60, "calibrated": cheap, "posterior": cheap}
    with_labels = results_block(results, plan)["totals"]["quality_mean"]["current"]
    rows_only = results_block(results, UncertaintyPlan(replicates=300))[
        "totals"]["quality_mean"]["current"]
    # Label uncertainty adds to row sampling; it never narrows the range.
    assert with_labels["hi"] - with_labels["lo"] > rows_only["hi"] - rows_only["lo"] > 0


def test_u4_one_unit_in_both_arms_cancels_in_the_delta(tmp_path):              # U4
    """A no-op proposal: both arms use the same unit and so the same draw.
    The quality delta is exactly 0 in every replicate although each arm's
    quality has a wide range."""
    rec = _cheap_rec()
    results = _replay(_rows(60), _snap(rec, ("local-cheap", None)))
    totals = results_block(results, _plan(tmp_path, rec, replicates=300))["totals"]
    assert totals["quality_mean"]["delta"] == {"point": 0.0, "lo": 0.0, "hi": 0.0}
    assert totals["quality_mean"]["current"] == totals["quality_mean"]["proposed"]
    assert totals["quality_mean"]["current"]["hi"] - totals["quality_mean"]["current"]["lo"] > 0.1


def test_the_posterior_does_not_move_any_other_figure(tmp_path):
    """Quality draws use their own random streams: cost, latency and count
    ranges are the same with and without the file."""
    rec = _cheap_rec()
    snap = _snap(rec, ("local-cheap", None))
    rows = _rows(30) + [make_row(f"big{i:02d}", "app-a", complexity=1.0, prompt_tokens=3000)
                        for i in range(30)]
    results = _replay(rows, snap, {"schema": P, "endpoints": {"remove": ["local-mid"]}})
    a = results_block(results, _plan(tmp_path, rec, replicates=100))["totals"]
    b = results_block(results, UncertaintyPlan(replicates=100))["totals"]
    for key in a:
        if key != "quality_mean":
            assert a[key] == b[key], key


def test_quality_draws_are_a_function_of_seed_replicate_and_unit():
    post = Posterior(41.0, 9.0, 0.82)
    key = ("abc", "local-cheap", None)
    assert draw_quality(0, 3, key, post) == draw_quality(0, 3, key, post)
    assert draw_quality(0, 3, key, post) != draw_quality(0, 4, key, post)
    assert draw_quality(0, 3, key, post) != draw_quality(1, 3, key, post)
    assert draw_quality(0, 3, key, post) != draw_quality(0, 3, ("abc", "local-cheap", "x"), post)
    assert all(0.0 < draw_quality(0, b, key, post) < 1.0 for b in range(50))


def test_plan_block_and_inputs_name_the_files(tmp_path):
    rec = _cheap_rec()
    plan = _plan(tmp_path, rec)
    (set_id, sha), = plan.posterior_files
    assert plan_inputs(plan)["quality_posteriors_sha256"] == [sha]
    assert plan_block(plan)["quality_posterior_sets"] == [set_id] == [rec["set_id"]]
    assert plan_inputs(UncertaintyPlan())["quality_posteriors_sha256"] == []
    assert plan_block(UncertaintyPlan())["quality_posterior_sets"] == []
