"""P3c-1 Task 1: relaxations + enumeration (spec §3)."""
from __future__ import annotations

import copy

from app.eval.shadow import Relaxation, enumerate_relaxations
from tests.eval.sim_fixtures import app_entry, make_row, make_snapshot, onboarded_row


def _ids(rels):
    return [(r.requirement, r.app, r.endpoint) for r in rels]


def test_enumerates_only_constraints_the_app_sets_in_order():
    snap = make_snapshot(apps=[app_entry("a", residency="onprem_only", allowed_tiers=["cheap"]),
                               app_entry("b")])
    rows = [make_row("r1", "a"), make_row("r2", "b")]
    rels, skipped = enumerate_relaxations(snap, rows)
    ids = _ids(rels)
    assert ("residency", "a", None) in ids and ("allowed_tiers", "a", None) in ids
    assert ("max_cost_usd_per_request", "a", None) in ids
    assert ("max_cost_usd_per_request", "b", None) in ids
    assert ("allowed_tiers", "b", None) not in ids
    # Exact expected order, derived from the enumeration rules (spec §3) for
    # this fixture: app "a" (residency=onprem_only, allowed_tiers=[cheap])
    # triggers residency, allowed_tiers and (always) max_cost_usd_per_request,
    # in REQUIREMENT_ORDER; app "b" (all defaults) triggers only
    # max_cost_usd_per_request. Neither app's single row carries a
    # requested_model or a risk_class above "cheap", and no declaration
    # anywhere is accepts_raw_sensitive=false, so those do not appear. No
    # onboarded row has a non-null allowed_app_ids, so there is no
    # endpoint_app_allowlist entry.
    assert ids == [
        ("residency", "a", None),
        ("allowed_tiers", "a", None),
        ("max_cost_usd_per_request", "a", None),
        ("max_cost_usd_per_request", "b", None),
    ]
    assert skipped == 0


def test_apps_without_rows_are_skipped():
    snap = make_snapshot(apps=[app_entry("a"), app_entry("ghost", allowed_tiers=["cheap"])])
    rels, _ = enumerate_relaxations(snap, [make_row("r1", "a")])
    assert all(r.app != "ghost" for r in rels)


def test_cap_truncates_and_counts():
    snap = make_snapshot(apps=[app_entry("a", residency="onprem_only", allowed_tiers=["cheap"],
                                         allowed_models=["local-mid"])])
    rels, skipped = enumerate_relaxations(snap, [make_row("r1", "a")], cap=2)
    assert len(rels) == 2 and skipped >= 1


def test_apply_never_mutates_and_scopes_rows():
    snap = make_snapshot(apps=[app_entry("a", residency="onprem_only"), app_entry("b")])
    rows = [make_row("r1", "a"), make_row("r2", "b")]
    s0, r0 = copy.deepcopy(snap), copy.deepcopy(rows)
    rel = Relaxation("residency", "a", None, "any (escalation off)")
    doc, scoped = rel.apply(snap, rows)
    assert (snap, rows) == (s0, r0)
    pol = next(a for a in doc["apps"] if a["application_id"] == "a")["policy"]
    assert pol["residency"] == "any" and pol["escalate_residency_on_detection"] is False
    assert [r["request_id"] for r in scoped] == ["r1"]


def test_each_relaxation_edits_the_right_place():
    snap = make_snapshot(onboarded=[onboarded_row("local-cheap", "cheap",
                                                  accepts_raw_sensitive=False,
                                                  allowed_app_ids=["a"])],
                         apps=[app_entry("a", max_cost_usd_per_request=0.001)])
    rows = [make_row("r1", "a", requested_model="local-cheap")]
    doc, _ = Relaxation("raw_sensitive", "a", None, "true").apply(snap, rows)
    assert doc["onboarded"][0]["accepts_raw_sensitive"] is True
    doc, _ = Relaxation("endpoint_app_allowlist", None, "local-cheap", "null").apply(snap, rows)
    assert doc["onboarded"][0]["allowed_app_ids"] is None
    doc, scoped = Relaxation("requested_model", "a", None, "unpinned").apply(snap, rows)
    assert scoped[0]["requested_model"] is None
    doc, _ = Relaxation("max_cost_usd_per_request", "a", None, "1e12").apply(snap, rows)
    assert doc["apps"][0]["policy"]["max_cost_usd_per_request"] == 1e12
    doc, _ = Relaxation("risk_floor", "a", None, "cheap").apply(snap, rows)
    assert set(doc["settings"]["routing"]["risk_floors"].values()) == {"cheap"}


def test_endpoint_relaxation_scopes_all_rows():
    snap = make_snapshot(onboarded=[onboarded_row("local-cheap", "cheap", allowed_app_ids=["a"])],
                         apps=[app_entry("a"), app_entry("b")])
    rows = [make_row("r1", "a"), make_row("r2", "b")]
    _, scoped = Relaxation("endpoint_app_allowlist", None, "local-cheap", "null").apply(snap, rows)
    assert len(scoped) == 2
