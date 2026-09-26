# P3c-1 Shadow Prices Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** For recorded traffic, price every constraint an app sets. Each constraint is relaxed on
its own, the scoped rows are replayed through the real eligibility and routing code, and the
report shows cost, rejections avoided, quality, latency and on-prem share. Security constraints
are reported separately as the cost of a protection.

**Architecture:** A new offline module, `app/eval/shadow.py`, built on the P3a scenario
machinery. It has three parts:

- **Enumerate** the relaxations from the baseline snapshot doc and rows.
- **Evaluate** each one: build the relaxed scenario, re-simulate only the in-scope rows with
  `simulate_row`, check monotonicity, and summarise with `_bucket_stats`.
- **Render** the results into `simulation.json` and `report.md` through
  `scripts/policy_simulate.py --shadow-prices`.

**Tech Stack:** Python 3.12, Hypothesis, pytest. No serving-code changes.

**Spec:** `docs/superpowers/specs/2026-09-26-shadow-prices-design.md`

## Global Constraints

- **Offline only.** Serving never imports `app.eval`, and `tests/eval/test_isolation.py` must
  keep passing. No change to any serving module.
- **Requirement ids and the security set** come from `app/policy/core.py`: `REQUIREMENT_ORDER`
  and `SECURITY_IDS`. Import them; never restate them.
- **Relaxations are fixed by the table in spec §3:**
  - `residency` → the app's `residency: "any"` and `escalate_residency_on_detection: false`.
  - `allowed_providers`, `allowed_regions`, `allowed_tiers`, `allowed_models` → `null`.
  - `require_no_retention` → `false`.
  - `raw_sensitive` → every `accepts_raw_sensitive: false` declaration becomes `true`. This
    covers onboarded rows, `settings.providers.*` and `settings.routing.model_pool[*]`.
  - `endpoint_app_allowlist` → that onboarded row's `allowed_app_ids: null`.
  - `requested_model` → the scoped app's rows get `requested_model: None`.
  - `max_cost_usd_per_request` → `1e12`.
  - `risk_floor` → every `settings.routing.risk_floors` value becomes `"cheap"`.
- **Scoping.** Every relaxation except `endpoint_app_allowlist` re-simulates only the scoped
  app's rows. `endpoint_app_allowlist` re-simulates all rows. Rows outside scope are unchanged
  by construction and are not simulated.
- **Monotonicity.** For every re-simulated row:
  - the relaxed eligible set ⊇ the baseline eligible set;
  - a baseline-routed row is never rejected under the relaxation.

  A breach raises `ShadowInvariantError`. The CLI exits 2 on it.
- **Candidate order:** app id, then `REQUIREMENT_ORDER`, then endpoint slug. `--max-relaxations`
  defaults to `50`.
- **Ranking key:**
  `(-(cost_saved_per_day if not None else -cost_both_routed_delta), -rejections_avoided, requirement, scope_key)`.
- **Cost attribution (plan ruling, refines spec §4).**
  - `cost_saved_per_day` = `-cost_usd_both_routed.delta / window_days`. This is like-for-like:
    only rows routed in both arms count, so newly served rows are not counted as added cost.
  - `cost_usd` (all routed) is also reported.
- **Caveat text:** the spec §5 blockquote, verbatim. Every protection-table row carries the
  label `price of a protection — not a recommendation`.
