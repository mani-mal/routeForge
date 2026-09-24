"""Tests for the P2b-3 replay diff (`app.eval.calibration.replay`), the
report (`render_report`) and `scripts/quality_recommend.py` (Task 4).

The equivalence fixtures are built with the REAL
`DefaultRoutingStrategy().rank(...)` and JSON-round-tripped through the
export line shape, so the replay is checked against the scoring records the
router actually writes -- not against a hand-written guess at them. Only the
strategy's *inputs* (quality, latency, availability) are stubbed, so each
case can pin a specific term shape: a `peer_prior` latency, a latency above
the `max_latency_norm` cap, a `disabled` term, a cost tie.
"""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.config.schema import PoolCandidate, SignalsCfg
from app.eval.calibration import (
    REPLAY_CAVEAT,
    build_recommendation,
    estimate,
    render_report,
    replay,
)
from app.eval.outcomes import load_export
from app.routing.quality import QualityScore
from app.routing.signals import SignalValue
from app.routing.strategy import DefaultRoutingStrategy

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "quality_recommend.py"

WEIGHTS = {"quality": 0.4, "cost": 0.3, "latency": 0.2, "availability": 0.1}

# --- real scoring records ----------------------------------------------


class _Quality:
    def __init__(self, table: dict[str, tuple[float, str]]):
        self._t = table

    def resolve(self, model, task_class=None):
        v, src = self._t[model]
        return QualityScore(v, src)


class _Signals:
    def __init__(self, lat: dict[str, SignalValue], avail: dict[str, SignalValue]):
        self._lat, self._avail = lat, avail

    def latency(self, name, tier, now=None):
        return self._lat[name]

    def availability(self, name, now=None):
        return self._avail[name]


class _Ctx:
    def __init__(self, workload):
        self.workload = workload


def _scoring(cands: list[tuple[str, str, float]], *,
             quality: dict[str, tuple[float, str]],
             lat: dict[str, SignalValue] | None = None,
             avail: dict[str, SignalValue] | None = None,
             weights: dict[str, float] = WEIGHTS,
             cfg: SignalsCfg | None = None, workload: str | None = "chat") -> dict:
    """A real `RankResult.to_record()`, JSON round-tripped."""
    cfg = cfg or SignalsCfg()
    lat = lat or {}
    avail = avail or {}
    eligible = [(PoolCandidate(name=n, provider="onprem", tier=t), est)
                for n, t, est in cands]
    signals = _Signals(
        {n: lat.get(n, SignalValue(cfg.tier_latency_ms[t], "tier_default", 0))
         for n, t, _ in cands},
        {n: avail.get(n, SignalValue(1.0, "neutral", 0)) for n, _, _ in cands})
    result = DefaultRoutingStrategy().rank(
        eligible, ctx=_Ctx(workload), tokens_after=100, weights=weights,
        quality_resolver=_Quality(quality), signals=signals, signals_cfg=cfg)
    return json.loads(json.dumps(result.to_record()))


