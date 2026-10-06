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


# --- Task 2: evaluation, metrics, monotonicity -------------------------------

import asyncio

import pytest
from hypothesis import HealthCheck, example, given, settings as hsettings, strategies as st

from app.eval.shadow import ShadowInvariantError, shadow_prices
from tests.eval.sim_fixtures import BASE_SETTINGS


def _run(records, doc, **kw):
    return asyncio.run(shadow_prices(records, doc, base=BASE_SETTINGS, **kw))


def _entry(block, requirement, app=None, endpoint=None):
    for c in block["constraints"]:
        if c["requirement"] == requirement and c["scope"] == (
                {"app": app} if app else {"endpoint": endpoint}):
            return c
    return None


def test_binding_allowed_models_avoids_rejections():
    snap = make_snapshot(apps=[app_entry("a", allowed_models=["ghost-model"])])
    rows = [make_row(f"r{i}", "a", ts=f"2026-09-20T12:00:0{i}+00:00") for i in range(3)]
    block = _run(rows, snap)
    e = _entry(block, "allowed_models", app="a")
    assert e and e["rejections_avoided"] == 3 and e["security"] is False


def test_non_binding_is_counted_not_listed():
    snap = make_snapshot(apps=[app_entry("a", allowed_tiers=["cheap", "mid", "frontier"])])
    block = _run([make_row("r1", "a")], snap)
    assert _entry(block, "allowed_tiers", app="a") is None
    assert block["non_binding"] >= 1


def test_residency_relaxation_is_security_and_scoped():
    snap = make_snapshot(providers={"azure": {"enabled": True}}, onboarded=[],
                         apps=[app_entry("a", residency="onprem_only"), app_entry("b")])
    rows = [make_row("ra", "a"), make_row("rb", "b")]
    block = _run(rows, snap)
    e = _entry(block, "residency", app="a")
    assert e and e["security"] is True and e["rejections_avoided"] == 1
    assert e["rows_in_scope"] == 1 and e["affected_apps"] == ["a"]


def test_invariant_breach_raises(monkeypatch):
    import dataclasses

    import app.eval.shadow as shadow
    real = shadow.simulate_row
    calls = {"n": 0}

    def fake(scn, row, observed):
        arm = real(scn, row, observed)
        calls["n"] += 1
        if calls["n"] > 1:   # every relaxed-arm call reports a rejection
            return dataclasses.replace(arm, outcome="rejected", model=None, provider=None,
                                       tier=None, eligible=())
        return arm
    monkeypatch.setattr(shadow, "simulate_row", fake)
    snap = make_snapshot(apps=[app_entry("a")])
    with pytest.raises(ShadowInvariantError):
        _run([make_row("r1", "a")], snap)


def test_deterministic():
    snap = make_snapshot(apps=[app_entry("a", allowed_models=["ghost-model"], residency="onprem_only")])
    rows = [make_row("r1", "a"), make_row("r2", "a")]
    import json
    assert json.dumps(_run(rows, snap), sort_keys=True) == json.dumps(_run(rows, snap), sort_keys=True)


def test_block_shape_and_cost_per_day_like_for_like():
    # Two-day window; allowed_tiers pins app "a" to frontier, relaxing it
    # moves every row to the cheap tier -- a positive like-for-like saving.
    snap = make_snapshot(apps=[app_entry("a", allowed_tiers=["frontier"])])
    rows = [make_row("r1", "a", ts="2026-09-18T12:00:00+00:00"),
            make_row("r2", "a", ts="2026-09-20T12:00:00+00:00")]
    block = _run(rows, snap)
    assert block["baseline"] == "snapshot" and block["skipped_by_cap"] == 0
    assert block["evaluated"] == len(enumerate_relaxations(snap, rows)[0])
    e = _entry(block, "allowed_tiers", app="a")
    assert e["route_changed"] == 2 and e["rejections_avoided"] == 0
    both = e["cost_usd_both_routed"]
    assert both["delta"] < 0
    assert e["cost_saved_per_day"] == pytest.approx(-both["delta"] / 2, abs=1e-6)
    assert set(e["cost_usd"]) == {"baseline", "relaxed", "delta"}
    assert e["onprem_share"] == {"baseline": 1.0, "relaxed": 1.0}
    assert e["moves"] == [{"from": "local-big", "to": "local-cheap", "count": 2}]
    assert e["relaxed_to"] == "null"


