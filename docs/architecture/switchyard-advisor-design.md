# Switchyard advisor — design (B102)

**Status:** signed off by the owner 2026-11-12 (D1 plain mapping, D2 pin 0.4.0a1) · **Date:** 2026-11-12 · **Backlog:** B102 (unblocks B103–B108)
**Sources:** `docs/research/RouteForge-Switchyard-Feature-Comparison-and-Integration-Recommendation.md`,
backlog B100–B112 preamble, `docs/reference/switchyard-integration.md` (B100). Verified against
`nemo-switchyard==0.4.0a1` installed in an isolated venv.

## 1. What this decides

Where NVIDIA NeMo Switchyard plugs into the request path, what it is allowed to see, what it
is allowed to change, and what happens when it fails. Everything here is default-off. With
`routing.switchyard.mode: off`, the code path below never runs.

## 2. The seam: one element of the tier target

`Router._select` computes today's tier target as

```python
targets = [complexity_tier(complexity), <risk floor>, <min_tier if any>]
target  = _clamp_to_best_allowed(max(targets))          # app/routing/router.py ~L531
```

- `complexity_tier` is a length heuristic: `complexity = min(1, tokens_after/2000)`, set in
  `app/pipeline.py` on both the ordinary and streaming paths.
- The risk floor and `min_tier` (escalation) form the **hard floor**.
- The clamp enforces rule 8: the target never goes above what policy allows.

**The advisor replaces exactly the first element and nothing else.** When enforcing, `select`
receives `advisor_tier: str | None`. When it's set, it is used in place of
`complexity_tier(complexity)`. Every other line of `_select` is unchanged:
- the hard floor, the clamp and failover;
- the circuit breaker's step-down, upward failover and budget fallback;
- the within-tier ranking by `RoutingStrategy.rank()`;
- the conflicting core.

Consequences, all by construction:
- **It can't go below the floor.** `max()` with the risk floor and `min_tier` still applies,
  so an "efficient" proposal on a high-risk request still lands on the floor (rule 8).
- **It can't escape placement.** The clamp and the policy-eligible set are computed before
  and independently of the advisor. It names a *tier*, never a model or provider, so it
  can't reach a candidate policy filtered out (rule 3).
- **It can't widen anything.** It feeds one input of a `max()` that was already bounded on
  both sides.
- **Escalation still wins.** A confidence-escalation retry raises `min_tier`, which
  dominates the `max()`.

### Why not `RoutingStrategy.rank()`?

`rank()` reorders the candidates *inside* the tier `_select` already picked. Switchyard's
decision is efficient-vs-capable, which is cross-tier. Inside one tier it would add nothing.
`rank()` stays as it is.

### B80 quality floors keep precedence

When `ctx.quality_floor` is live in `enforce` mode, `_select` takes the B80 path (the cheapest
qualifier). The advisor is not consulted for that call. Its proposal is still recorded, with
`fallback_cause: quality_floor`. In B80 `shadow` mode the B80 path doesn't decide, so the
advisor seam applies as normal.

## 3. Role → tier mapping (D1, decided: plain mapping)

The advisor is handed **role placeholders, not model ids**:

```python
models = {"efficient": ["efficient"], "capable": ["capable"], "any": ["efficient", "capable"]}
```

`run_stream(request, models)` in 0.4.0a1 takes the pool per request and returns one of those
ids. RouteForge then maps the role to a tier. The concrete model is still chosen by
RouteForge's own `select` from the feasible set. So Switchyard never sees a model id, a
provider or a price, and "it may only choose among feasible candidates" holds with nothing
to validate except `role ∈ {efficient, capable}`.

**Recommended mapping:** `efficient → cheap` and `capable → frontier`. Both then pass
through the unchanged `max(floor) → clamp(best allowed)`. On an on-prem-only deployment,
`capable` therefore clamps to the best on-prem tier, and `efficient` on a high-risk request
lands on the floor. `mid` is reached only through the floor or the clamp. This is the
two-model setup Switchyard is benchmarked on, and it keeps the mapping trivial to reason
about.

**Alternative, "lower-only":** `efficient → cheap` and `capable → (today's
complexity tier)`. The advisor can then only *lower* the length-derived target, never raise
it, so cost can only go down versus today. It's safer for cost but discards Switchyard's
"recover on errors" signal, which is half its value.

**Decided (owner, 2026-11-12): the plain mapping**, plus an optional per-deployment
`switchyard.capable_tier: mid | frontier` (default `frontier`) so an operator can cap it at
`mid`. The lower-only alternative was rejected.

## 4. What the advisor sees (rules 1, 2, 7)

- **Input:** the messages the gateway is about to send. These are `comp.messages`: post-gate,
  so `model_input`-shaped (redacted unless the app declared `model_input: raw`), and
  post-compression. That's the same text the chosen model will see, so the advisor never
  sees anything the model doesn't. It runs in-process with no network calls: classifier-less
  `stage_router` makes no model calls, and B103 refuses any `CallModel`/`CallDecision` that
  appears anyway.
