"""Tests for `app.eval.calibration` (P2b-3 Task 3): the offline Beta-posterior
quality estimator and the hash-identified recommendation file.

Fixtures are hand-built NDJSON exports written to `tmp_path` and read back
through the real `app.eval.outcomes.load_export`, so the estimator sees
exactly the record shape the P2b-2 export produces.

Golden Beta quantiles were cross-checked with an independent, exact method:
for integer shape parameters, `I_x(a, b) = P(Binomial(a+b-1, x) >= a)`, which
was evaluated in exact rational arithmetic (`fractions.Fraction` +
`math.comb`) and inverted by bisection to ~1e-13:

  Beta(10, 30): q0.95 = 0.3681529011, q0.05 = 0.1462186304
  Beta(23, 9):  q0.05 = 0.5823396663, q0.95 = 0.8393889087
  Beta(2, 2):   q0.50 = 0.5

(The plan's placeholder "0.3683" for Beta(10,30) q0.95 was wrong in the 4th
decimal: both methods give 0.36815.)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.eval.calibration import (
    RECOMMENDATION_SCHEMA,
    _beta_cdf,
    _beta_ppf,
    build_recommendation,
    dump_recommendation,
    estimate,
    recommendation_set_id,
)
from app.eval.outcomes import load_export

# --- fixture builders ----------------------------------------------------

def _cands(*models: tuple[str, float, str]) -> dict:
    return {"candidates": [{"name": m, "quality": {"v": v, "src": s}}
                           for m, v, s in models]}


_DEFAULT_SCORING = _cands(("m-a", 0.5, "published"), ("m-b", 0.6, "tier_default"))


def _rec(rid: str, chosen: str | None = "m-a", *, status: str = "ok",
         workload: str | None = "chat", reg: str | None = "reg-v1",
         ts: str = "2026-09-20T00:00:00+00:00", cache_hit: bool = False,
         feedback: list[tuple[str, object]] = (), scoring: dict | None = None) -> dict:
    return {
        "schema": "routeforge.outcome/v1",
        "outcome": {
            "request_id": rid, "ts": ts, "status": status,
            "endpoint_registry_version": reg, "workload": workload,
            "chosen": chosen, "cache_hit": cache_hit,
            "scoring": _DEFAULT_SCORING if scoring is None else scoring,
        },
        "feedback": [{"score_name": n, "value": v, "source": "admin", "ts": ts}
                     for n, v in feedback],
    }


def _load(tmp_path: Path, lines: list[dict]) -> list[dict]:
    path = tmp_path / "export.ndjson"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    records, warnings = load_export(str(path))
    assert warnings == []
    return records


def _hand_example() -> list[dict]:
    """18 route_correct=1, 2 route_correct=0, 2 verify_failed -> s=18, t=22."""
    lines = [_rec(f"ok-{i}", feedback=[("route_correct", 1)]) for i in range(18)]
    lines += [_rec(f"bad-{i}", feedback=[("route_correct", 0)]) for i in range(2)]
    lines += [_rec(f"vf-{i}", status="verify_failed") for i in range(2)]
    return lines


def _unit(est: dict, model: str, workload: str | None) -> dict:
    [u] = [u for u in est["units"] if u["model"] == model and u["workload"] == workload]
    return u


# --- Beta helper ---------------------------------------------------------

def test_beta_ppf_known_values():
    assert _beta_ppf(0.5, 2, 2) == pytest.approx(0.5, abs=1e-9)
    assert _beta_ppf(0.05, 1, 1) == pytest.approx(0.05, abs=1e-9)
    for p in (0.1, 0.37, 0.9):
        assert _beta_ppf(p, 1, 1) == pytest.approx(p, abs=1e-9)
    # Exact binomial-identity goldens (module docstring).
    assert round(_beta_ppf(0.95, 10, 30), 3) == 0.368
    assert _beta_ppf(0.95, 10, 30) == pytest.approx(0.3681529011, abs=1e-8)
    assert _beta_ppf(0.05, 10, 30) == pytest.approx(0.1462186304, abs=1e-8)
    assert _beta_ppf(0.05, 23, 9) == pytest.approx(0.5823396663, abs=1e-8)
    assert _beta_ppf(0.95, 23, 9) == pytest.approx(0.8393889087, abs=1e-8)


def test_beta_cdf_matches_binomial_identity():
    from fractions import Fraction
    from math import comb

    def exact(x: float, a: int, b: int) -> float:
        n, xf = a + b - 1, Fraction(x)
        return float(sum(comb(n, j) * xf**j * (1 - xf)**(n - j)
                         for j in range(a, n + 1)))

    for a, b in ((10, 30), (23, 9), (2, 2), (1, 5)):
        for x in (0.05, 0.3, 0.5, 0.72, 0.95):
            assert _beta_cdf(x, a, b) == pytest.approx(exact(x, a, b), abs=1e-12)


# --- hand-computed posterior --------------------------------------------

def test_hand_computed_posterior(tmp_path):
    est = estimate(_load(tmp_path, _hand_example()), max_ci_width=0.3)
    u = _unit(est, "m-a", None)
    assert u["q0"] == 0.5 and u["q0_source"] == "published"
    assert (u["alpha"], u["beta"]) == (23.0, 9.0)
    assert u["mean"] == 0.71875
    assert u["shift"] == 0.21875
    assert u["ci_low"] == round(_beta_ppf(0.05, 23, 9), 6) == 0.58234
    assert u["ci_high"] == round(_beta_ppf(0.95, 23, 9), 6) == 0.839389
    assert (u["n_labels"], u["n_rows"], u["label_coverage"]) == (22, 22, 1.0)
    assert u["status"] == "recommended" and u["reason"] is None


def test_hand_example_ci_too_wide_at_default(tmp_path):
    """The Beta(23, 9) 90% interval is 0.257049 wide, just above the 0.25
    default, so with default options the same unit is insufficient."""
    u = _unit(estimate(_load(tmp_path, _hand_example())), "m-a", None)
    assert u["status"] == "insufficient"
    assert u["reason"] == "ci width 0.257049 > max_ci_width 0.25"


def test_quality_fraction_evidence(tmp_path):
    lines = [_rec("q1", feedback=[("quality", 0.25), ("route_correct", 1)]),
             _rec("q2", feedback=[("quality", 1.5)]),          # out of range: ignored
             _rec("q3", feedback=[("route_correct", 0.5)]),    # not 0/1: ignored
             _rec("q4", feedback=[("preferred_model", "m-b")]),  # categorical
             _rec("q5")]
    u = _unit(estimate(_load(tmp_path, lines)), "m-a", None)
    # s = 0.25 + 1, t = 2 -> Beta(5 + 1.25, 5 + 0.75)
    assert (u["alpha"], u["beta"]) == (6.25, 5.75)
    assert (u["n_labels"], u["n_rows"], u["label_coverage"]) == (2, 5, 0.2)


# --- exclusions ---------------------------------------------------------

def test_exclusions(tmp_path):
    good = [_rec(f"g{i}", feedback=[("route_correct", 1)]) for i in range(3)]
    excluded = [
        _rec("cache", cache_hit=True, feedback=[("route_correct", 0)]),
        _rec("none", chosen=None, feedback=[("route_correct", 0)]),
        _rec("ne", status="no_eligible", feedback=[("route_correct", 0)]),
        _rec("bf", status="backend_failed", feedback=[("route_correct", 0)]),
        _rec("co", status="context_overflow", feedback=[("route_correct", 0)]),
    ]
    u = _unit(estimate(_load(tmp_path, good + excluded)), "m-a", None)
    assert (u["n_rows"], u["n_labels"], u["alpha"], u["beta"]) == (3, 3, 8.0, 5.0)


# --- status rules -------------------------------------------------------

def test_min_labels_insufficient(tmp_path):
    lines = [_rec(f"r{i}", feedback=[("route_correct", 1)]) for i in range(5)]
    u = _unit(estimate(_load(tmp_path, lines)), "m-a", None)
    assert u["status"] == "insufficient"
    assert u["reason"] == "n_labels 5 < min_labels 20"


def test_small_shift_no_change(tmp_path):
    # q0=0.5, 20 labels at exactly half -> mean 0.5, shift 0.
    lines = [_rec(f"r{i}", feedback=[("route_correct", i % 2)]) for i in range(40)]
    u = _unit(estimate(_load(tmp_path, lines)), "m-a", None)
    assert u["mean"] == 0.5
    assert u["status"] == "no_change"
    assert u["reason"] == "|shift| 0.0 < min_shift 0.02"


def test_no_prior_insufficient(tmp_path):
    lines = [_rec(f"r{i}", chosen="m-z", feedback=[("route_correct", 1)])
             for i in range(30)]
    u = _unit(estimate(_load(tmp_path, lines)), "m-z", None)
    assert u["status"] == "insufficient" and u["reason"] == "no prior"
    assert u["q0"] is None and u["mean"] is None and u["n_labels"] == 30


def test_calibrated_src_is_not_a_prior(tmp_path):
    """A `calibrated`/`customer` quality term is a previous recommendation or
    a declaration, never the prior for recalibration."""
    sc = _cands(("m-a", 0.9, "calibrated"), ("m-b", 0.8, "customer"))
    lines = [_rec("r1", scoring=sc, feedback=[("route_correct", 1)]),
             _rec("r2", chosen="m-b", scoring=sc, feedback=[("route_correct", 1)])]
    est = estimate(_load(tmp_path, lines))
    assert _unit(est, "m-a", None)["reason"] == "no prior"
    assert _unit(est, "m-b", None)["reason"] == "no prior"
    # ...but a published term elsewhere in the group is used.
    lines.append(_rec("r3", ts="2026-09-19T00:00:00+00:00", feedback=[]))
    assert _unit(estimate(_load(tmp_path, lines)), "m-a", None)["q0"] == 0.5


def test_priors_override(tmp_path):
    est = estimate(_load(tmp_path, _hand_example()), priors={"m-a": 0.8})
    u = _unit(est, "m-a", None)
    assert (u["q0"], u["q0_source"]) == (0.8, "prior")
    assert (u["alpha"], u["beta"]) == (26.0, 6.0)
    assert est["options"]["priors"] == {"m-a": 0.8}


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 1.2, float("nan")])
def test_degenerate_prior_refused(tmp_path, bad):
    records = _load(tmp_path, _hand_example())
    with pytest.raises(ValueError):
        estimate(records, priors={"m-a": bad})
    lines = [_rec("r", scoring=_cands(("m-a", bad, "published")))]
    assert _unit(estimate(_load(tmp_path, lines)), "m-a", None)["reason"] == "no prior"


def test_newest_prior_record_wins(tmp_path):
    lines = [_rec("old", ts="2026-09-19T00:00:00+00:00",
                  scoring=_cands(("m-a", 0.4, "tier_default"))),
             _rec("new", ts="2026-09-20T00:00:00+00:00",
                  scoring=_cands(("m-a", 0.7, "published")))]
    u = _unit(estimate(_load(tmp_path, lines)), "m-a", None)
    assert (u["q0"], u["q0_source"]) == (0.7, "published")


# --- grouping -----------------------------------------------------------

def test_registry_versions_never_pooled(tmp_path):
    v1 = [_rec(f"a{i}", reg="reg-v1", ts="2026-09-19T00:00:00+00:00",
               feedback=[("route_correct", 1)]) for i in range(4)]
    v2 = [_rec(f"b{i}", reg="reg-v2", ts="2026-09-21T00:00:00+00:00",
               feedback=[("route_correct", 0)]) for i in range(3)]
    records = _load(tmp_path, v1 + v2)

    est = estimate(records)                     # newest group by default
    assert est["registry_version"] == "reg-v2"
    u = _unit(est, "m-a", None)
    assert (u["n_rows"], u["alpha"], u["beta"]) == (3, 5.0, 8.0)

    est = estimate(records, registry_version="reg-v1")
    assert est["registry_version"] == "reg-v1"
    u = _unit(est, "m-a", None)
    assert (u["n_rows"], u["alpha"], u["beta"]) == (4, 9.0, 5.0)

    with pytest.raises(ValueError):
        estimate(records, registry_version="reg-v9")


def test_newest_group_is_latest_ts_not_label(tmp_path):
    lines = [_rec("z", reg="reg-z", ts="2026-09-01T00:00:00+00:00"),
             _rec("a", reg="reg-a", ts="2026-09-02T00:00:00+00:00"),
             _rec("u", reg=None, ts="2026-08-01T00:00:00+00:00")]
    records = _load(tmp_path, lines)
    assert estimate(records)["registry_version"] == "reg-a"
    assert estimate(records, registry_version="(unknown)")["units"][0]["n_rows"] == 1


# --- units --------------------------------------------------------------

def test_workload_and_star_units(tmp_path):
    lines = [_rec("c1", workload="chat"), _rec("c2", workload="code"),
             _rec("c3", workload=None), _rec("b1", chosen="m-b", workload="chat")]
    est = estimate(_load(tmp_path, lines))
    keys = [(u["model"], u["workload"]) for u in est["units"]]
    assert keys == [("m-a", "chat"), ("m-a", "code"), ("m-a", None),
                    ("m-b", "chat"), ("m-b", None)]
    assert _unit(est, "m-a", None)["n_rows"] == 3   # untagged rows count in *
    assert _unit(est, "m-a", "chat")["n_rows"] == 1
    assert _unit(est, "m-b", None)["q0_source"] == "tier_default"


# --- recommendation file -------------------------------------------------

def test_recommendation_shape_and_determinism(tmp_path):
    records = _load(tmp_path, _hand_example())
    rec1 = build_recommendation(estimate(records), export_sha256="ab" * 32)
    rec2 = build_recommendation(estimate(list(reversed(records))),
                                export_sha256="ab" * 32)
    assert rec1["schema"] == RECOMMENDATION_SCHEMA
    assert rec1["export_sha256"] == "ab" * 32
    assert rec1["registry_version"] == "reg-v1"
    assert len(rec1["set_id"]) == 16
    assert rec1["set_id"] == rec2["set_id"]
    assert dump_recommendation(rec1) == dump_recommendation(rec2)
    assert rec1["set_id"] == recommendation_set_id(rec1)


def test_set_id_is_the_specified_hash(tmp_path):
    import hashlib
    rec = build_recommendation(estimate(_load(tmp_path, _hand_example())),
                               export_sha256="00")
    body = json.dumps({k: v for k, v in rec.items() if k != "set_id"},
                      sort_keys=True, separators=(",", ":"))
    assert rec["set_id"] == hashlib.sha256(body.encode()).hexdigest()[:16]


def test_set_id_changes_with_any_unit_score(tmp_path):
    rec = build_recommendation(estimate(_load(tmp_path, _hand_example())),
                               export_sha256="00")
    for field in ("mean", "ci_low", "ci_high", "alpha", "beta", "q0"):
        mutated = json.loads(json.dumps(rec))
        mutated["units"][0][field] += 0.000001
        assert recommendation_set_id(mutated) != rec["set_id"], field
    other = build_recommendation(
        estimate(_load(tmp_path, _hand_example()), priors={"m-a": 0.6}),
        export_sha256="00")
    assert other["set_id"] != rec["set_id"]
