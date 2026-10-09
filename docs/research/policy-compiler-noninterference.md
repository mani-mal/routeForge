# Constraint-isolated policy compilation and noninterference for an LLM policy gateway

**Status:** research notes, written as the code landed. 2026-09-18.
**Branch:** `feat/policy-compiler-p1a` (merge base `a7db4f5`). Design:
`docs/superpowers/specs/2026-09-18-policy-compiler-p1a-design.md`.
**Audience:** whoever writes the paper.

This is the evidence file, not the paper. Its job is to record exactly what was built,
exactly what was demonstrated, and — with more care than the rest of it — **exactly what
was not**. A paper written from this document should be able to survive a reviewer who
reads the test suite.

---

## 1. The problem, stated as a systems problem

A multi-tenant LLM gateway has to answer two questions about every request, and they are
different questions:

- **Where may this computation run?** (jurisdiction, contract, air-gap — *placement*)
- **How much is done to the data on the way through?** (detection, redaction to the model,
  redaction to the client, what telemetry stores — *protection*)

RouteForge shipped with these fused into one control, `policy_mode ∈ {enforced,
passthrough}`, which simultaneously decided six independent things: whether the detector
ran, whether input was redacted before the model, whether the app's custom recognizers
ran, whether detected sensitivity could escalate placement, whether egress redaction ran,
and which telemetry capture values were legal.

Four consequences were observable in the shipped code at `a7db4f5`:

1. **A configuration customers asked for could not be expressed.** *"The trusted,
   in-boundary model and tool see the real value; the client response is redacted;
   telemetry stores a redacted copy or nothing."* The admin API rejected the closest
   approximation outright (`admin_routes.py:631`, `passthrough + capture_io: redacted`)
   because the exporter's "redacted" view was defined as whatever the gate had handed the
   *model* (`app/obs/export.py:79`).
2. **A placement setting silently rewrote a protection value.** `app/api/routes.py:599`
   turned `capture_io: full` into `redacted` for any `onprem_only` app regardless of
   posture — so an app the admin API had accepted as `onprem_only + passthrough + full`
   exported content that was raw apart from a regex pass, under a `redacted` label.
3. **A protection setting silently loosened placement.** Choosing `passthrough` disabled
   the detector, so an SSN that would have forced `onprem_only` under `enforced` no longer
   did.
4. **One axis was never validated at all.** `set_policy` checked `residency`,
   `pii_detection`, `stream_mode` and `policy_mode` with four hand-written conditionals
   and stopped; `capture_io` accepted any string.

These are not four unrelated bugs. They are what happens when two orthogonal concerns
share one control and no component can state what the effective policy actually is. **The
research claim is that this failure mode is structural and has a structural fix**: compile
the policy once, in one pure function, with the axes separated by construction, and make
noninterference between the two kinds of axis a checkable property rather than a review
convention.

---

## 2. The intervention

One pure function compiles the effective policy; everything else consumes its output.

```python
# app/policy/compile.py
compile_policy(app_policy: AppPolicyView, deployment: Settings) -> EffectivePolicy
apply_signals(eff: EffectivePolicy, detected: tuple[str, ...], rule) -> EffectivePolicy
```

`compile_policy` is **static and total**: no DB, no clock, no request, no I/O, no
randomness, and every value it returns is a legal member of its axis whatever is stored
and whatever is mounted. It is what the admin API validates, what `app_policy_version`
hashes, what the gate and pipeline read, what the decision certificate reports, and what
the property harness calls hundreds of thousands of times.

Three design decisions carry the weight:

- **Explicit representations replace one mutable sanitized string.** The gate returns a
  frozen `Representations` record — `raw`, `redacted` (`None` *exactly when* no detector
  ran), `detected_entity_types`. Each consumer selects its own view by its own axis: the
  model by `model_input`, the client by `client_output`, the exporter by `telemetry_io`.
  **No consumer reads another's view.** Counterexample CE-1 is precisely a consumer
  reading another's view.
- **One authority per question.** `resolve_residency` is shared between the compiler and
  `app/policy/context.py`, because placement decided in two places is placement that can
  disagree with itself — and it did (§5, Finding 2).