def _real_cases() -> list[dict]:
    """A spread of real scoring records covering every term shape."""
    return [
        # 1. plain cheap-tier set, tier-default latency and neutral availability
        _scoring([("m-a", "cheap", 0.001), ("m-b", "cheap", 0.002)],
                 quality={"m-a": (0.5, "tier_default"), "m-b": (0.7, "published")}),
        # 2. peer_prior: m-c unmeasured borrows the measured peers' median
        _scoring([("m-a", "cheap", 0.001), ("m-b", "cheap", 0.001),
                  ("m-c", "cheap", 0.0005)],
                 quality={"m-a": (0.5, "tier_default"), "m-b": (0.5, "published"),
                          "m-c": (0.5, "tier_default")},
                 lat={"m-a": SignalValue(3000.0, "measured", 40),
                      "m-b": SignalValue(9000.0, "measured", 40)}),
        # 3. a latency far above the max_latency_norm cap (18 s / 1000 > 3.0)
        _scoring([("m-a", "cheap", 0.001), ("m-b", "cheap", 0.0001)],
                 quality={"m-a": (0.6, "published"), "m-b": (0.9, "published")},
                 lat={"m-a": SignalValue(400.0, "measured", 30),
                      "m-b": SignalValue(18000.0, "measured", 30)},
                 avail={"m-b": SignalValue(0.4, "measured", 30)},
                 # A small latency weight so the cap decides the winner:
                 # capped, m-b wins; uncapped, m-a would.
                 weights={"quality": 0.4, "cost": 0.3, "latency": 0.02,
                          "availability": 0.1}),
        # 4. exact score tie -> the est_cost tie-break decides
        _scoring([("m-a", "cheap", 0.002), ("m-b", "cheap", 0.002),
                  ("m-c", "cheap", 0.001)],
                 quality={"m-a": (0.5, "tier_default"), "m-b": (0.5, "tier_default"),
                          "m-c": (0.5, "tier_default")},
                 weights={"quality": 0.4, "cost": 0.0, "latency": 0.2,
                          "availability": 0.1}),
        # 5. signals disabled, mixed tiers, zero-cost set (cost_norm = 0)
        _scoring([("m-a", "cheap", 0.0), ("m-d", "mid", 0.0)],
                 quality={"m-a": (0.5, "tier_default"), "m-d": (0.75, "tier_default")},
                 lat={"m-a": SignalValue(200.0, "disabled", 0),
                      "m-d": SignalValue(500.0, "disabled", 0)},
                 avail={"m-a": SignalValue(1.0, "disabled", 0),
                        "m-d": SignalValue(1.0, "disabled", 0)},
                 cfg=SignalsCfg(enabled=False)),
        # 6. single candidate
        _scoring([("m-a", "cheap", 0.001)], quality={"m-a": (0.5, "tier_default")}),
        # 7. measured availability decides: m-b is dearer but far more available
        _scoring([("m-a", "cheap", 0.0009), ("m-b", "cheap", 0.001)],
                 quality={"m-a": (0.5, "tier_default"), "m-b": (0.5, "tier_default")},
                 avail={"m-a": SignalValue(0.5, "measured", 50),
                        "m-b": SignalValue(1.0, "measured", 50)}),
    ]


def _outcome(rid: str, scoring: dict | None, *, chosen: str | None = None,
             workload: str | None = "chat", reg: str | None = "reg-v1",
             status: str = "ok", cost: float = 0.001,
             feedback: list[tuple[str, object]] = (),
             ts: str = "2026-09-20T00:00:00+00:00") -> dict:
    if chosen is None and scoring is not None:
        chosen = scoring["candidates"][0]["name"]
    return {
        "request_id": rid, "ts": ts, "status": status,
        "endpoint_registry_version": reg, "workload": workload,
        "chosen": chosen, "cache_hit": False, "cost_usd": cost,
        "scoring": scoring,
        "feedback": [{"score_name": n, "value": v, "source": "admin", "ts": ts}
                     for n, v in feedback],
    }


def _rec(units: list[dict], reg: str | None = "reg-v1") -> dict:
    return {"registry_version": reg, "units": units}


def _unit(model: str, workload: str | None, mean: float,
          status: str = "recommended") -> dict:
    return {"model": model, "workload": workload, "mean": mean, "status": status}


# --- equivalence ------------------------------------------------------


def test_the_real_cases_cover_the_term_shapes():
    """Guard the fixture itself: the shapes the brief names must be present,
    or the equivalence test proves less than it claims."""
    cases = _real_cases()
    lat_srcs = {c["latency"]["src"] for s in cases for c in s["candidates"]}
    assert {"peer_prior", "measured", "tier_default", "disabled"} <= lat_srcs
    capped = [c for s in cases for c in s["candidates"]
              if c["latency"]["ms"] / s["latency_scale_ms"] > s["max_latency_norm"]]
    assert capped
    assert cases[2]["candidates"][0]["name"] == "m-b"   # the capped one wins
    # Case 4 is a genuine score tie that only est_cost breaks.
    tie = cases[3]["candidates"]
    assert tie[0]["score"] == tie[1]["score"] == tie[2]["score"]
    assert tie[0]["name"] == "m-c"
    assert cases[6]["candidates"][0]["name"] == "m-b"   # availability decides


