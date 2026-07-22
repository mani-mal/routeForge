# Traffic generator

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