- **Fail closed, and keep the evidence.** An unparseable value resolves to its axis's
  protective end *and* is recorded in `EffectivePolicy.rejected`, so a validator can tell
  a typo from a deliberate safe setting and refuse the write. Failing closed without
  keeping evidence is how CE-2 persisted `capture_io: "sometimes"` and answered `200`.

---

## 3. The formal model

### 3.1 Axes

Let an **effective policy** be a pair `(P, R)` where `P` is the placement component and
`R` the protection component.

```
Placement:
  residency  ∈  { any  <  private_cloud_ok  <  onprem_only }        (a total order)

Protection (each axis independently ordered, loose < strict):
  model_input        ∈ { raw  < redact }
  client_output      ∈ { raw  < redact }
  telemetry_io       ∈ { full < redacted < off }
  detection          ∈ { off  < regex < presidio }
  content_integrity  ∈ { transformations_allowed < exact }
  stream_mode        ∈ { stream_unverified < incremental_verify
                         < verified_then_stream < off }
```

`policy_mode` is **not** an axis. It is a preset — a section of the protection product:

```
enforced     ↦  (model_input = redact, client_output = redact)
passthrough  ↦  (model_input = raw,    client_output = raw)
                 and, only when pii_detection is unset, detection = off
```

Protection is the product lattice `R = ∏ᵢ Aᵢ` under componentwise order. The full policy
space is `L = Placement × R`, also a lattice under the componentwise order, with meet and
join taken componentwise. "Tighter" = higher in `L`.

### 3.2 Resolution as a fold over precedence

Each axis is resolved by a four-rung fold, highest-priority first:

```
resolve(a) =  ceiling(a)          if a ceiling forbids the requested value
              app[a]              if the app named a legal value
              preset(policy_mode)[a]   if the preset covers a
              default(a)               otherwise
```

with two closure properties that make the rest tractable:

- **(Totality)** `resolve(a) ∈ Aᵢ` for every stored input and every deployment. Any value
  that is not a legal member of `Aᵢ` — from the app, from a preset, or from a deployment
  default — maps to `deny(a) = ⊤ᵢ`, the strict end of that axis.
- **(Ceiling monotonicity)** a ceiling only ever substitutes a value `≥` the one requested.
  A ceiling that cannot be parsed as the literal boolean `true` reads as closed. So
  ceilings are a closure operator on `R`: idempotent, extensive, order-preserving.

### 3.3 The meet

The one cross-axis effect in the product is `config/policy.yaml`'s `sensitivity_rules`:
observing entity types raises sensitivity, residency, `risk_class` and
`require_verification`. `apply_signals` applies it, and is defined to be **raise-only on
placement and the identity on protection**:

```
apply_signals((P, R), E, rule) =
    ( P ⊔ (escalate ? gate(rule) : ⊥),  R )

where  gate(rule) = the residency the declared rule names
       escalate   = the app's escalate_residency_on_detection
       E          = the observed entity types  (E = ∅  ⇒  identity)
```

The join on a total order is `max`, so placement is **non-decreasing** in the observation.
Protection is untouched — literally: `apply_signals` returns `replace(eff, placement=…,
require_verification=…, sensitivity=…, cross_axis_rules_fired=…)` and never names a
protection field.

This is the same algebra as roadmap item IP-1's per-hop context ratchet, applied at a
single request rather than across hops. RouteForge removed its internal agent runtime on
2026-09-17, so there is exactly one gate seam per model call; the multi-hop extension
becomes a per-conversation ratchet and is future work (§9).

### 3.4 Noninterference, stated

Write `π_P` and `π_R` for the projections onto placement and protection. The two
properties the design rests on are noninterference in both directions:

```
(NI-1)  ∀ policies p, deployments d, protection axes a, values v ≠ v' :
            π_P(compile(p[a ↦ v], d))  =  π_P(compile(p[a ↦ v'], d))

(NI-2)  ∀ p, d, residencies r ≠ r' :
            π_R(compile(p[residency ↦ r], d))  =  π_R(compile(p[residency ↦ r'], d))
```

with the single declared exception, which is not an exception to NI-1 as stated — it is a
*different* function, `apply_signals`, applied after compilation and quantified over the
observation rather than over the configuration:

```
(NI-1')  a placement change under apply_signals is admissible only if the firing rule is
         named in BOTH cross_axis_rules_fired AND placement.sources
```

NI-1 is an information-flow property in the classical sense: the protection component is
*high* input with respect to the placement observer, and the placement projection must be
invariant under variation of high input. The unusual part is not the statement but that it
is worth stating at all — in a policy gateway the two are routinely fused, and fusing them
is what produced all four bugs in §1.

### 3.5 What "tightening" means for the eligible set

Placement is only interesting because it bounds the candidate pool. Define

```
elig(P, registry)  =  { m ∈ registry  :  provider(m) ∈ allowed_providers(P) }
```

Then monotonicity of the whole pipeline is the statement that `elig` is antitone in the
policy order: tightening any axis never enlarges `elig`. `onprem_only` is enforced **by
construction** — cloud providers are removed from `allowed_providers` before the router
scores anything, never rejected afterwards. `elig = ∅` is a *rejection*, never a silent
relaxation; that is roadmap item IP-9's derived reject, and `has_healthy_onprem() = false`
is one of its inputs.

---

## 4. The invariants, and how each is checked

**Eight invariants**, carried by ten property functions in
`tests/policy_compiler/test_invariants.py` (invariant 4 takes two), over `compile_policy`
/ `apply_signals` and the real placement path. The module total of twenty test functions
in `tests/policy_compiler/` is these ten plus the ten counterexample regressions of §6.

| # | Invariant | Property |
|---|---|---|
| 1 | **Monotonicity** — tightening any axis never enlarges what is permitted | `test_tightening_never_enlarges_what_is_permitted` |
| 2 | **Noninterference (NI-1)** — a protection axis never moves placement unless a declared rule fired and is named | `test_protection_never_changes_placement` |
| 3 | **Noninterference (NI-2)** — changing residency leaves every protection value *and its recorded source* unchanged | `test_placement_never_changes_protection` |
| 4 | **Ceilings win; malformed ceilings close** | `test_ceilings_win`, `test_malformed_ceiling_closes_the_permission` |
| 5 | **Empty eligible set rejects** — never a silent relaxation (IP-9) | `test_empty_eligible_set_rejects` |
| 6 | **Preset fidelity** — the golden table of currently-legal combinations | `test_preset_fidelity_matches_the_golden_table` |
| 7 | **`onprem_only` never yields a cloud candidate**, for every protection combination, with the risk floor clamped *inside* the residency boundary (CLAUDE.md rules 3 and 8) | `test_onprem_only_never_yields_a_cloud_candidate` |
| 8 | **Totality** — every compiled value is legal whatever is stored and whatever is mounted, including malformed deployment defaults | `test_every_compiled_value_is_legal_and_malformed_defaults_deny` |

Invariants 5 and 7 run through the real `build_context` and the real `Router`, not a
reimplementation. Invariant 6 compares against the golden table's own independently
written contract function rather than against the compiler.

### 4.1 The save-time check

Invariants 1–3 are properties of the *compiler as a function*, so they cannot be checked
by looking at one output. `set_policy` therefore compiles the app being saved over a
**fixed, bounded 18-point grid** — the three residency values, both extremes of each of
the six protection axes, and three detected-entity signals — and refuses the write with
`PolicyViolation(code="INVARIANT_…")` if any of the three fails for that app. The grid is
a module-level constant built from literals at import time, so the write path pays a
fixed, small number of pure-function calls and never unbounded work
(`test_policy_save_validation.py` asserts `len(_INVARIANT_GRID) <= 64`).

Two of the three checks are deliberately **not** a re-implementation of the compiler that
would trivially agree with it: the escalation check re-derives which `sensitivity_rules`
entry must fire by reading the config directly rather than asking `apply_signals`, and the
unnamed-placement-change check inspects `cross_axis_rules_fired` / `placement.sources`,
which a compiler that moved placement without declaring why would fail whatever value it
moved to.

This is the mechanism that makes `app_policy_version` mean something for IP-4: a persisted
version hash is one that passed the grid.

### 4.2 Mutation adequacy

Every property was checked by breaking the behaviour it asserts over and confirming the
property went red. **15 of 18 mutants caught.** The three misses are analysed rather than
rounded away, in §8.

