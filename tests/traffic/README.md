# Traffic generator

> **This is a load / seed tool, not the product's user model.** The *personas*
> here simulate many client applications hammering the router so the dashboard
> fills with realistic traffic — they are test fixtures, not real accounts. A
> real application integrates via an admin-issued API token; see
> [`docs/help/integrating-an-app.md`](../../docs/help/integrating-an-app.md).
> The personas send the legacy seeded tokens (`acme-key`, `hospital-key`,
> `demo-key`), which map to the seeded demo applications.

Fires a seeded, weighted mix of synthetic requests at the live Serving Plane
API so the demo dashboard fills with realistic traffic (CLAUDE.md demo
narrative step 5). Two pieces:

- `tests/traffic/personas.py` — the weighted persona mix and synthetic
  request-body generators (**this file**, Phase 7 Task 1).
- `tests/traffic/generator.py` + `tests/traffic/__main__.py` — the async
  driver and CLI (Phase 7 Task 2).

## Synthetic data only

Every SSN, email address, and account number this generator produces is
**synthetic**, hand-rolled from a seeded `random.Random(seed)` in
`personas.py` (`fake_ssn`, `fake_email`, `fake_account`, `fake_phone`). No
Faker, no network calls, no real customer data — ever. The shapes are chosen
to match `app/policy/detectors.py`'s `RegexDetector` patterns exactly (e.g.
SSN `\d{3}-\d{2}-\d{4}`) so PII-bearing personas reliably trip the policy
gate offline, the same way real PII would in production with Presidio.

## Personas

Weights live in `tests/personas.yaml` — the single source of truth, loaded by
`load_personas()`. The weighting is the demo's argument: most traffic is
cheap, so most spend should be too.

| Persona | Weight | api_key | tenant | expect_onprem | Purpose |
|---|---|---|---|---|---|
| `simple_qa` | 45 | `acme-key` | acme | false | Trivial question -> cheap tier. Bulk of the savings. |
| `moderate` | 20 | `acme-key` | acme | false | Somewhat longer question -> mid tier. |
| `pii_bearing` | 15 | `hospital-key` | hospital | **true** | Synthetic SSN + email in the body -> gate detects `US_SSN`/`EMAIL_ADDRESS` -> `onprem_only`. **The privacy proof.** |
| `multi_step` | 12 | `acme-key` | acme | false | `mock_multistep: true` -> agent runtime, several hops, per-call routing. Carries the `large_tool_output` overlay: a big newline-*duplicated* log block (duplicate lines compress under Headroom; a single long line does not) so the compression demo has material. |
| `high_risk` | 5 | `hospital-key` | hospital | **true** | Synthetic bank account number -> `US_BANK_NUMBER` -> restricted/high risk -> best-allowed (on-prem) tier, `require_verification`. |
| `low_confidence` | 3 | `acme-key` | acme | false | `mock_confidence: 0.1` (below the 0.60 escalation threshold in `config/routing.yaml`) -> exactly one escalation. |

`expect_onprem` documents test/demo intent; the actual `onprem_only` decision
is made by the policy gate (`config/policy.yaml` `sensitivity_rules`) from
the detected entities, not by the persona metadata.

Note: `MEDICAL_LICENSE` and `EMPLOYEE_ID` (also `restricted`/`onprem_only` in
`config/policy.yaml`) are Presidio-only entity types — `RegexDetector` (used
by the offline test suite) doesn't recognize them. `pii_bearing` and
`high_risk` instead use `US_SSN`/`EMAIL_ADDRESS` and `US_BANK_NUMBER`, which
the regex detector does catch, so these personas trip the gate the same way
whether `policy.pii_detection` is `regex` or `presidio`.

## api_key -> tenant mapping

From `app/main.py::API_KEYS`:

| api_key | tenant | default_residency (`config/policy.yaml`) |
|---|---|---|
| `acme-key` | acme | `private_cloud_ok` |
| `hospital-key` | hospital | `onprem_only` |
| `demo-key` | default | `any` |