def test_cost_saved_per_day_null_under_one_day_and_ranking():
    snap = make_snapshot(apps=[app_entry("a", allowed_tiers=["frontier"],
                                         allowed_models=["ghost-model", "local-big",
                                                         "local-cheap", "local-mid"])])
    block = _run([make_row("r1", "a")], snap)
    e = _entry(block, "allowed_tiers", app="a")
    assert e["cost_saved_per_day"] is None
    # Ranked by -(saving) first: allowed_tiers (saves) precedes any
    # non-saving binding entry.
    keys = [(c["requirement"], c["scope"]) for c in block["constraints"]]
    assert keys[0] == ("allowed_tiers", {"app": "a"})


def test_cap_is_reported():
    snap = make_snapshot(apps=[app_entry("a", residency="onprem_only", allowed_tiers=["cheap"],
                                         allowed_models=["local-mid"])])
    block = _run([make_row("r1", "a")], snap, cap=2, baseline_label="snapshot+proposal")
    assert block["evaluated"] == 2 and block["skipped_by_cap"] >= 1
    assert block["baseline"] == "snapshot+proposal"


def test_endpoint_allowlist_resimulates_all_rows():
    snap = make_snapshot(onboarded=[onboarded_row("local-cheap", "cheap", allowed_app_ids=["a"]),
                                    onboarded_row("local-big", "frontier")],
                         apps=[app_entry("a"), app_entry("b")])
    rows = [make_row("ra", "a"), make_row("rb", "b")]
    block = _run(rows, snap)
    e = _entry(block, "endpoint_app_allowlist", endpoint="local-cheap")
    assert e and e["rows_in_scope"] == 2 and e["affected_apps"] == ["b"]
    from app.policy.core import SECURITY_IDS
    assert e["security"] is ("endpoint_app_allowlist" in SECURITY_IDS)


# Raw sensitive: an app whose compiled model_input is `raw` and whose row
# carries a detected entity classified above `public` "sends raw sensitive
# content" (app/policy/context.py); an endpoint declaring
# accepts_raw_sensitive=false is then excluded (app/policy/feasible.py).
# The declaration here lives on the DEPLOYMENT side -- settings.providers.*
# or settings.routing.model_pool[*] -- not on an onboarded row. Escalation
# is off so the detected entity does not also tighten residency to
# onprem_only (which would keep the row rejected for a second reason and
# make raw_sensitive non-binding on its own).

def _raw_app(app_id):
    return app_entry(app_id, model_input="raw", escalate_residency_on_detection=False)


def test_raw_sensitive_from_provider_declaration_binds():
    snap = make_snapshot(onboarded=[],
                         providers={"azure": {"enabled": True, "accepts_raw_sensitive": "false"}},
                         apps=[_raw_app("a"), app_entry("b")])
    rows = [make_row("ra", "a", detected_entity_types=["US_SSN"]),
            make_row("rb", "b", detected_entity_types=["US_SSN"])]
    rels, _ = enumerate_relaxations(snap, rows)
    assert ("raw_sensitive", "a", None) in _ids(rels)
    block = _run(rows, snap)
    e = _entry(block, "raw_sensitive", app="a")
    assert e and e["rejections_avoided"] == 1 and e["security"] is True
    assert e["rows_in_scope"] == 1 and e["affected_apps"] == ["a"]
    assert e["moves"] == [{"from": "(rejected)", "to": "azure-gpt-mid", "count": 1}]


def test_raw_sensitive_from_model_pool_declaration_binds():
    snap = make_snapshot(onboarded=[], providers={"azure": {"enabled": True}},
                         apps=[_raw_app("a")])
    for entry in snap["settings"]["routing"]["model_pool"]:
        entry["accepts_raw_sensitive"] = "false"
    rows = [make_row("ra", "a", detected_entity_types=["US_SSN"])]
    rels, _ = enumerate_relaxations(snap, rows)
    assert ("raw_sensitive", "a", None) in _ids(rels)
    e = _entry(_run(rows, snap), "raw_sensitive", app="a")
    assert e and e["rejections_avoided"] == 1