def test_unchanged_replay_reproduces_every_real_logged_choice():
    records = [_outcome(f"r{i}", s) for i, s in enumerate(_real_cases())]
    out = replay(records, _rec([]))
    assert out["equivalence_ok"] is True
    assert out["mismatches"] == []
    wl = out["per_workload"]["chat"]
    assert wl["decisions_total"] == len(records)
    assert wl["decisions_changed"] == 0
    assert wl["moves"] == {}


def test_non_recommended_units_never_change_a_decision():
    records = [_outcome(f"r{i}", s) for i, s in enumerate(_real_cases())]
    units = [_unit("m-a", None, 0.99, status="insufficient"),
             _unit("m-a", None, 0.99, status="no_change")]
    out = replay(records, _rec(units))
    assert out["per_workload"]["chat"]["decisions_changed"] == 0


def test_a_tampered_record_fails_equivalence():
    s = _real_cases()[0]
    tampered = copy.deepcopy(s)
    tampered["candidates"].reverse()
    records = [_outcome("good", s), _outcome("bad", tampered)]
    out = replay(records, _rec([]))
    assert out["equivalence_ok"] is False
    assert [m["request_id"] for m in out["mismatches"]] == ["bad"]
    assert out["mismatches"][0]["logged"] == tampered["candidates"][0]["name"]


def test_an_unrecomputable_scoring_record_fails_equivalence():
    s = copy.deepcopy(_real_cases()[0])
    del s["candidates"][1]["latency"]
    out = replay([_outcome("r", s)], _rec([]))
    assert out["equivalence_ok"] is False
    assert "not recomputable" in out["mismatches"][0]["reason"]


def test_records_without_scoring_are_skipped_not_replayed():
    records = [_outcome("r0", _real_cases()[0]),
               _outcome("r1", None, chosen=None, status="no_eligible")]
    out = replay(records, _rec([]))
    assert out["equivalence_ok"] is True
    assert out["per_workload"]["chat"]["decisions_total"] == 1
    assert out["n_skipped_no_scoring"] == 1


def test_only_the_recommendation_registry_group_is_replayed():
    s = _real_cases()[0]
    tampered = copy.deepcopy(s)
    tampered["candidates"].reverse()
    records = [_outcome("in", s, reg="reg-v2"),
               _outcome("other", tampered, reg="reg-v1")]
    out = replay(records, _rec([], reg="reg-v2"))
    assert out["equivalence_ok"] is True       # reg-v1's bad record is not replayed
    assert out["per_workload"]["chat"]["decisions_total"] == 1


# --- proposed replay --------------------------------------------------


def _flip_scoring() -> dict:
    # m-a wins on cost; m-b is close behind on quality.
    return _scoring([("m-a", "cheap", 0.001), ("m-b", "cheap", 0.0012)],
                    quality={"m-a": (0.5, "tier_default"), "m-b": (0.5, "tier_default")},
                    workload=None)


def test_a_proposed_quality_flip_is_counted_under_its_workload():
    s = _flip_scoring()
    assert s["candidates"][0]["name"] == "m-a"
    records = [
        _outcome("c1", s, workload="chat"),
        _outcome("c2", s, workload="chat"),
        _outcome("k1", s, workload="code"),
        _outcome("u1", s, workload=None),
    ]
    # The chat-specific unit lifts m-b in chat only; code falls back to the
    # (m-b, *) unit, which is too small to flip.
    units = [_unit("m-b", "chat", 0.9), _unit("m-b", None, 0.51)]
    out = replay(records, _rec(units))
    assert out["equivalence_ok"] is True
    chat = out["per_workload"]["chat"]
    assert (chat["decisions_total"], chat["decisions_changed"]) == (2, 2)
    assert chat["moves"] == {"m-a->m-b": 2}
    code = out["per_workload"]["code"]
    assert (code["decisions_total"], code["decisions_changed"]) == (1, 0)
    assert code["moves"] == {}
    assert out["per_workload"]["(untagged)"]["decisions_changed"] == 0


