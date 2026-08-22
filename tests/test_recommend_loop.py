"""Offline recommend-loop tests (F4 Task 1): tools/recommend/*.

Covers the 7 invariants from the task brief:
1. serving path never imports tools.recommend (subprocess isolation)
2. mine never mutates config/ and `applied` is always False
3. aggregate() correctness (incl. empty input)
4. each rule fires on its trigger and stays silent otherwise
5. proposed_routing_policy_version correctness
6. out-path guard (overwrite refusal, traversal, config/ refusal)
7. min-requests floor

No pytest-asyncio in this repo (matches tests/test_apps_store.py's pattern):
async DB work runs through a small asyncio.run helper.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from app.apps.bootstrap import seed_demo_apps
from app.apps.db import Database
from app.apps import store
from app.config.loader import _policy_version, _read
from app.config.schema import RoutingCfg
from tools.recommend.aggregate import aggregate
from tools.recommend.mine import main as mine_main
from tools.recommend.report import (
    RecommendationReport, ReportPathError, Window,
    compute_proposed_policy_version, safe_out_path, write_report,
)
from tools.recommend.rules import (
    Recommendation, cheap_underutilization, frontier_overutilization,
    high_tier_error_rate, onprem_capacity_pressure,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

UTC = dt.timezone.utc


def _row(**over):
    base = dict(
        model="m", provider="onprem", tier="cheap", prompt_tokens=1,
        completion_tokens=1, total_tokens=2, quality=0.5,
        quality_source="tier_default", cost_usd=0.001, baseline_usd=0.01,
        latency_ms=10.0, sensitivity="public", onprem_only=False,
        status="ok", ts=dt.datetime(2026, 1, 1, tzinfo=UTC),
    )
    base.update(over)
    return base


def _routing_cfg(**over) -> RoutingCfg:
    base = dict(
        tiers=["cheap", "mid", "frontier"],
        score_on="post_compression",
        weights={"quality": 1.0, "cost": 0.6, "latency": 0.3, "capacity": 0.2},
        tier_quality={"cheap": 0.5, "mid": 0.75, "frontier": 1.0},
        complexity_thresholds={"cheap_max": 0.35, "mid_max": 0.70},
        confidence_escalation_threshold=0.60,
        max_escalations=1,
        risk_floors={"high": "frontier", "medium": "mid", "low": "cheap"},
        model_pool=[],
        fallback={"max_retries_same_provider": 1, "backoff_base_ms": 100},
    )
    base.update(over)
    return RoutingCfg(**base)


def _run(coro):
    return asyncio.run(coro)


def _seed_rows(db_url: str, rows: list[dict]) -> None:
    """Write `rows` (dicts as returned by `_row`) as UsageRecords under
    application_id="acme" to a fresh DB at `db_url`."""

    async def go():
        db = Database(url=db_url, seed=seed_demo_apps)
        async with db.session() as s:
            for i, r in enumerate(rows):
                await store.record_usage(
                    s, application_id="acme", request_id=f"req-{i}",
                    model=r["model"], provider=r["provider"], tier=r["tier"],
                    prompt_tokens=r["prompt_tokens"],
                    completion_tokens=r["completion_tokens"],
                    total_tokens=r["total_tokens"], cost_usd=r["cost_usd"],
                    baseline_usd=r["baseline_usd"], latency_ms=r["latency_ms"],
                    sensitivity=r["sensitivity"], onprem_only=r["onprem_only"],
                    status=r["status"], quality=r["quality"],
                    quality_source=r["quality_source"])
            await s.commit()
        await db.dispose()

    _run(go())


# --- 1. offline isolation ----------------------------------------------------

def test_serving_graph_does_not_import_tools_recommend():
    """Nothing under app/ imports tools.recommend — importing the whole
    server module graph must never pull it in (rule 4 / offline isolation)."""
    env = os.environ.copy()
    env.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    result = subprocess.run(
        [sys.executable, "-c",
         "import app.main, sys; "
         "assert 'tools.recommend' not in sys.modules, "
         "'tools.recommend leaked into the serving import graph'"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, env=env)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


# --- 2. never mutates config / applied is always False -----------------------

def test_mine_full_run_never_touches_config_and_applied_is_false(tmp_path):
    config_dir = REPO_ROOT / "config"
    before_files = {
        p: p.read_bytes() for p in sorted(config_dir.rglob("*")) if p.is_file()}

    rows = [_row(tier="cheap") for _ in range(10)] + [_row(tier="mid") for _ in range(10)]
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'usage.db'}"
    _seed_rows(db_url, rows)

    out_path = tmp_path / "report.yaml"
    rc = mine_main(["--db", db_url, "--out", str(out_path), "--min-requests", "5"])
    assert rc == 0
    assert out_path.exists()

    after_files = {
        p: p.read_bytes() for p in sorted(config_dir.rglob("*")) if p.is_file()}
    assert after_files == before_files, "mine must never create/modify anything under config/"

    report = yaml.safe_load(out_path.read_text())
    assert report["applied"] is False


def test_recommendation_report_applied_is_structurally_false():
    """`applied` is typed Literal[False] — it cannot be constructed True."""
    report = RecommendationReport(
        generated_window=Window(n_requests=0), current_routing_policy_version="abc",
        recommendations=[])
    assert report.applied is False
    with pytest.raises(Exception):
        RecommendationReport(
            generated_window=Window(n_requests=0),
            current_routing_policy_version="abc", recommendations=[], applied=True)


# --- 3. aggregate correctness -------------------------------------------------

def test_aggregate_empty_input_is_well_defined():
    agg = aggregate([])
    assert agg.n_requests == 0
    assert agg.n_ok == 0
    assert agg.tiers == {}
    assert agg.since is None and agg.until is None
    assert agg.overall_reject_error_rate == 0.0
    assert agg.onprem_only_share == 0.0
    assert agg.onprem_only_reject_share == 0.0


def test_aggregate_tier_shares_quality_cost_latency():
    rows = [
        _row(tier="cheap", quality=0.6, cost_usd=0.001, latency_ms=100.0),
        _row(tier="cheap", quality=0.8, cost_usd=0.003, latency_ms=200.0),
        _row(tier="mid", quality=0.9, cost_usd=0.01, latency_ms=300.0),
    ]
    agg = aggregate(rows)
    assert agg.n_requests == 3
    assert agg.n_ok == 3
    cheap = agg.tiers["cheap"]
    assert cheap.ok_count == 2
    assert cheap.share == pytest.approx(2 / 3)
    assert cheap.avg_quality == pytest.approx(0.7)
    assert cheap.avg_cost_usd == pytest.approx(0.002)
    assert cheap.avg_latency_ms == pytest.approx(150.0)
    mid = agg.tiers["mid"]
    assert mid.ok_count == 1
    assert mid.share == pytest.approx(1 / 3)


def test_aggregate_reject_and_error_rates_overall_and_per_tier():
    rows = [
        _row(tier="mid", status="ok"),
        _row(tier="mid", status="ok"),
        _row(tier="mid", status="error"),
        _row(tier="-", status="rejected"),
    ]
    agg = aggregate(rows)
    assert agg.n_requests == 4
    assert agg.n_ok == 2
    assert agg.n_error == 1
    assert agg.n_rejected == 1
    assert agg.overall_reject_error_rate == pytest.approx(0.5)
    mid = agg.tiers["mid"]
    assert mid.ok_count == 2
    assert mid.error_count == 1
    assert mid.total_count == 3
    assert mid.error_rate == pytest.approx(1 / 3)


def test_aggregate_onprem_only_shares():
    rows = [
        _row(status="ok", onprem_only=True),
        _row(status="ok", onprem_only=False),
        _row(status="rejected", tier="-", onprem_only=True),
        _row(status="rejected", tier="-", onprem_only=False),
    ]
    agg = aggregate(rows)
    assert agg.onprem_only_share == pytest.approx(0.5)
    assert agg.onprem_only_reject_share == pytest.approx(0.5)


# --- 4. rules fire on trigger, stay silent otherwise --------------------------

def test_frontier_overutilization_fires_and_stays_silent():
    routing = _routing_cfg()
    triggering = aggregate(
        [_row(tier="mid", quality=0.80) for _ in range(6)]
        + [_row(tier="frontier", quality=0.82) for _ in range(6)])
    rec = frontier_overutilization(triggering, routing)
    assert rec is not None
    assert rec.field_path == "complexity_thresholds.mid_max"
    assert rec.current_value == pytest.approx(0.70)
    assert rec.recommended_value == pytest.approx(0.75)
    assert "frontier_share" in rec.evidence
    assert rec.rationale

    quiet = aggregate(
        [_row(tier="mid", quality=0.80) for _ in range(10)]
        + [_row(tier="frontier", quality=0.82) for _ in range(2)])
    assert frontier_overutilization(quiet, routing) is None


def test_cheap_underutilization_fires_and_stays_silent():
    routing = _routing_cfg()
    triggering = aggregate(
        [_row(tier="cheap", quality=0.55) for _ in range(5)]
        + [_row(tier="mid", quality=0.75) for _ in range(25)])
    rec = cheap_underutilization(triggering, routing)
    assert rec is not None
    assert rec.field_path == "complexity_thresholds.cheap_max"
    assert rec.current_value == pytest.approx(0.35)
    assert rec.recommended_value == pytest.approx(0.40)
    assert "cheap_share" in rec.evidence

    # Cheap is under-utilized, but its quality is below the cheap rung — stay silent.
    quiet = aggregate(
        [_row(tier="cheap", quality=0.30) for _ in range(5)]
        + [_row(tier="mid", quality=0.75) for _ in range(25)])
    assert cheap_underutilization(quiet, routing) is None


def test_high_tier_error_rate_fires_and_stays_silent():
    routing = _routing_cfg()
    triggering = aggregate(
        [_row(tier="mid", status="ok") for _ in range(8)]
        + [_row(tier="mid", status="error") for _ in range(2)])
    rec = high_tier_error_rate(triggering, routing)
    assert rec is not None
    assert rec.field_path == "fallback (tier=mid)"
    assert rec.current_value == rec.recommended_value == routing.fallback
    assert "error_rate" in rec.evidence

    quiet = aggregate([_row(tier="mid", status="ok") for _ in range(10)])
    assert high_tier_error_rate(quiet, routing) is None


def test_high_tier_error_rate_ignores_the_synthetic_reject_bucket_tier():
    """The `tier="-"` bucket (budget rejects — app/api/routes.py) is not a
    real configured tier. Even with a high error/reject rate concentrated
    there, the rule must not point at a non-existent tier "-"; it must still
    fire for a genuine high-error real tier alongside it."""
    routing = _routing_cfg()
    agg = aggregate(
        # Reject-bucket rows: high "error" rate, but tier "-" is not in
        # routing.tiers and must never be recommended for review.
        [_row(tier="-", status="rejected") for _ in range(9)]
        + [_row(tier="-", status="ok") for _ in range(1)]
        # A genuine real tier ("mid") with its own elevated error rate.
        + [_row(tier="mid", status="ok") for _ in range(8)]
        + [_row(tier="mid", status="error") for _ in range(2)])
    rec = high_tier_error_rate(agg, routing)
    assert rec is not None
    assert rec.field_path == "fallback (tier=mid)"
    assert "tier=-" not in rec.evidence
    assert "tier=- " not in rec.field_path

    # With ONLY the reject-bucket rows present (no real tier breaching the
    # threshold), the rule must stay completely silent.
    only_reject_bucket = aggregate(
        [_row(tier="-", status="rejected") for _ in range(9)]
        + [_row(tier="-", status="ok") for _ in range(1)])
    assert high_tier_error_rate(only_reject_bucket, routing) is None


def test_onprem_capacity_pressure_fires_and_stays_silent():
    routing = _routing_cfg()
    triggering = aggregate(
        [_row(tier="-", status="rejected", onprem_only=True) for _ in range(4)]
        + [_row(tier="-", status="rejected", onprem_only=False) for _ in range(2)])
    rec = onprem_capacity_pressure(triggering, routing)
    assert rec is not None
    assert rec.field_path == "infra.onprem_capacity"
    assert rec.current_value is None
    assert rec.recommended_value
    assert "onprem_only_reject_share" in rec.evidence

    quiet = aggregate(
        [_row(tier="-", status="rejected", onprem_only=False) for _ in range(6)])
    assert onprem_capacity_pressure(quiet, routing) is None


# --- 5. proposed_routing_policy_version correctness ---------------------------

def test_proposed_policy_version_changes_on_concrete_delta_and_leaves_raw_untouched():
    routing_raw = _read(str(REPO_ROOT / "config"), "routing.yaml")
    pricing_raw = _read(str(REPO_ROOT / "config"), "pricing.yaml")
    snapshot = {"routing": dict(routing_raw), "pricing": dict(pricing_raw)}
    current_hash = _policy_version(routing_raw, pricing_raw)

    current_mid_max = routing_raw["complexity_thresholds"]["mid_max"]
    rec = Recommendation(
        field_path="complexity_thresholds.mid_max",
        current_value=current_mid_max,
        recommended_value=current_mid_max + 0.05,
        rationale="test", evidence="test")

    proposed = compute_proposed_policy_version([rec], routing_raw, pricing_raw)
    assert proposed is not None
    assert proposed != current_hash

    # The real dicts passed in are never mutated.
    assert routing_raw["complexity_thresholds"]["mid_max"] == current_mid_max
    assert routing_raw == snapshot["routing"]
    assert pricing_raw == snapshot["pricing"]


def test_proposed_policy_version_is_none_with_no_concrete_deltas():
    routing_raw = _read(str(REPO_ROOT / "config"), "routing.yaml")
    pricing_raw = _read(str(REPO_ROOT / "config"), "pricing.yaml")

    review_rec = Recommendation(
        field_path="fallback (tier=mid)", current_value={"a": 1},
        recommended_value={"a": 1}, rationale="review", evidence="e")
    advisory_rec = Recommendation(
        field_path="infra.onprem_capacity", current_value=None,
        recommended_value="onboard more", rationale="advisory", evidence="e")

    assert compute_proposed_policy_version(
        [review_rec, advisory_rec], routing_raw, pricing_raw) is None
    assert compute_proposed_policy_version([], routing_raw, pricing_raw) is None


# --- 6. out-path guard ---------------------------------------------------------

def test_out_path_guard_refuses_to_overwrite_existing_file(tmp_path):
    existing = tmp_path / "report.yaml"
    existing.write_text("already here")
    with pytest.raises(ReportPathError):
        safe_out_path(existing, config_dir=str(REPO_ROOT / "config"))


def test_out_path_guard_rejects_traversal():
    with pytest.raises(ReportPathError):
        safe_out_path("../x", config_dir=str(REPO_ROOT / "config"))


def test_out_path_guard_rejects_writing_under_config(tmp_path):
    with pytest.raises(ReportPathError):
        safe_out_path(str(REPO_ROOT / "config" / "sneaky.yaml"),
                      config_dir=str(REPO_ROOT / "config"))


def test_write_report_round_trips_and_respects_the_guard(tmp_path):
    report = RecommendationReport(
        generated_window=Window(n_requests=5), current_routing_policy_version="abc123",
        recommendations=[])
    out_path = tmp_path / "report.yaml"
    written = write_report(out_path, report, config_dir=str(REPO_ROOT / "config"))
    assert written == out_path.resolve()
    loaded = yaml.safe_load(out_path.read_text())
    assert loaded["applied"] is False
    assert loaded["current_routing_policy_version"] == "abc123"

    with pytest.raises(ReportPathError):
        write_report(out_path, report, config_dir=str(REPO_ROOT / "config"))


# --- 7. min-requests floor ------------------------------------------------------

def test_min_requests_floor_declines_without_writing_noise(tmp_path, capsys):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'usage.db'}"
    _seed_rows(db_url, [_row() for _ in range(2)])

    out_path = tmp_path / "report.yaml"
    rc = mine_main(["--db", db_url, "--out", str(out_path), "--min-requests", "5"])
    assert rc == 0
    assert not out_path.exists()
    captured = capsys.readouterr()
    assert "not enough data" in captured.err