_POLICY = st.fixed_dictionaries({}, optional={
    "residency": st.sampled_from(["any", "onprem_only"]),
    "allowed_tiers": st.lists(st.sampled_from(["cheap", "mid", "frontier"]), min_size=1, unique=True),
    "allowed_regions": st.just(["eu"]),
    "require_no_retention": st.just(True),
    # 0.001 / 0.002 at 1000 prompt tokens: the cheap tier fits, the risk
    # floor's tier does not (the final-fix C1 counterexamples).
    "max_cost_usd_per_request": st.sampled_from([1e-9, 0.001, 0.002, 0.01, 1.0]),
})


def _relaxed_arms_newly_rejected(rows, snap, rel):
    """Recompute, row by row, how many scoped rows `rel` turns from routed
    into rejected -- independently of `shadow_prices`' own bookkeeping."""
    from app.eval.scenario import build_scenario, close_scenario
    from app.eval.simulate import simulate_row

    async def go():
        base = await build_scenario(snap, base=BASE_SETTINGS)
        doc_r, scoped = rel.apply(snap, rows)
        relaxed = await build_scenario(doc_r, base=BASE_SETTINGS)
        try:
            by_id = {r["request_id"]: r for r in rows}
            n, codes = 0, {}
            for row in scoped:
                b = simulate_row(base, by_id[row["request_id"]], {})
                r = simulate_row(relaxed, row, {})
                assert set(b.eligible) <= set(r.eligible)
                if b.outcome == "routed" and r.outcome == "rejected":
                    n += 1
                    for code in r.reject_codes:
                        codes[code] = codes.get(code, 0) + 1
            return n, codes
        finally:
            await close_scenario(relaxed)
            await close_scenario(base)
    return asyncio.run(go())


@hsettings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(policy=_POLICY, etypes=st.lists(st.sampled_from(["US_SSN", "PERSON"]), unique=True, max_size=2),
       complexity=st.floats(0.0, 3.0), prompt_tokens=st.sampled_from([200, 1000]),
       bedrock=st.booleans())
@example(policy={"allowed_tiers": ["cheap"], "max_cost_usd_per_request": 0.001},
         etypes=["US_SSN"], complexity=0.1, prompt_tokens=1000, bedrock=False)   # Case A
@example(policy={"residency": "onprem_only", "max_cost_usd_per_request": 0.002},
         etypes=["US_SSN"], complexity=0.1, prompt_tokens=1000, bedrock=True)    # Case B
def test_eligible_superset_for_random_policies(policy, etypes, complexity, prompt_tokens,
                                               bedrock):   # spec §7.4 (as amended)
    providers = {"azure": {"enabled": True}}
    if bedrock:
        providers["bedrock"] = {"enabled": True}
    snap = make_snapshot(providers=providers, apps=[app_entry("a", **policy)])
    rows = [make_row("h", "a", detected_entity_types=etypes, complexity=complexity,
                     prompt_tokens=prompt_tokens)]
    block = _run(rows, snap)   # raises ShadowInvariantError on a superset breach only
    rels = {(r.requirement, r.scope_key()): r for r in enumerate_relaxations(snap, rows)[0]}
    for c in block["constraints"]:
        assert c["route_changed"] + c["rejections_avoided"] + c["newly_rejected"] > 0
        assert c["newly_rejected"] >= 0
        assert sum(c["newly_rejected_reasons"].values()) >= c["newly_rejected"]
        scope = c["scope"]
        key = f"endpoint:{scope['endpoint']}" if "endpoint" in scope else f"app:{scope['app']}"
        n, codes = _relaxed_arms_newly_rejected(rows, snap, rels[(c["requirement"], key)])
        assert c["newly_rejected"] == n
        assert c["newly_rejected_reasons"] == dict(sorted(codes.items()))


# --- Task 2 fix round 1 -------------------------------------------------------