def test_a_proposed_tie_is_broken_by_est_cost_not_logged_order():
    """X leads on quality alone (cost weight 0). Proposing X down to Y's
    quality makes an exact score tie, which the strategy breaks by est_cost
    -- Y, the cheaper one, even though X is first in the logged order."""
    s = _scoring([("m-x", "cheap", 0.002), ("m-y", "cheap", 0.001)],
                 quality={"m-x": (0.6, "published"), "m-y": (0.5, "published")},
                 weights={"quality": 0.4, "cost": 0.0, "latency": 0.2,
                          "availability": 0.1})
    assert s["candidates"][0]["name"] == "m-x"
    out = replay([_outcome("r", s)], _rec([_unit("m-x", None, 0.5)]))
    assert out["per_workload"]["chat"]["moves"] == {"m-x->m-y": 1}


def test_the_wildcard_unit_applies_when_the_workload_has_none():
    s = _flip_scoring()
    records = [_outcome("k1", s, workload="code")]
    out = replay(records, _rec([_unit("m-b", None, 0.9)]))
    assert out["per_workload"]["code"]["moves"] == {"m-a->m-b": 1}


def test_a_new_pick_never_served_is_flagged_no_evidence():
    s = _flip_scoring()
    records = [_outcome("c1", s, workload="chat")]
    out = replay(records, _rec([_unit("m-b", "chat", 0.9)]))
    assert out["per_workload"]["chat"]["new_pick_evidence"] == {"m-b": "no evidence"}


def test_new_pick_evidence_comes_from_its_own_logged_rows():
    s = _flip_scoring()
    records = [
        _outcome("c1", s, workload="chat"),
        # m-b was served for chat twice (once verify_failed), and for code once.
        _outcome("b1", s, chosen="m-b", workload="chat", cost=0.002,
                 feedback=[("quality", 0.8)]),
        _outcome("b2", s, chosen="m-b", workload="chat", status="verify_failed",
                 cost=0.0, feedback=[("quality", 0.2)]),
        _outcome("b3", s, chosen="m-b", workload="code", cost=0.5,
                 feedback=[("quality", 0.0)]),
    ]
    out = replay(records, _rec([_unit("m-b", "chat", 0.9)]))
    ev = out["per_workload"]["chat"]["new_pick_evidence"]["m-b"]
    assert ev == {"served": 2, "quality_mean": 0.5, "verify_failed": 1,
                  "cost_mean": 0.002}


# --- report -----------------------------------------------------------


def test_report_opens_with_the_caveat_and_lists_units_and_diff():
    s = _flip_scoring()
    records = [_outcome("c1", s, workload="chat")]
    est = {"registry_version": "reg-v1", "options": {}, "units": [
        {"model": "m-b", "workload": "chat", "q0": 0.5, "q0_source": "tier_default",
         "mean": 0.9, "ci_low": 0.8, "ci_high": 0.95, "shift": 0.4, "n_labels": 40,
         "n_rows": 40, "label_coverage": 1.0, "status": "recommended", "reason": None},
        {"model": "m-c", "workload": None, "q0": None, "q0_source": None,
         "mean": None, "ci_low": None, "ci_high": None, "shift": None, "n_labels": 0,
         "n_rows": 3, "label_coverage": 0.0, "status": "insufficient",
         "reason": "no prior"},
    ]}
    rp = replay(records, _rec(est["units"]))
    text = render_report(est, rp)
    assert text.startswith(REPLAY_CAVEAT)
    assert REPLAY_CAVEAT == (
        "This replay shows which logged decisions would change. It cannot show "
        "what the changed decisions would have produced: production observes "
        "only the served model.")
    assert "m-b" in text and "no prior" in text
    assert "m-a->m-b" in text and "no evidence" in text


# --- CLI --------------------------------------------------------------