---

## 5. What the harness actually found

**This is the strongest evidence in the file.** The counterexamples of §6 are bugs that
were already known and already fixed; the harness re-asserts them. These two are bugs
**nobody knew about**, found by Hypothesis on the harness's *first* run, both pre-existing
in shipped code, both fail-**open** on placement, and both now fixed.

### 5.1 Finding 1 — a falsy stored residency failed open

Fixed in **`4fa0fb7`** (`app/policy/compile.py`, 2 lines of logic plus a citation
comment). Refactored into `resolve_residency` by `7a18303`.

Minimal reproducing input, shrunk by Hypothesis on the first run of
`test_ce2_every_axis_fails_closed_and_is_refused`:

```
field='residency', bad='', view=AppPolicyView(residency='')
AssertionError: residency='' compiled to 'any', not the fail-closed 'onprem_only'
```

The compiler opened with

```python
residency = _get(app_policy, "residency") or "any"
```

That single `or` resolved **every falsy stored value** — an empty string from a migration,
a `default_residency: ""` written through the admin API — to `any`, the **loosest**
placement, silently, with nothing recorded in `rejected` for a validator to refuse the
write on. Every other axis denied an unparseable value; residency, the one axis that *is*
placement, did not. It contradicted the module's own documented contract and CLAUDE.md
rule 5 (fail closed).

Reachable without touching the database: `CreateAppIn.default_residency` and
`UpdateAppIn.default_residency` were plain `str` with no enum check, and a policy row with
null residency inherits `app.default_residency`.

The fix distinguishes `None` (unset → inherit `any`, the documented null-means-inherit
default) from any other unparseable value (→ deny to `onprem_only`, record in `rejected`,
refuse the write with `RESIDENCY_UNKNOWN_VALUE`).

**Why it matters for the paper.** This is exactly the class of bug a property-based
harness is supposed to find and a test suite of examples is not: nobody writes the test
case `residency=""`, because nobody intends to store `""`. It was found by quantifying
over "values that are not legal members of the axis" rather than over "values a developer
thought of", and the shrinker reduced it to one character.

### 5.2 Finding 2 — the placement path disagreed with the compiler, and fell open

Fixed in **`7a18303`**, at both ends.

Minimal reproducing input:

```python
view = AppPolicyView(residency="ONPREM_ONLY", escalate_residency_on_detection=False)

compile_policy(view, CLOUD).placement.residency   # 'onprem_only'   (denied — correct)
build_context(...).allowed_providers              # frozenset({'azure','bedrock','onprem'})
build_context(...).onprem_only                    # False
```

One misspelled value, and the compiler and the request path gave **opposite** answers
about where an application's compute could run. `_placement_providers` branched

```python
if residency == "onprem_only": ...
else: allowed = enabled          # <- any unrecognised value lands here
```

so any value it did not recognise was treated as the **loosest** placement. With
`escalate_residency_on_detection: false`, the `_stricter` call that would have raised
`KeyError` was skipped entirely and the request was routable to cloud. With escalation on,
the same value raised `KeyError` — a 500, which at least did not route.

Reachable through the admin API with no direct DB write: create the app, save a policy
with `escalate_residency_on_detection: false` (legal), then `PATCH` the application's
`default_residency` to a typo — `update_application` whitelisted the column and validated
nothing.

Fixed at both ends, smallest change that closes it:

1. **One authority for what a stored residency means.** `resolve_residency(value) ->
   (residency, parsed)` in `compile.py`; `context.py` calls the same function, in
   `build_context` before `_stricter` and the placement filter, and again inside
   `_placement_providers` as a second line of defence. The compiler and the placement path
   can no longer disagree **by construction** rather than by review.
2. **The admin boundary.** `create_application` and `update_application` now validate
   `default_residency` against the enum, in the same 422 shape as every other enum field.
   That is how the typo got stored.

**Why it matters for the paper.** Finding 1 is a fail-open in one function. Finding 2 is
the failure mode the whole "compile once, consume everywhere" thesis predicts: as long as
*two* components independently decide what a stored value means, they will eventually
disagree, and the disagreement will resolve towards the permissive side, because the
permissive side is the `else` branch. The fix is not a better `if`; it is removing the
second decision site. That is the paper's actual argument, demonstrated on its own
codebase, against a bug neither the author nor the reviewer had noticed.