def _fake_relaxed_arm(monkeypatch, mutate):
    """Route every relaxed-arm `simulate_row` call (all but the first, the
    baseline) through `mutate(arm)`."""
    import app.eval.shadow as shadow
    real = shadow.simulate_row
    calls = {"n": 0}

    def fake(scn, row, observed):
        arm = real(scn, row, observed)
        calls["n"] += 1
        return mutate(arm) if calls["n"] > 1 else arm
    monkeypatch.setattr(shadow, "simulate_row", fake)


def test_routed_to_rejected_is_reported_not_raised(monkeypatch):
    import dataclasses
    # Eligible set kept intact: a routed -> rejected row is a reportable
    # result (final-fix C1), not an invariant breach.
    _fake_relaxed_arm(monkeypatch, lambda arm: dataclasses.replace(
        arm, outcome="rejected", model=None, provider=None, tier=None,
        reject_codes=("over_budget",)))
    snap = make_snapshot(apps=[app_entry("a")])
    block = _run([make_row("r1", "a")], snap)   # must not raise
    e = _entry(block, "max_cost_usd_per_request", app="a")
    assert e and e["newly_rejected"] == 1
    assert e["newly_rejected_reasons"] == {"over_budget": 1}
    assert e["route_changed"] == 0 and e["rejections_avoided"] == 0


def test_invariant_lost_eligible_raises_alone(monkeypatch):
    import dataclasses
    # Still routed: only the eligible-superset branch can fire.
    _fake_relaxed_arm(monkeypatch, lambda arm: dataclasses.replace(
        arm, eligible=arm.eligible[1:]))
    snap = make_snapshot(apps=[app_entry("a")])
    with pytest.raises(ShadowInvariantError, match="lost eligible candidate"):
        _run([make_row("r1", "a")], snap)


def test_cost_saved_per_day_is_like_for_like():
    # allowed_models=[local-big]: r1 routes to local-big and is rerouted to
    # local-cheap by the relaxation; r2 pins local-cheap, so it is rejected
    # at baseline and newly served. cost_usd counts r2's new cost,
    # cost_usd_both_routed does not.
    snap = make_snapshot(apps=[app_entry("a", allowed_models=["local-big"])])
    rows = [make_row("r1", "a", ts="2026-09-18T12:00:00+00:00"),
            make_row("r2", "a", ts="2026-09-20T12:00:00+00:00",
                     requested_model="local-cheap")]
    e = _entry(_run(rows, snap), "allowed_models", app="a")
    assert e["route_changed"] == 1 and e["rejections_avoided"] == 1
    days = 2.0
    both, alll = e["cost_usd_both_routed"]["delta"], e["cost_usd"]["delta"]
    assert both != alll
    assert e["cost_saved_per_day"] == pytest.approx(-both / days, abs=1e-6)
    assert e["cost_saved_per_day"] != pytest.approx(-alll / days, abs=1e-6)


def test_cost_saved_per_day_uses_the_full_export_window():
    # App "a" is active for one hour; app "b" spans three days. The export
    # window is three days, so a's relaxation gets a per-day figure over it.
    snap = make_snapshot(apps=[app_entry("a", allowed_tiers=["frontier"]), app_entry("b")])
    rows = [make_row("b1", "b", ts="2026-09-17T12:00:00+00:00"),
            make_row("a1", "a", ts="2026-09-19T12:00:00+00:00"),
            make_row("a2", "a", ts="2026-09-19T13:00:00+00:00"),
            make_row("b2", "b", ts="2026-09-20T12:00:00+00:00")]
    e = _entry(_run(rows, snap), "allowed_tiers", app="a")
    assert e["rows_in_scope"] == 2
    both = e["cost_usd_both_routed"]["delta"]
    assert both < 0
    assert e["cost_saved_per_day"] == pytest.approx(-both / 3.0, abs=1e-6)


def test_ranking_full_order_with_tie_break():
    # a and b are identical (same saving, same rejections, same requirement)
    # -> tie broken by scope key app:a < app:b. c only avoids rejections
    # (like-for-like saving 0), so it ranks after both.
    snap = make_snapshot(apps=[app_entry("b", allowed_tiers=["frontier"]),
                               app_entry("a", allowed_tiers=["frontier"]),
                               app_entry("c", allowed_models=["ghost-model"])])
    rows = [make_row("rb", "b"), make_row("ra", "a"), make_row("rc", "c")]
    block = _run(rows, snap)
    order = [(c["requirement"], c["scope"]) for c in block["constraints"]]
    assert order == [("allowed_tiers", {"app": "a"}),
                     ("allowed_tiers", {"app": "b"}),
                     ("allowed_models", {"app": "c"})]