## Usage

```bash
python -m tests.traffic --requests 500 --rps 10 --seed 42 --profile demo
python -m tests.traffic --requests 500 --seed 42 --compare-compression  # A/B: Headroom off vs on
```

Same `--seed` always produces the same request stream (deterministic
`random.Random(seed)` sampling over the weighted mix) so the demo's numbers
are reproducible run to run. On completion the CLI prints the demo's
headline numbers: tier distribution, total vs. baseline (all-frontier) cost,
% saved (split into routing-saved and compression-saved, tracked
separately), on-prem count, and p50/p95 latency.

## Assertions that matter most (coverage map)

Phase 7 Task 3. The `traffic-and-tests` skill lists 11 assertions that "encode
the demo's promises" — if any fail, the demo is dishonest. Most already have
tests elsewhere in the suite; this table cites the real files/functions so a
reader can verify the demo's promises are actually checked, not just
asserted in prose.

| # | Assertion | Covered by |
|---|---|---|
| 1 | A `pii_bearing` request never reaches a cloud provider — assert on the **gateway call log**, not just the decision record. | `tests/test_traffic_honesty.py` (new, this task) — the primary guard: `test_pii_request_never_calls_a_cloud_provider_at_the_backend` and `test_high_risk_request_never_calls_a_cloud_provider_at_the_backend` wrap a `RecordingBackend` around the (faked) LiteLLM SDK call — all egress now goes through `LiteLLMBackend`, mock is removed — and assert directly on `models_called`, not the DecisionRecord. `test_bedrock_enabled_pii_stays_onprem_while_clean_request_can_reach_cloud` is the **discriminating pair**: with cloud disabled (the shipped default) *all* traffic is onprem regardless of whether the residency filter runs at all, so that config alone can't prove anything — it enables `bedrock` and shows the SAME cloud-enabled settings both (a) keep a PII/onprem_only request fully onprem and (b) let a clean low-confidence request actually reach `bedrock-frontier`, proving `RecordingBackend` really does observe cloud calls when policy permits them. `test_residency_filter_is_what_keeps_pii_onprem_not_provider_availability` goes one step further and reproduces `app/policy/context.py`'s residency filter with the filter deliberately deleted, confirming the guard above would in fact fail if that filter regressed. `test_multistep_pii_request_stays_onprem_at_decision_level` covers the multi-step path at the DecisionRecord level (see its docstring for why the direct-path RecordingBackend approach isn't reused there — the agent runtime self-calls over real HTTP). Decision-record-level precursors (still worth keeping, cheaper to run): `tests/test_policy_gate.py::test_ssn_request_is_onprem_only`, `tests/test_e2e_slice.py::test_pii_request_excludes_cloud_in_trace`, `tests/test_router.py::test_onprem_only_excludes_cloud_with_reason` / `test_high_risk_onprem_only_stays_on_prem`. |
| 2 | A known SSN appears in no exported Langfuse span. | `tests/test_obs_export.py::test_export_defense_in_depth_exporter_redacts` (exporter-level scrub) and `::test_export_own_redaction_removes_pii` (isolates `export_trace`'s own redaction, no exporter wrapper); `tests/test_obs_redact.py::test_redacts_ssn_email_phone`. |
| 3 | `inference_backend: none` + `onprem_only` → rejection, zero model calls. | Resolved by the LiteLLM gateway swap (2026-07-30): `app/policy/context.py` drops `onprem` from the enabled provider set whenever `inference_backend == "none"`, so an `onprem_only` request's `allowed_providers` is empty and the router rejects it before any backend call. `tests/test_none_backend.py::test_none_backend_drops_onprem_for_onprem_only_request` covers this directly; `::test_none_backend_public_request_unaffected_when_cloud_enabled` is the companion check that cloud-eligible traffic is untouched. |
| 4 | Multi-step task produces one DecisionRecord per hop. | `tests/test_agent_runtime.py::test_multi_step_run_yields_hops` (`all(h.step == "agent_hop" and h.chosen for h in result.hops)`); `tests/test_agent_pipeline_e2e.py::test_multistep_request_returns_multi_hop_trace`. |
| 5 | Cost accumulates across hops; totals match the sum of parts. | `tests/test_agent_pipeline_e2e.py::test_multistep_request_returns_multi_hop_trace` (`total_usd == sum(hop costs)`); `tests/test_two_lever_cost.py` (both tests, direct-path additive identity `compression_saved + routing_saved == baseline - total`). |
| 6 | Escalation happens at most once per call. | `tests/test_gateway.py::test_low_confidence_escalates_exactly_once` and `::test_escalation_cap_holds_when_confidence_stays_low` (confidence stays low every hop; still capped at exactly one escalation). |
| 7 | With Headroom on, the router prices `tokens_after` — scored cost matches post-compression counts, not pre. | `tests/test_agent_hop_compression.py::test_hop_prices_post_compression` (`tokens_priced < 800` against an ~800-line input); `tests/test_two_lever_cost.py::test_cost_dict_has_two_levers` (via `compression_on_pipeline`); `tests/test_controls.py::test_pipeline_honors_runtime_cfg_toggle_between_calls`. |
| 8 | CCR writes stay inside the residency boundary; for `onprem_only` assert no external write path is touched. | `tests/test_compress_orchestrator.py::test_onprem_only_ccr_never_to_cloud` (`ccr_retrievals_to_cloud == 0`); `tests/test_ccr_store.py::test_to_cloud_counter` / `test_store_and_retrieve_local` (the counter mechanism itself). |
| 9 | Compression failure degrades gracefully — request succeeds uncompressed. | `tests/test_compress_orchestrator.py::test_fail_open_on_compressor_error` (compressor raises → `bypass_reason == "compressor_error"`, messages unchanged, request still succeeds — fail *open*, contrast the policy gate's fail *closed*). |
| 10 | Routing savings and compression savings are tracked separately and sum to total. | `tests/test_two_lever_cost.py::test_cost_dict_has_two_levers` and `::test_routing_saved_goes_negative_under_escalation_identity_holds` (identity holds even when `routing_saved_usd` goes negative under escalation — no clamping). |
| 11 | Step 9 verifies egress: PII in a completion is redacted before return; a `require_verification` request that can't be verified fails closed; an `onprem_only` request that escalates on verification stays on-prem. | `tests/test_output_controls.py::test_pii_in_completion_is_redacted`, `::test_required_unverifiable_fails_closed`, `::test_required_clean_passes`; `tests/test_policy_gate.py::test_no_ssn_value_in_response`. **Partial gap:** the "escalates on verification" sub-clause has no corresponding behavior yet — `app/output/verify.py`'s `verify()` never sets `VerificationRecord.escalated = True` (the field exists but is dead), so there is nothing to test there either. |

## Generator (`tests/traffic/generator.py`)

`run_traffic(base_url, *, n, rps, seed, personas=None, headroom=None,
client=None) -> Summary` drives an `httpx.AsyncClient` against a *running*
API — it talks only HTTP, never imports the pipeline directly, so it
exercises the exact path the UI watches and the dashboard fills live. Errors
(422/503/etc, or a network exception) are recorded in `Summary.errors` and
do **not** crash the run.

Tier resolution: the `/v1/chat/completions` response carries `cost` and
`policy.onprem_only` but not the routing `tier` (that mapping lives
server-side, in `HistoryStore`/`app/api/routes.py`). Rather than duplicate
`config/routing.yaml`'s model->tier mapping here, the generator fires all
requests first, then does a single `GET /api/requests?limit=...` and
aggregates `tier_distribution`/`onprem_count`/costs from the returned
`RequestSummary` records, filtered to just the ids this run produced. p50/p95
latency, in contrast, is measured client-side with `time.monotonic()` around
each HTTP round trip — the number a demo audience actually feels.