### 5.3 A harness bug the harness found

Recorded because a property-based suite whose own fixtures collide is a *silently*
weakened invariant — the failure mode this whole exercise is most concerned with.

The first run of `test_malformed_ceiling_closes_the_permission` failed with
`bad_passthrough=1`. The compiler was right (`_flag` is `value is True`); the harness was
wrong. Its `Settings` memo was an `@lru_cache`, and in Python `1 == True` with `hash(1) ==
hash(True)` — so the `allow_app_passthrough = 1` deployment was handed the settings built
for `True`, and the malformed-ceiling property would have "passed" against a well-formed
deployment. The cache key now carries each value's type.

---

## 6. The three counterexamples — and an honest account of what is demonstrated

Design §13 states: *"The harness finds `CE-1`, `CE-2` and `CE-3` from a clean checkout of
the pre-fix code, and passes after the fixes."*

**That claim is not achievable as written, and it is dropped.** It is stated here rather
than quietly omitted, because it is the kind of claim a reviewer will try to reproduce.

The reason is mundane and structural: **the modules the CE tests assert over post-date the
bugs.** CE-1 lived at `app/api/routes.py:596-598`; the CE-1 tests assert through
`compile_policy` and `app/obs/export.py::resolve_capture_io`, neither of which existed in
the pre-fix tree — and reinstating the clamp at its original site *today* would redden
nothing, because `routes.py` no longer decides capture at all. CE-2 lived as a missing
check in an HTTP handler; the CE-2 tests call `validate_effective` directly. Nothing in
this work ran pre-fix code at all.

**What is demonstrated instead**, and it is a weaker but still meaningful claim: *each bug
cannot be re-introduced at the seam the design moved the decision to*, plus genuine
live-code mutation coverage for three of the four halves.