# --- Task 3: render (spec §5) -------------------------------------------------

from app.eval.shadow import SHADOW_CAVEAT, render_shadow_section  # noqa: E402


def _shadow_entry(requirement, *, app="a", security=False, rejections_avoided=0,
                  route_changed=0, cost_saved_per_day=None, both_delta=0.0):
    return {
        "requirement": requirement, "scope": {"app": app}, "security": security,
        "relaxed_to": "null", "rows_in_scope": 5, "route_changed": route_changed,
        "rejections_avoided": rejections_avoided,
        "newly_rejected": 0, "newly_rejected_reasons": {},
        "cost_usd": {"baseline": 1.0, "relaxed": 0.5, "delta": -0.5},
        "cost_usd_both_routed": {"baseline": 1.0, "relaxed": 0.5, "delta": both_delta},
        "cost_saved_per_day": cost_saved_per_day,
        "quality_mean_delta": 0.01, "latency_p50_delta": -12.5,
        "onprem_share": {"baseline": 0.5, "relaxed": 0.9},
        "affected_apps": [app], "moves": [],
    }


def test_shadow_caveat_matches_spec_text():
    assert SHADOW_CAVEAT == (
        "**What each constraint costs.** Each row relaxes ONE constraint for one app "
        "(or endpoint) and\nreplays the same recorded traffic. Nothing is applied. "
        "Security constraints are shown as the\ncost of a protection — they are "
        "never a recommendation to relax them.")


def test_render_shadow_section_has_caveat_headings_and_footer():
    block = {"baseline": "snapshot", "evaluated": 2, "skipped_by_cap": 1, "non_binding": 3,
             "constraints": [_shadow_entry("allowed_tiers", cost_saved_per_day=1.5),
                             _shadow_entry("residency", security=True,
                                          rejections_avoided=1)]}
    text = render_shadow_section(block)
    assert text.startswith("## Shadow prices\n")
    for line in SHADOW_CAVEAT.split("\n"):
        assert f"> {line}" in text
    assert "### Operational constraints" in text
    assert "### Cost of protection" in text
    assert "| allowed_tiers |" in text
    assert "| residency |" in text
    assert "price of a protection — not a recommendation" in text
    assert "Non-binding: 3" in text and "skipped by cap: 1" in text


def test_render_shadow_section_empty_tables_read_none():
    block = {"baseline": "snapshot", "evaluated": 0, "skipped_by_cap": 0, "non_binding": 0,
             "constraints": []}
    text = render_shadow_section(block)
    assert text.count("None.") == 2


def test_render_shadow_section_operational_and_protection_are_separated():
    block = {"baseline": "snapshot", "evaluated": 1, "skipped_by_cap": 0, "non_binding": 0,
             "constraints": [_shadow_entry("allowed_tiers", security=False)]}
    text = render_shadow_section(block)
    op_idx = text.index("### Operational constraints")
    prot_idx = text.index("### Cost of protection")
    assert op_idx < text.index("allowed_tiers") < prot_idx
    prot_section = text[prot_idx:]
    assert "None." in prot_section
    assert "price of a protection" not in text[:prot_idx]


# --- final fix wave: C1 (newly rejected), M1, M2 ------------------------------


def _high_risk_row():
    return make_row("r1", "a", detected_entity_types=["US_SSN"], risk_class="high",
                    prompt_tokens=1000)