- **Determinism:** `json.dumps(sort_keys=True)` at dump time, floats rounded to 6 places (reuse
  P3a's `_round6`).
- **Tests:**
  - Command: `LITELLM_LOCAL_MODEL_COST_MAP=True /home/manimal/projects/routeForge/.venv/bin/python -m pytest <paths> -p no:warnings`.
    Don't add `-q`. There is no pytest-asyncio; use `asyncio.run`.
  - Shared fixtures: `tests/eval/sim_fixtures.py` (`make_snapshot`, `app_entry`,
    `onboarded_row`, `make_row`, `BASE_SETTINGS`).
- **Commits** end with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

## File map

| File | Status | Responsibility |
|---|---|---|
| `app/eval/shadow.py` | create | `Relaxation`, `enumerate_relaxations`, `shadow_prices`, `render_shadow_section`, `ShadowInvariantError`, `SHADOW_CAVEAT` |
| `scripts/policy_simulate.py` | modify | `--shadow-prices`, `--max-relaxations`; `--proposal` optional with `--shadow-prices` |
| `tests/eval/test_shadow.py` | create | Unit, Hypothesis and scoping tests |
| `tests/eval/test_policy_simulate_cli.py` | modify | CLI and live round-trip tests |
| `docs/reference/policy-simulator.md`, `docs/research/feasible-route-bench.md` | modify | Task 3 |

---

### Task 1: Relaxations and enumeration

**Files:** Create `app/eval/shadow.py` (enumeration part). Test: `tests/eval/test_shadow.py`.

**Interfaces:**
- Consumes:
  - the snapshot doc shape from P3a (see `app/apps/snapshot.py`):
    - `apps[] {application_id, policy{...}}`;
    - `onboarded[] {slug, accepts_raw_sensitive, allowed_app_ids, ...}`;
    - `settings.providers.{name}.accepts_raw_sensitive`;
    - `settings.routing.{model_pool[], risk_floors}`;
  - outcome rows as used by `app/eval/simulate.py`: `application_id`, `requested_model`,
    `risk_class`, `detected_entity_types`;
  - `REQUIREMENT_ORDER` and `SECURITY_IDS` from `app/policy/core.py`.
- Produces:

```python
@dataclass(frozen=True)
class Relaxation:
    requirement: str            # a REQUIREMENT_ORDER id
    app: str | None             # scoped app; None for endpoint_app_allowlist
    endpoint: str | None        # slug for endpoint_app_allowlist, else None
    relaxed_to: str             # human text for the report, e.g. "null", "any (escalation off)", "1e12", "cheap"

    @property
    def security(self) -> bool: ...          # requirement in SECURITY_IDS
    def scope_key(self) -> str: ...          # "app:<id>" or "endpoint:<slug>"
    def apply(self, doc: dict, rows: list[dict]) -> tuple[dict, list[dict]]:
        """Return (relaxed deep-copied doc, the in-scope rows, transformed where the
        relaxation is a row transform). Never mutates inputs."""

def enumerate_relaxations(doc: dict, rows: list[dict], *, cap: int = 50
                          ) -> tuple[list[Relaxation], int]:   # (evaluated list, skipped_by_cap)
```

**Enumeration rules (spec §3).** Consider only apps that have at least one row. Per app, in
`REQUIREMENT_ORDER`:

- `residency`: when the policy residency is not `"any"` (and not null), OR when
  `escalate_residency_on_detection` is not `false` and any of the app's rows has a non-empty
  `detected_entity_types`.
- `allowed_providers`, `allowed_regions`, `allowed_tiers`, `allowed_models`: when non-null.
- `require_no_retention`: when `true`.
- `raw_sensitive`: when any declaration anywhere (onboarded, providers, model_pool) is `false`.
- `requested_model`: when any of the app's rows has a truthy `requested_model`.
- `max_cost_usd_per_request`: always.
- `risk_floor`: when any row of the app has a `risk_class` whose floor in
  `settings.routing.risk_floors` is not `"cheap"`.

Then append `endpoint_app_allowlist` for each onboarded row with a non-null `allowed_app_ids`,
sorted by slug, with `app=None`.

Truncate to `cap` and return the number cut. Row transforms such as `requested_model` apply to
copies of the rows.

- [ ] **Step 1: Write the failing tests** in `tests/eval/test_shadow.py`:

```python
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
    assert ids == sorted(ids, key=lambda t: (t[1] or "~", ids.index(t)))  # app-grouped, stable
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
```

The ordering assertion in `test_enumerates_only_constraints_the_app_sets_in_order` is loose. The
implementer should replace it with an exact expected list, derived from the enumeration rules
for that fixture, before implementing.

- [ ] **Step 2: Run the tests to verify they fail** (ImportError).
- [ ] **Step 3: Implement** the enumeration part of `app/eval/shadow.py`. Its module docstring
  covers: offline only, one-at-a-time relaxation, the scoping rule, and that nothing is ever
  applied.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(eval): shadow-price relaxations and enumeration (P3c-1)`.

---

### Task 2: Evaluation, metrics, monotonicity

**Files:** Modify `app/eval/shadow.py`. Test: `tests/eval/test_shadow.py`.

**Interfaces:**
- Consumes:
  - Task 1's `Relaxation` and `enumerate_relaxations`;
  - `app.eval.scenario.build_scenario` and `close_scenario`;
  - from `app.eval.simulate`: `simulate_row`, `observed_latencies`, `select_rows`,
    `RowResult`, `_bucket_stats`, `_moves`, `_window`;
  - from `app.eval.outcomes`: `_round6`;
  - `apply_proposal`, when a proposal is given.
- Produces:

```python
class ShadowInvariantError(RuntimeError): ...

async def shadow_prices(records: list[dict], baseline_doc: dict, *, base: Settings,
                        cap: int = 50, baseline_label: str = "snapshot") -> dict
    # returns the spec §5 `shadow_prices` block (already _round6-rounded, lists sorted)
```

**Algorithm:**

1. `observed = observed_latencies(records)`.
2. Build the baseline scenario once. Select rows with `select_rows(records, set(baseline.apps))`.
   Compute `base_arm[request_id] = simulate_row(baseline, row, observed)` for every selected
   row.
3. `rels, skipped = enumerate_relaxations(baseline_doc, rows, cap=cap)`.
4. For each relaxation:
   1. `doc_r, scoped = rel.apply(baseline_doc, rows)`.
   2. Build the relaxed scenario, and close it in a `finally`.
   3. For each scoped row: `ra = simulate_row(relaxed, scoped_row, observed)`.
      - **Monotonicity check:** `set(ra.eligible) >= set(base_arm[id].eligible)`, and not
        (`base_arm[id].outcome == "routed"` and `ra.outcome == "rejected"`). Otherwise raise
        `ShadowInvariantError(f"{rel.requirement}@{rel.scope_key()}: row {id} ...")`.
      - Special case: for `requested_model` the baseline row was pinned and the relaxed row is
        unpinned, so the eligible superset holds by construction. Still check it.
   4. Build `results = [RowResult(row, base_arm[id], ra) ...]` and `stats = _bucket_stats(results)`.
   5. The relaxation is **binding** when `stats["route_changed"] + stats["newly_served"] > 0`;
      otherwise count it as `non_binding`.
   6. For a binding relaxation, emit an entry with:
      - `requirement`, `scope` (`{"app": id}` or `{"endpoint": slug}`), `security`, `relaxed_to`;
      - `rows_in_scope`, `route_changed`, `rejections_avoided` (= `newly_served`);
      - `cost_usd` (= `stats["cost_usd"]` renamed `baseline`/`relaxed`/`delta`);
      - `cost_usd_both_routed` (same renaming);
      - `cost_saved_per_day`: `-both.delta / days`, where `days = (end - start) / 86400` from
        `_window(results)`, or `None` when `days < 1`;
      - `quality_mean_delta`, `latency_p50_delta`;
      - `onprem_share {baseline, relaxed}`;
      - `affected_apps`: the sorted apps of rows with a route change or avoided rejection;
      - `moves`: `_moves(results)[:5]`.
5. Sort by the ranking key in Global Constraints. Return
   `{"baseline": baseline_label, "evaluated": len(rels), "skipped_by_cap": skipped,
   "non_binding": n, "constraints": [...]}`, run through `_round6`.
6. Close the baseline scenario in a `finally`.

- [ ] **Step 1: Write the failing tests** (append to `tests/eval/test_shadow.py`):

```python
import asyncio

import pytest
from hypothesis import HealthCheck, given, settings as hsettings, strategies as st

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
    import app.eval.shadow as shadow
    real = shadow.simulate_row
    calls = {"n": 0}
    def fake(scn, row, observed):
        arm = real(scn, row, observed)
        calls["n"] += 1
        if calls["n"] > 1:   # every relaxed-arm call reports a rejection
            return arm.__class__(**{**arm.__dict__, "outcome": "rejected", "model": None,
                                    "provider": None, "tier": None, "eligible": ()})
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


_POLICY = st.fixed_dictionaries({}, optional={
    "residency": st.sampled_from(["any", "onprem_only"]),
    "allowed_tiers": st.lists(st.sampled_from(["cheap", "mid", "frontier"]), min_size=1, unique=True),
    "allowed_regions": st.just(["eu"]),
    "require_no_retention": st.just(True),
    "max_cost_usd_per_request": st.sampled_from([1e-9, 0.01, 1.0]),
})


@hsettings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(policy=_POLICY, etypes=st.lists(st.sampled_from(["US_SSN", "PERSON"]), unique=True, max_size=2),
       complexity=st.floats(0.0, 3.0))
def test_monotonic_for_random_policies(policy, etypes, complexity):   # spec §7.4
    snap = make_snapshot(providers={"azure": {"enabled": True}},
                         apps=[app_entry("a", **policy)])
    rows = [make_row("h", "a", detected_entity_types=etypes, complexity=complexity)]
    block = _run(rows, snap)   # raises ShadowInvariantError on any breach
    for c in block["constraints"]:
        assert c["route_changed"] + c["rejections_avoided"] > 0
```

The `test_invariant_breach_raises` fake depends on the call order: the baseline arm first, then
one call per relaxation. If `ArmResult` is a frozen dataclass without `__dict__` overrides, use
`dataclasses.replace(arm, outcome="rejected", ...)` instead. The assertion (the breach raises)
must hold either way.

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** the algorithm.
- [ ] **Step 4: Run** `tests/eval/`. Expected: PASS, including `test_isolation.py`.
- [ ] **Step 5: Commit** `feat(eval): shadow-price evaluation with monotonicity check (P3c-1)`.

---

### Task 3: Render, CLI, live round trip, docs

**Files:**
- Modify: `app/eval/shadow.py` (render), `scripts/policy_simulate.py`,
  `tests/eval/test_policy_simulate_cli.py`, `docs/reference/policy-simulator.md`,
  `docs/research/feasible-route-bench.md`.
- Test: `tests/eval/test_shadow.py`, `tests/eval/test_policy_simulate_cli.py`.

**Interfaces:**
- Consumes: `shadow_prices`, `ShadowInvariantError` (Task 2).
- Produces: `SHADOW_CAVEAT: str` (the spec §5 blockquote text, verbatim, without the leading
  `> `) and `render_shadow_section(block: dict) -> str`.

**Rules:**

**Render.** The section is `## Shadow prices`, then the caveat as a blockquote, then:
- the table `### Operational constraints` (non-security) with columns requirement, scope,
  rows, route changed, rejections avoided, cost saved/day (or Δcost both-routed), Δquality,
  Δp50 ms, on-prem share baseline→relaxed;
- the table `### Cost of protection` (security), with the same columns plus a final label
  column holding exactly `price of a protection — not a recommendation`;
- the footer `Non-binding: N · skipped by cap: M`.

An empty table reads "None."

**CLI.**
- `--shadow-prices` (flag) and `--max-relaxations N` (int, default 50, must be ≥ 1, else exit 2).
- `--proposal` is required unless `--shadow-prices` is given.
- With `--shadow-prices`:
  - The baseline doc is `apply_proposal(snapshot, proposal, base=base)` when a proposal is
    given (label `"snapshot+proposal"`); otherwise it is the snapshot (label `"snapshot"`).
  - The CLI calls `asyncio.run(shadow_prices(...))`.
- Without a proposal, `simulation.json` = `{"schema": "routeforge.simulation/v1", "inputs": {...}, "shadow_prices": block}`.
  - The `inputs` block is the same as P3a's, with `proposal_sha256: null` and
    `proposal_title: null`.
  - `report.md` = the P3a caveat line, then the shadow section.
- With a proposal, the P3a output is unchanged, plus a top-level `shadow_prices` key and the
  shadow section appended to the report.
- `ShadowInvariantError` → `error: internal invariant: ...` on stderr, exit 2.
- Every existing P3a exit path is unchanged.

**CLI tests** (`tests/eval/test_policy_simulate_cli.py`):
1. `--shadow-prices` without `--proposal` → exit 0; `simulation.json` has `shadow_prices`;
   `report.md` contains `## Shadow prices` and the caveat.
2. `--max-relaxations 0` → exit 2.
3. Neither `--proposal` nor `--shadow-prices` → exit 2 (argparse error, or an explicit check).
4. **Live round trip:**
   - Create a real app through the admin API with `allowed_tiers` restricted to a tier that
     has no model. Use the seeded models' tiers to pick one, and state the choice in a comment.
   - Send 2 requests (expecting 422s), then take the snapshot and export.
   - Run the CLI with `--shadow-prices`.
   - The entry `allowed_tiers` for that app has `rejections_avoided == 2`.

   Follow `test_live_round_trip_baseline_is_perfect` for the client pattern.
5. Determinism: two CLI runs give byte-identical `simulation.json`.

**Docs:**
- `docs/reference/policy-simulator.md`: a new section "Shadow prices" covering:
  - what it answers;
  - the relaxation table (from Global Constraints);
  - the scoping rule;
  - the metrics, including why cost/day is like-for-like;
  - the two tables and the non-recommendation rule;
  - the flags;
  - the `ShadowInvariantError` exit;
  - the limits: one at a time, no joint effects, no uncertainty, no capacity.
- `docs/research/feasible-route-bench.md`: append `## 17. Shadow prices (P3c-1)`, with
  subsections:
  - 17.1 the model;
  - 17.2 scoped attribution and why;
  - 17.3 the monotonicity invariant and test names;
  - 17.4 Results: "Pending the Spark run after deploy" and an empty table with columns
    `constraint | scope | rejections avoided | cost saved/day | security`.
- The docs vocabulary gate (`tests/test_docs_links.py`) forbids the purged demo-model names;
  run it.

- [ ] **Step 1: Write the failing tests** (the render test in `test_shadow.py` and the CLI tests
  above).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** render and the CLI; write the docs.
- [ ] **Step 4: Run** `tests/eval/ tests/test_docs_links.py`, then the FULL `tests/` once.
  Quote the summary lines.
- [ ] **Step 5: Commit** `feat(eval): shadow prices in policy_simulate (report, CLI, docs) (P3c-1)`.