- **Pre- vs post-compression:** Headroom may compress away the tool-result text that stage
  routing reads, and that reduces signal. Post-compression is the default because it is the
  model's view, and pre-compression would hand the advisor text the router never prices.
  B109 measures both. If pre-compression is clearly better, it can be offered as an option,
  since both views are already post-gate.
- **Conversion:** the adapter translates RouteForge `Message`s (OpenAI shape) into
  Switchyard's protocol types: `content` as typed blocks, and `tool_calls` / `tool`
  messages as tool-use and tool-result blocks. The translation lives only in
  `app/routing/switchyard/`. An unconvertible message is not an error: the adapter returns
  `None` with `fallback_cause: unconvertible`.
- **Output:** a frozen `SwitchyardProposal` with these fields, and nothing else crosses back:
  - `role`, `tier`, `confidence`
  - allowlisted evidence (B104)
  - `latency_ms`, `version`, `algorithm`

## 5. When it runs, and failure

- **When:** once per request, in `app/pipeline.py`, after compression and before
  `gateway.run` / `gateway.run_stream`, on both paths. The proposal is reused across that
  request's failover attempts and isn't recomputed per attempt.
- **Time budget:** `asyncio.wait_for(..., timeout_ms/1000)`, default 20 ms.
- **Fallback.** On any failure the target is exactly today's `complexity_tier` and the
  request continues: timeout, exception, a role outside `{efficient, capable}`, an
  unconvertible message, or a routing-time call requested. The cause is recorded. **The
  advisor never rejects a request and never raises into the request path.**
- **Fallback ownership:** RouteForge's failover, breaker and budget logic are unchanged.
  Switchyard's own fallback list and context-window fallback are ignored.

## 6. Modes and who may turn them on

| Layer | Setting | Meaning |
|---|---|---|
| Deployment ceiling | `routing.yaml` `switchyard.mode: off \| shadow \| enforce` | The most any app may get. `off` means the code path never runs. |
| Per app (B108) | `adaptive_routing: off \| shadow \| enforce`, `sample_rate` | Raise-only *toward* the ceiling: an app can't exceed it. The default is `off`. |

- **shadow (B105):** compute and record the proposal, and don't pass `advisor_tier`. The
  served model, response, metering and cost are identical to `off` (the identity test).
  The advisor record is **operator-visible** (trace, Langfuse, `routing_outcomes`, the
  console) and **never client-visible**: the client response body is identical to `off`.
- **enforce (B108):** pass `advisor_tier`. The proposal is named on the response
  certificate and folded into `app_policy_version`.
- **Not an axis (rule 2a).** It isn't placement and it isn't protection: it is a routing
  preference inside the bounds placement and policy already set. It must never read or write
  a placement or protection value.

## 7. Streaming and step 9

The advisor changes which model answers, never how the answer is verified, so the step-9
floor is untouched. `onprem_only` and `require_verification` requests are verified exactly
as today.

## 8. State and sessions

0.4.0a1's Python `run_stream` has no caller-owned `State` parameter: holds live inside the
algorithm instance. Holds keyed by an unscoped session would leak routing state across
tenants, and they don't survive restarts or replicas. So:
- **B103/B105 run stateless.** The adapter passes no session or correlation headers, so
  stage holds never engage, and each request is advised on its own message history. Agent
  loops send the whole history every call, so most stage signals are still present.
- **Client headers are never forwarded.** Switchyard reads session ids from several headers
  and body paths: `x-switchyard-session-id`, `x-claude-code-session-id`, `x-dynamo-session-id`,
  Codex turn metadata, and others. The adapter builds the Switchyard request from scratch,
  with messages only and `headers=None`. A client therefore can't steer advisor state by
  sending one of those headers. A test pins this.
- **B106 is blocked on upstream** exposing caller-owned `State` in the Python binding (it
  exists in Rust on `main`). File that as an upstream request instead of working around it.
  This is the separation rule in practice.

## 9. Telemetry (B104, summary)

- Trace attributes are `sp.switchyard.{mode, role, tier, confidence, agreed, fallback_cause,
  latency_ms, version}`. Here `agreed` means the proposal tier equals the served tier.
- The same columns go on `routing_outcomes`.
- Langfuse tags: `switchyard`, `switchyard:{shadow|enforce}` and
  `switchyard:{agreed|overridden|fallback}`.
- Evidence uses a name+type allowlist. Switchyard logging is held at WARN and above, with no
  global OTel provider capture.

## 10. Version pin (D2, decided: exact 0.4.0a1)

PyPI has only `0.4.0a1` for 0.4. The `v0.4.0-rc.2` tag exists on GitHub but isn't published.
The backlog said to "merge only on a 0.4.x final".

**Decided (owner, 2026-11-12):** build and merge B103–B105 against the exact pin `0.4.0a1`. This is safe
because everything is default-off, the extra is optional, and the import guard keeps the
blast radius to one package. Bump to `0.4.0` final in one PR when it is published. **Turning
on `enforce` for any real app (B108) waits for a final release.**

## 11. Out of scope here

- Classifier, judge, advisor-gate and VGR calls (B111).
- Persisting state (B106).
- Per-app config and the rollout UI (B108).
- Evaluation (B109).
- `switchyard-server` and the LiteLLM plugin (never).