def test_case_a_allowed_tiers_relaxation_newly_rejects_over_budget():
    # allowed_tiers=[cheap] clamps the high-risk target to cheap (rule 8),
    # which fits the 0.001 budget. Relaxing allowed_tiers makes the frontier
    # floor eligible; the target lifts to it, it is over budget, and the
    # budget fallback never drops below the floor -> rejected.
    snap = make_snapshot(apps=[app_entry("a", allowed_tiers=["cheap"],
                                         max_cost_usd_per_request=0.001)])
    block = _run([_high_risk_row()], snap)   # must not raise
    e = _entry(block, "allowed_tiers", app="a")
    assert e is not None                     # binding on newly_rejected alone
    assert e["newly_rejected"] == 1
    assert e["newly_rejected_reasons"] == {"over_budget": 1}
    assert e["route_changed"] == 0 and e["rejections_avoided"] == 0
    assert e["moves"] == [{"from": "local-cheap", "to": "(rejected)", "count": 1}]


def test_case_b_hospital_residency_relaxation_newly_rejects_over_budget():
    # The hospital persona: onprem_only, high-risk SSN, on-prem has cheap +
    # mid only, cloud frontier enabled. Baseline clamps to the best allowed
    # on-prem tier (mid) and routes. Relaxing residency makes the cloud
    # frontier floor eligible; it is over the 0.002 budget -> rejected.
    snap = make_snapshot(providers={"azure": {"enabled": True}, "bedrock": {"enabled": True}},
                         onboarded=[onboarded_row("local-cheap", "cheap"),
                                    onboarded_row("local-mid", "mid")],
                         apps=[app_entry("a", residency="onprem_only",
                                         max_cost_usd_per_request=0.002)])
    block = _run([_high_risk_row()], snap)   # must not raise
    e = _entry(block, "residency", app="a")
    assert e is not None and e["security"] is True
    assert e["newly_rejected"] == 1
    assert e["newly_rejected_reasons"] == {"over_budget": 1}
    assert e["route_changed"] == 0 and e["rejections_avoided"] == 0


def test_newly_rejected_fields_on_every_entry():
    snap = make_snapshot(apps=[app_entry("a", allowed_tiers=["frontier"])])
    e = _entry(_run([make_row("r1", "a")], snap), "allowed_tiers", app="a")
    assert e["newly_rejected"] == 0 and e["newly_rejected_reasons"] == {}


def test_render_has_newly_rejected_column_and_note():
    from app.eval.shadow import NEWLY_REJECTED_NOTE
    entry = _shadow_entry("allowed_tiers", cost_saved_per_day=1.5)
    entry.update(newly_rejected=2, newly_rejected_reasons={"over_budget": 2})
    prot = _shadow_entry("residency", security=True)
    block = {"baseline": "snapshot", "evaluated": 2, "skipped_by_cap": 0, "non_binding": 0,
             "constraints": [entry, prot]}
    text = render_shadow_section(block)
    headers = [line for line in text.splitlines() if line.startswith("| requirement |")]
    assert len(headers) == 2 and all("| newly rejected |" in h for h in headers)
    assert "| rejections avoided | newly rejected |" in headers[0]
    assert "| allowed_tiers | app:a | 5 | 0 | 0 | 2 |" in text
    assert NEWLY_REJECTED_NOTE == (
        "Relaxing a constraint can newly reject a row: a wider allowed set can lift "
        "the target to the risk floor, and the budget fallback never goes below the floor.")
    assert NEWLY_REJECTED_NOTE in text
    assert text.index("### Cost of protection") < text.index(NEWLY_REJECTED_NOTE) \
        < text.index("Non-binding:")


# M1: `feasible._declared` strips and lowercases, so any spelling of "false"
# is enforced as false -- the relaxation must see (and flip) it too.

@pytest.mark.parametrize("spelling", ["False", " FALSE ", "false"])
def test_raw_sensitive_declaration_is_case_and_space_insensitive(spelling):
    snap = make_snapshot(providers={"azure": {"enabled": True, "accepts_raw_sensitive": spelling}},
                         apps=[_raw_app("a")])
    rows = [make_row("ra", "a", detected_entity_types=["US_SSN"])]
    rels, _ = enumerate_relaxations(snap, rows)
    assert ("raw_sensitive", "a", None) in _ids(rels)
    doc, _ = Relaxation("raw_sensitive", "a", None, "true").apply(snap, rows)
    assert doc["settings"]["providers"]["azure"]["accepts_raw_sensitive"] == "true"


