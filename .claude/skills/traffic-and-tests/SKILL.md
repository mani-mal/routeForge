---
name: traffic-and-tests
description: Use when building the traffic generator, request personas, or the test suite for the Serving Plane. Trigger on mentions of traffic generation, load testing, personas, test suite, pytest, or demo data.
---

# Traffic Generator & Test Suite

Two audiences: the traffic generator produces a **story** for customers; the test
suite proves **correctness** for us. Don't conflate them.

## Personas

Weighted mix, defined in `tests/personas.yaml`. The weighting is the demo's argument:
most traffic is cheap, so most spend should be too.

| Persona | Weight | Purpose |
|---|---|---|
| `simple_qa` | 45% | Trivial → cheap tier. Bulk of the savings. |
| `moderate` | 20% | Mid tier. |
| `pii_bearing` | 15% | SSN/email/account numbers → `onprem_only`. **The privacy proof.** |
| `multi_step` | 12% | Agent workflow, 3–5 hops, per-call routing. |
| `high_risk` | 5% | Forces `frontier` tier (best allowed) regardless of cost; `onprem_only` variant stays on best on-prem tier. |
| `low_confidence` | 3% | Triggers exactly one escalation. |
| `large_tool_output` | — | Overlay on `multi_step`: big JSON/log payloads so Headroom has something to compress. Without this the compression demo has no material. |

Synthetic PII only — generate with Faker. **Never** use real customer data in a demo
corpus, and say so in the README.

## Generator

```bash
python -m tests.traffic --requests 500 --rps 10 --profile demo
python -m tests.traffic --requests 500 --seed 42 --compare-compression  # A/B off vs on
```

Async, configurable RPS, emits to the same API the UI watches so the dashboard fills
live. Support `--seed` for reproducibility — a demo that produces different numbers
each run is hard to narrate.

Print a summary on completion: tier distribution, total vs. baseline cost, % saved,
count kept on-prem, p50/p95 latency. These are the numbers you'll read aloud.

## Test suite

`pytest`, split by concern:

- `test_policy_gate.py` — detection, redaction, fail-closed, context immutability
- `test_router.py` — scoring, policy exclusion, escalation cap, high-risk override
- `test_backends.py` — Protocol conformance, `none` + `onprem_only` rejection
- `test_compression.py` — reduction, bypass rules, CCR residency, post-compression pricing
- `test_output_controls.py` — step 9: leakage check on completions, `require_verification`
  flow, escalation-then-fail-closed, `stream: true` rejected (see `output-controls` skill)
- `test_observability.py` — traces exported; **assert no raw PII and no CCR contents in spans**
- `test_agent_runtime.py` — hop count, per-hop routing, step/spend caps
- `test_e2e.py` — each persona end-to-end through the real pipeline

## Assertions that matter most

These encode the demo's promises. If any fail, the demo is dishonest:

1. A `pii_bearing` request **never** reaches a cloud provider — assert on the
   gateway call log, not just the decision record.
2. A known SSN appears in **no** exported Langfuse span.
3. `inference_backend: none` + `onprem_only` → rejection, zero model calls.
4. Multi-step task produces one DecisionRecord per hop.
5. Cost accumulates across hops; totals match the sum of parts.
6. Escalation happens at most once per call.
7. With Headroom on, the router prices `tokens_after` — assert the scored cost matches
   post-compression counts, not pre.
8. CCR writes stay inside the residency boundary; for `onprem_only` assert no external
   write path is touched.
9. Compression failure degrades gracefully — request succeeds uncompressed.
10. Routing savings and compression savings are tracked separately and sum to total.
11. Step 9 verifies egress: PII in a completion is redacted before return, a
    `require_verification` request that can't be verified fails closed (never returns
    the unverified completion), and an `onprem_only` request that escalates on
    verification stays on-prem.

## Fixtures

Fix time, seed randomness, use the mock backend. Tests must not need GPUs or network.
CI runs this on a laptop.