def _write_export(path: Path, records: list[dict]) -> None:
    lines = []
    for r in records:
        outcome = {k: v for k, v in r.items() if k != "feedback"}
        lines.append(json.dumps({"schema": "routeforge.outcome/v1",
                                 "outcome": outcome, "feedback": r["feedback"]}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _cli_records() -> list[dict]:
    """m-b is served a lot for chat and labelled well; its prior is 0.5, so
    it is recommended upward and flips the m-a decisions in chat."""
    s = _flip_scoring()
    recs = [_outcome(f"a{i}", s, workload="chat") for i in range(5)]
    recs += [_outcome(f"b{i}", s, chosen="m-b", workload="chat",
                      feedback=[("quality", 1.0), ("route_correct", 1)])
             for i in range(40)]
    return recs


def _run(*args: str) -> subprocess.CompletedProcess:
    env = {"LITELLM_LOCAL_MODEL_COST_MAP": "True", "PATH": "/usr/bin:/bin"}
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                          text=True, env=env, cwd=str(REPO_ROOT))


def test_cli_writes_both_files_byte_identically(tmp_path):
    export = tmp_path / "export.ndjson"
    _write_export(export, _cli_records())
    out1, out2 = tmp_path / "o1", tmp_path / "o2"
    r1 = _run("--export", str(export), "--out", str(out1))
    r2 = _run("--export", str(export), "--out", str(out2))
    assert r1.returncode == 0, r1.stderr
    assert r2.returncode == 0, r2.stderr
    for name in ("recommendation.json", "report.md"):
        assert (out1 / name).read_bytes() == (out2 / name).read_bytes()
    report = (out1 / "report.md").read_text(encoding="utf-8")
    assert report.startswith(REPLAY_CAVEAT)
    assert "m-a->m-b" in report
    rec = json.loads((out1 / "recommendation.json").read_text(encoding="utf-8"))
    assert rec["export_sha256"] == hashlib.sha256(export.read_bytes()).hexdigest()
    # The CLI's recommendation is exactly the library's.
    records, _ = load_export(str(export))
    expect = build_recommendation(estimate(records),
                                  export_sha256=rec["export_sha256"])
    assert rec == expect
    assert any(u["status"] == "recommended" and u["model"] == "m-b"
               for u in rec["units"])


def test_cli_exits_2_on_an_equivalence_failure(tmp_path):
    recs = _cli_records()
    tampered = copy.deepcopy(recs[0])
    tampered["request_id"] = "tampered"
    tampered["scoring"]["candidates"].reverse()
    tampered["chosen"] = "m-b"
    export = tmp_path / "export.ndjson"
    _write_export(export, recs + [tampered])
    out = tmp_path / "o"
    r = _run("--export", str(export), "--out", str(out))
    assert r.returncode == 2
    assert "tampered" in r.stderr
    assert not (out / "report.md").exists()
    assert not (out / "recommendation.json").exists()


@pytest.mark.parametrize("extra", [
    ["--min-labels", "-1"],
    ["--registry-version", "nope"],
    ["--priors", "/nonexistent/priors.json"],
])
def test_cli_exits_1_on_option_or_load_errors(tmp_path, extra):
    export = tmp_path / "export.ndjson"
    _write_export(export, _cli_records())
    r = _run("--export", str(export), "--out", str(tmp_path / "o"), *extra)
    assert r.returncode == 1, r.stderr


def test_cli_exits_1_on_a_missing_export(tmp_path):
    r = _run("--export", str(tmp_path / "missing.ndjson"), "--out", str(tmp_path / "o"))
    assert r.returncode == 1


def test_cli_applies_a_priors_file(tmp_path):
    export = tmp_path / "export.ndjson"
    _write_export(export, _cli_records())
    priors = tmp_path / "priors.json"
    priors.write_text(json.dumps({"m-b": 0.3}), encoding="utf-8")
    out = tmp_path / "o"
    r = _run("--export", str(export), "--out", str(out), "--priors", str(priors))
    assert r.returncode == 0, r.stderr
    rec = json.loads((out / "recommendation.json").read_text(encoding="utf-8"))
    assert rec["options"]["priors"] == {"m-b": 0.3}