def test_raw_sensitive_capitalised_false_binds():
    snap = make_snapshot(onboarded=[],
                         providers={"azure": {"enabled": True, "accepts_raw_sensitive": "False"}},
                         apps=[_raw_app("a")])
    rows = [make_row("ra", "a", detected_entity_types=["US_SSN"])]
    e = _entry(_run(rows, snap), "raw_sensitive", app="a")
    assert e and e["rejections_avoided"] == 1


def test_raw_sensitive_flip_keeps_the_field_shape():
    snap = make_snapshot(onboarded=[onboarded_row("local-cheap", "cheap",
                                                  accepts_raw_sensitive=False)],
                         providers={"azure": {"enabled": True, "accepts_raw_sensitive": "FALSE"}},
                         apps=[app_entry("a")])
    doc, _ = Relaxation("raw_sensitive", "a", None, "true").apply(snap, [make_row("r1", "a")])
    assert doc["onboarded"][0]["accepts_raw_sensitive"] is True
    assert doc["settings"]["providers"]["azure"]["accepts_raw_sensitive"] == "true"


# M2: risk_floor candidacy uses the risk class replay derives from the
# row's entities and the doc's rules, never the recorded `risk_class`.

def test_risk_floor_candidate_without_recorded_risk_class():
    snap = make_snapshot(apps=[app_entry("a")])
    row = make_row("r1", "a", detected_entity_types=["US_SSN"])
    del row["risk_class"]
    rels, _ = enumerate_relaxations(snap, [row])
    assert ("risk_floor", "a", None) in _ids(rels)


def test_risk_floor_candidacy_respects_a_proposal_rule_change():
    from app.eval.proposal import apply_proposal
    snap = make_snapshot(apps=[app_entry("a")])
    # Recorded as high risk under the old rules ...
    row = make_row("r1", "a", detected_entity_types=["US_SSN"], risk_class="high")
    assert ("risk_floor", "a", None) in _ids(enumerate_relaxations(snap, [row])[0])
    # ... but the proposal gives app "a" its own rules, classifying US_SSN low.
    low = {"entities": ["US_SSN"], "sensitivity": "internal", "risk_class": "low",
           "residency": "any", "require_verification": False}
    doc = apply_proposal(snap, {"schema": "routeforge.policy-proposal/v1",
                                "apps": {"a": {"sensitivity_rules": [low]}}},
                         base=BASE_SETTINGS)
    assert ("risk_floor", "a", None) not in _ids(enumerate_relaxations(doc, [row])[0])
    # And the reverse: recorded low, the proposal makes PERSON high risk.
    high = dict(low, entities=["PERSON"], sensitivity="restricted", risk_class="high")
    row2 = make_row("r2", "a", detected_entity_types=["PERSON"], risk_class="low")
    doc2 = apply_proposal(snap, {"schema": "routeforge.policy-proposal/v1",
                                 "apps": {"a": {"sensitivity_rules": [high]}}},
                          base=BASE_SETTINGS)
    assert ("risk_floor", "a", None) in _ids(enumerate_relaxations(doc2, [row2])[0])


def test_zero_saving_never_renders_as_negative_zero():
    # Spark run, 2026-09-26: an unpinned relaxation between two unpriced
    # on-prem models gave -(0.0) / days = -0.0, rendered "-0/day".
    import math
    from app.eval.outcomes import _round6
    assert math.copysign(1.0, _round6(-0.0)) == 1.0
    assert math.copysign(1.0, _round6({"x": [-1e-9]})["x"][0]) == 1.0


# --- P3c-2 Task 1: --since reaches shadow prices too (spec §6) ----------------

def test_shadow_prices_respects_since():
    from app.eval.simulate import parse_since
    snap = make_snapshot(apps=[app_entry("a", allowed_tiers=["frontier"])])
    rows = [make_row("old", "a", ts="2026-09-01T00:00:00+00:00"),
            make_row("new", "a", ts="2026-09-20T12:00:00+00:00")]
    assert _entry(_run(rows, snap), "allowed_tiers", app="a")["rows_in_scope"] == 2
    cut = _run(rows, snap, since=parse_since("2026-09-10T00:00:00Z"))
    assert _entry(cut, "allowed_tiers", app="a")["rows_in_scope"] == 1