| | Bug's original site | Fix | What the harness pins | Live-code mutation? |
|---|---|---|---|---|
| **CE-1** (clamp half) — a placement setting rewrote a telemetry protection value | `app/api/routes.py:596-598` (deleted) | `47ecfcc`, `87679aa` | **Seam-level.** The clamp cannot be written at the compiler seam: `test_ce1_no_residency_ever_moves_telemetry_io` quantifies NI-2 over all policies and deployments. | No — mutation M3 inserts an equivalent clamp *into* `compile_policy`, a different location. Honest reading: "this bug cannot be written at the new seam", not "the harness finds the old bug". |
| **CE-1** (exporter half) — the `redacted` branch exported `sanitized_prompt or redact(prompt)`, i.e. the view the model got | `app/obs/export.py:70-75` (rewritten) | `47ecfcc`, `981061f` | **Live code.** `resolve_capture_io` cannot label raw as redacted (`test_ce1_a_redacted_label_always_has_a_redactor_behind_it`). | **Yes** — mutant CE-1b (remove the `redacted`-with-no-detector guard): caught, 1.34 s. |
| **CE-2** — `set_policy` accepted any string as `capture_io` | `app/api/admin_routes.py:424-431` (rewritten) | `1030019`, `e8514d0` | **Seam-level.** The authority `set_policy` delegates to cannot have the hole. | **Yes** — mutant CE-2b (`_resolve` denies without recording `rejected`, which is CE-2's actual failure mode): caught, 0.72 s. |
| **CE-3** — `passthrough` silently disabled placement escalation for a detected SSN | `app/pipeline.py:216-217` (rewritten) | `e064c40` | **Live code, original site.** `test_ce3_passthrough_with_a_named_detector_still_scans` calls the real `Pipeline._gate_inputs`. | **Yes** — mutant CE-3 (`escalate = … and mode != "passthrough"`): caught, 1.07 s. |

End-to-end coverage for CE-1 and CE-2 *does* exist, in suites this work did not write:
`tests/test_capture_io.py` / `tests/test_obs_export.py` at span level, and
`tests/test_admin_api.py` at the HTTP boundary. The same narrowing is recorded in the
module docstring of `test_counterexamples.py` and in the four affected test docstrings, so
a reader who never opens this file still gets it.

### 6.1 The CE-3 residual, kept on purpose

An app stored as `passthrough` with `pii_detection` NULL still resolves `detection: off`,
still gets the null detector, and therefore still escalates nothing — which *at the level
of outcomes* is CE-3 for that population. Design §4 keeps this deliberately: those rows
must stay byte-identical for backward compatibility.

What changed is that it is no longer **silent**. The compiled `detection` axis says `off`,
`escalate_residency_on_detection` still says `true`, both appear in the decision
certificate, and both are folded into `app_policy_version`.
`test_ce3_residual_passthrough_without_a_named_detector_still_scans_nothing` exists so the
residual cannot be mistaken for something the harness proved absent.

---

## 7. Reproducing it

```bash
git clone <repo> && cd routeForge
git checkout feat/policy-compiler-p1a          # merge base a7db4f5
uv sync --group dev                            # installs hypothesis, OTel SDK, prometheus-client

# the harness: 20 test functions (10 invariant properties + 10 CE regressions), ~8 s
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/policy_compiler -o addopts="" -q

# with the compiler's own unit + golden-table suites (1514 tests, ~8 s)
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest \
    tests/policy_compiler tests/test_policy_compile.py \
    tests/test_policy_golden_table.py -o addopts="" -q

# search rather than regression: 2000 examples, non-derandomized
HYPOTHESIS_PROFILE=deep LITELLM_LOCAL_MODEL_COST_MAP=True \
    uv run pytest tests/policy_compiler -o addopts="" -q

# which branches the properties actually reached
uv run pytest tests/policy_compiler -o addopts="" -q --hypothesis-show-statistics
```

`LITELLM_LOCAL_MODEL_COST_MAP=True` is not optional: without it LiteLLM fetches a cost map
over the network at import and pytest wedges in D-state on an offline machine.

**Layout.**

| File | Contents |
|---|---|
| `tests/policy_compiler/strategies.py` | Hypothesis strategies for `AppPolicyView` (legal, invalid and design-§9 unimplemented values), `Settings` (both ceilings, including malformed ones, and malformed deployment *defaults*), and detected-entity tuples drawn from `config/policy.yaml`'s own `sensitivity_rules`. Also the two profiles. |
| `tests/policy_compiler/test_invariants.py` | The eight invariants of §4, as ten property functions. |
| `tests/policy_compiler/test_counterexamples.py` | CE-1..3, each named for the bug it encodes, with the pre-branch `file:line` in its docstring. |

**Profiles.** `default` is 200 examples with `derandomize=True` — deliberately a
*regression net*, not a search, so the inputs cited in this document stay reproducible and
CI-less runs stay deterministic. `deep` is 2000 examples, non-derandomized, and is the one
to run when looking for something new.

**Mutation protocol.** Each mutant is a single edit to live code that breaks exactly the
behaviour one property asserts over; the mutant is "caught" if that property goes red.
Mutants are applied by hand and reverted immediately; they are listed with their verdicts
and runtimes in `.superpowers/sdd/2026-09-18-policy-compiler-p1a/task-10-report.md` §2–§3
and §8.2.

---

## 8. What is not proven

Read this section before writing any claim sentence.

- **No end-to-end demonstration from pre-fix code.** §6. None of CE-1..3 is shown to be
  found by the harness against a clean pre-fix checkout, and for CE-1 and CE-2 it cannot
  be, because the modules those tests exercise post-date the bugs. The demonstrated claim
  is the narrower "cannot be re-introduced at this seam", plus live-code mutation coverage
  for CE-1's exporter half, CE-2's parse-and-record path, and CE-3.

- **The harness quantifies over the declared signal, not over prompts.** The properties
  take a tuple of detected entity *types* as input. Whether a given detector would find
  `US_SSN` in a given string is the detectors' business (`tests/test_detectors.py`). So
  "a protection axis never moves placement" is proven for the placement function *given
  the same observation*. It is **not** the claim that turning the detector off cannot
  change what is observed. It can — that is exactly the CE-3 residual (§6.1).

- **The save-time check does not catch over-escalation or two-axis-simultaneous bugs.**
  Its escalation check is a *lower* bound (`after_rank < expected_rank` fails); a compiler
  that escalated *further* than the declared rule requires passes it. And its grid varies
  one axis at a time from the app's own stored policy, so a bug that only manifests when
  two axes move together is outside it by construction. The grid is bounded on purpose —
  it is on the write path — and the unbounded search is the harness's job, off that path.

- **The harness constrains the compiler seam, not every site.** It proves things about
  `compile_policy`, `apply_signals`, `resolve_capture_io`, `validate_effective`,
  `build_context` and the real `Router`. It says nothing about the several dozen other
  places a future edit could re-couple the axes. "This bug cannot be written *here*" is
  the claim; "this bug cannot be written" is not.

- **Two components are asserted at the seam, not verified end-to-end.** The exporter
  property is stated over `resolve_capture_io` (pure), not over a live OTel span. Span-level
  coverage exists in `tests/test_capture_io.py` and `tests/test_obs_export.py`, which are
  a different suite.

- **`_placement_providers`'s own residency normalisation is not independently exercised.**
  `build_context` normalises first, so mutant M10 (removing the second call) changes
  nothing this suite observes. It matters only on the `requestable_models` / `GET
  /v1/models` path, which has no property here. Kept as defence in depth; its *independent*
  value is unproven.

- **Mutation adequacy is 15/18, and the three misses are:** (a) **CE-2a**, bypassing
  `_resolve`'s membership check for `telemetry_io` — an equivalent mutant, and now
  demonstrably so rather than by assumption, because the second guard it falls through to
  is itself under property coverage; (b) **M10**, above; (c) an earlier "mis-aimed anchor"
  verdict on a malformed-deployment-default guard that turned out to be **a real coverage
  hole, not an equivalent mutant** — the strategies drew only legal deployment defaults, so
  nothing could reach the guard. Absence of coverage is not evidence of fail-closed
  behaviour. It was closed in `7d7624b` (the strategies now draw malformed defaults ~20% of
  the time; the guard is exercised and three mutants on that path are caught). That
  mis-verdict is worth a sentence in the paper on its own: **a mutation score computed
  against strategies that cannot reach the mutated line overstates adequacy**, and the
  overstatement looks exactly like an equivalent mutant.

- **Presidio is not installed in the development environment**, so `detection: presidio`
  is exercised only as a compiled *value*, never as a running detector. Deployments
  generated by the harness use `regex` wherever a detector is actually constructed.

- **`derandomize=True` in the default profile means repeated runs explore the same
  inputs.** It is a regression net at 200 examples. New search is the `deep` profile, run
  deliberately.

- **`INVALID_VALUES` is a fixed list, not `st.text()`.** Free-form text would explore more
  of the space at the cost of a slower, noisier suite; the fixed list is the set of
  near-misses that actually occur in practice. Worth revisiting if `deep` ever stops
  finding anything.

- **The evidence is qualitative.** Two real bugs found, eight properties, 15/18 mutants.
  There is **no** quantitative claim here — no bug-detection rate against a seeded fault
  population, no comparison against an example-based suite of matched effort, no
  feasible-oracle metric. Those belong to a later phase and are the honest answer to "how
  much did this buy you?". Do not let a paper draft imply otherwise.

---

## 9. Related work — TO VERIFY BEFORE CITING

⚠️ **Nothing in this section has been verified.** The arXiv identifiers in
[`docs/roadmap/ip-and-research-opportunities.md`](../roadmap/ip-and-research-opportunities.md)
came from automated search and **are leads, not citations**; that document's own caveat
says a meaningful share of the 2026-dated IDs (`26xx.xxxxx`) could not be independently
confirmed, and that some may not exist. **Every identifier must be checked against
arxiv.org before it appears in a draft.** The same applies to the "nobody does X" claims:
they mean "not found by targeted search in one pass", which is evidence of a gap, not proof
of one.

Threads to work through, with what needs establishing in each:

1. **Language-based information-flow security.** Noninterference, security type systems,
   declassification. This is the closest formal ancestor of §3.4 and the literature is
   mature and stable — Sabelfeld & Myers' 2003 survey is the standard entry point, and
   Jif/FlowCaml are the canonical systems. **What to establish:** that applying
   noninterference to a *configuration compiler* (as opposed to a program's data flow) is
   an unclaimed framing, and that the declared cross-axis rule maps onto the
   declassification literature's "where/what/who" dimensions. This is the reference class
   the paper most needs and the roadmap does not cover it at all.

2. **Configuration correctness and misconfiguration.** The systems literature on
   configuration errors as a dominant cause of outages, and on config validation /
   constraint inference. **What to establish:** whether anything treats a configuration
   surface as a *lattice with checkable interference properties*, rather than as a set of
   constraints to validate independently.

3. **Policy languages and formal policy analysis.** XACML analysis, OPA/Rego verification,
   firewall/ACL conflict detection, IAM policy reachability (AWS Zelkova is the obvious
   comparator). **What to establish:** these analyse whether a policy *permits* a request;
   the claim here is orthogonal — that changing one dimension of a policy must not change
   another. Confirm the distinction survives contact with the reachability literature.

4. **Property-based testing and mutation adequacy.** QuickCheck-lineage work, and the
   mutation-testing literature on equivalent mutants and on mutation score as an adequacy
   proxy. **What to establish:** support for the §8 observation that a mutation score
   computed against strategies that cannot reach the mutated line is an overstatement, and
   that it is indistinguishable from an equivalent mutant without coverage evidence.

5. **PII detection and over-redaction in LLM pipelines.** The roadmap cites SlotGuard
   (`2607.17147`) for generic redaction degrading task success and typed slots preserving
   it, and MathEd-PII (`2602.16571`) for numeric false positives in tutoring. **Both IDs
   are unverified.** These are the right supporting citations for *why* the
   `model_input: raw` + `client_output: redact` configuration needed to exist.

6. **Agentic routing on accumulating state.** The roadmap's IP-1/R3 cluster — Agentic
   Routing (`2607.11399`), ACRouter (`2606.22902`), TRACE-Router (`2607.22465`), Agora
   (`2607.09600`), OCELOT (`2606.12341`). **All unverified.** The claim to test is that
   these route per hop on *harness* state and none routes on accumulating *privacy* state.
   Relevant to the multi-hop extension (below), not to P1a itself.

7. **Compliance evidence and audit.** Traccia (`2607.14309`), Policy-as-Prompt
   (`2509.23994`), Strategic Auditee Gaming (`2605.06340`) — all unverified. Relevant to
   IP-4 and to §4.1's binding of an invariant check to a version hash.

**Future work, honestly scoped.** (a) The multi-hop ratchet: RouteForge removed its
internal agent on 2026-09-17, so the natural extension is a *per-conversation* meet keyed
on a session identifier, with a proof that eligible-set cardinality is non-increasing
across the conversation. (b) Quantitative evaluation against a seeded fault population.
(c) Extending noninterference from the compiler to the consumers, so "no consumer reads
another's view" becomes a checked property rather than a reviewed one.

---

## 10. Provenance

| | |
|---|---|
| Design | `docs/superpowers/specs/2026-09-18-policy-compiler-p1a-design.md` |
| Per-task reports (incl. every mutation verdict) | `.superpowers/sdd/2026-09-18-policy-compiler-p1a/` |
| Operator-facing contract | [`docs/reference/per-app-config.md`](../reference/per-app-config.md) |
| IP / research menu, and the verification caveat | [`docs/roadmap/ip-and-research-opportunities.md`](../roadmap/ip-and-research-opportunities.md) |

Commits, oldest first: `5410fc6` (the pure compiler) · `1030019` (deny value for every
axis, keep what was rejected) · `82e3d00` (the four columns) · `e8514d0` (validate through
the compiler) · `c9f0518` · `157dea9` (the save-time invariant check) · `e064c40` (gate and
pipeline read the compiled policy — CE-3) · `4dea25a` · `768d003` (egress is
`client_output`'s call) · `c028eb4` · `47ecfcc` / `981061f` (telemetry projects
independently — CE-1) · `b8a0b73` (`content_integrity: exact`) · `c76a47a` (the decision
certificate) · `87679aa` · `4fa0fb7` (the harness; Finding 1) · `7a18303` (Finding 2) ·
`7d7624b` (malformed-deployment-default coverage).
