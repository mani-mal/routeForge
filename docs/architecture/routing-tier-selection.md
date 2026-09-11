# How the step-6 router actually selects a tier and model — and what it ignores

**Audience:** anyone reasoning about *why* a given request landed on cheap vs.
frontier, or scoping a customer use case against what the router can and can't key on.

**One sentence:** RouteForge's step-6 router selects a **tier** from
`max(length-based complexity, risk floor, escalation floor)` clamped to the best
residency-permitted tier, then scores **model choice within that tier** on
cost/quality/latency/capacity — and it keys on **no other request dimension**: not
semantic difficulty, not modality, not model capability.

Everything below is descriptive of the code as it exists (`app/pipeline.py`,
`app/routing/router.py`, `app/routing/task_mode.py`, `config/routing.yaml`), not a
proposal.

---

## Part 1 — how a simple question ("What is the capital of the US") gets the cheap tier

The intuitive story is "the router recognized an easy question." **That is not what
happens.** There is no semantic difficulty classifier. Tier is chosen by a much blunter
signal: **prompt length in tokens.**

The path, step by step:

| Step | Module | What happens for "What is the capital of the US" |
|---|---|---|
| 2 · Gate | `app/policy/gate.py` | No PII → `sensitivity=public`, `risk_class=low`, `residency=any` |
| 4 · Task mode | `app/routing/task_mode.py` | Rules-only, classifier OFF. No trigger phrase (`"step by step"`, `"look up"`) → `direct` |
| 5 · Compression | `app/compression/` | Short prompt; a handful of tokens |
| — · Complexity | `app/pipeline.py:531` | `complexity = min(1.0, comp.tokens_after / 2000)` → ~7 tokens → **≈ 0.003** |
| 6 · Tier from complexity | `app/routing/router.py:45-46` | `0.003 ≤ cheap_max (0.35)` → **cheap** |
| 6 · Risk floor | `config/routing.yaml:62` | `low → cheap`. Target = `max(cheap, cheap)` = **cheap** |
| 6 · Residency clamp | `router.py:119-121` | `any` — nothing dropped, target stays cheap |
| 6 · Within-tier score | `router.py:150` | Among cheap candidates: `quality·1.0 − cost·0.6 − latency·0.3 + capacity·0.2` → cheapest/best cheap model wins (an onboarded on-prem model usually wins on cost). Ranking is now delegated to the pluggable strategy (`ordered = self._strategy.rank(...)`, `app/routing/strategy.py`); the default strategy reproduces this native formula |

### The load-bearing line

```python
# app/pipeline.py:531  (direct path; also :748 in run_stream())
complexity = min(1.0, comp.tokens_after / 2000)
# app/agent/internal_endpoint.py:153  (per hop, multi-step path)
complexity = min(1.0, tokens_after / 2000)
```

**Complexity is post-compression token count / 2000, clamped to 1.0.** That's it. The
thresholds it feeds (`config/routing.yaml`):

```yaml
complexity_thresholds:
  cheap_max: 0.35     # ≤ 700 tokens  → cheap
  mid_max:   0.70     # ≤ 1400 tokens → mid
  # > 1400 tokens     → frontier
```

### Why this is right often but wrong at the tails

Length is a *proxy* for difficulty. It correlates with real workload in typical traffic,
which is why the "~70% to cheap" demo story holds. But it inverts at the extremes:

- **Long but trivial** — paste 3,000 tokens of text then "reply OK" → complexity 1.0 →
  **frontier**, for a one-word answer.
- **Short but hard** — "Prove the Riemann hypothesis" → ~6 tokens → complexity ≈ 0.003 →
  **cheap**, for a genuinely frontier task.

### The backstop: confidence-driven escalation

The short-but-hard case is (partly) caught *after the fact* by escalation, not by better
up-front tier selection (`config/routing.yaml:40,48-49`):

```yaml
confidence_escalation_threshold: 0.60
confidence_logprob_temperature: 4.0   # calibrates a real backend's mean-token-logprob → confidence
max_escalations: 1
```

After a cheap-tier call, if the backend-reported confidence (derived from mean token
logprob, tempered by `T=4`) falls below `0.60`, the gateway escalates **one** tier and
re-runs. `Gateway.run` appends one `step="router"` DecisionRecord **per attempt**, so an
escalated response carries >1 routing record for a single answer. Escalation always moves
**within `allowed_providers`** — an `onprem_only` request escalates cheap→mid→frontier
*on-prem* and is never lifted to cloud (CLAUDE.md hard rule 8).

> The `mock` backend used in tests ignores the logprob math; its confidence is seeded
> per persona. See `routeforge-router-targets-mid` for why `T=4` (raw `T=1` geometric-mean
> probability sits ~0.4–0.6 even for fluent answers and would escalate almost everything,
> collapsing the "~70% cheap" story).

### Precedence, exactly

The chosen tier is `max` over three floors, then clamped down to what residency permits:

```
target = max( complexity_tier, risk_floors[risk_class], escalation_min_tier )
target = min( target, best_residency_permitted_tier )   # residency beats every floor
```

Residency runs **first** and wins: a high-risk `onprem_only` request floors to the best
*allowed* (on-prem) tier and sets `require_verification` — it is never promoted to a cloud
tier to satisfy the risk floor.

---

## Part 2 — what the router does NOT key on: modality and model capability

RouteForge is a **text/chat-only router**. There is **no modality or capability axis
anywhere** in the request schema, the model registry, or the scorer. Every candidate is
assumed to be an interchangeable OpenAI-compatible **chat** endpoint that differs only by
tier / cost / provider / residency / quality.

### Evidence in the code

1. **One endpoint.** `app/api/routes.py:115` exposes `POST /v1/chat/completions` (plus
   `/health`, `/metrics`, `/v1/feedback`, and UI routes — none of them OpenAI media
   endpoints). No `/v1/images/generations`, `/v1/embeddings`, or `/v1/audio/*`. An
   image-generation call 404s.

2. **The request schema can't carry an image.** `app/models.py:9`:
   ```python
   class Message(BaseModel):
       role: str
       content: str | None = None   # a plain string (or None on a tool-only turn) —
                                     # NOT OpenAI's content-parts array
       tool_calls: list[dict] | None = None
       tool_call_id: str | None = None
       name: str | None = None
   ```
   `content` was widened from a strict `str` to `str | None` for the OpenAI tool-calling
   round-trip (an assistant turn that only requested tools carries `content: null`) — but
   it is still never a content-parts array. OpenAI vision *input* uses `content:
   [{"type":"text",…},{"type":"image_url",…}]`; that shape is neither `str` nor `None`, so
   a vision request still fails validation (422) before it reaches the router. The handler
   confirms the text assumption, now guarding the nullable field: `prompt =
   (body.messages[-1].content or "") if body.messages else ""` (`app/api/routes.py:322`).

3. **Onboarded models carry no capability field.** `OnboardIn` (`app/api/admin_routes.py`)
   and `_OnboardedRow` (`app/apps/registry.py:13`) have: `slug, display_name,
   tier(cheap|mid|frontier), endpoint, api_key_env, price_input, price_output, quality,
   context_window, allowed_app_ids, enabled`. **No `modality`, no `capabilities`, no
   `vision`.** The router's `PoolCandidate` only knows `name / provider / tier /
   display_name / quality`, so it structurally *cannot* filter "give me a vision-capable
   model."

### Customer-use-case matrix (today)

| Use case | OpenAI shape | RouteForge today |
|---|---|---|
| **Text chat** (the whole product) | `/v1/chat/completions`, `content: str` | ✅ Routed by tier/cost/residency |
| **Vision input** ("what's in this X-ray?") | chat with `image_url` content parts | ❌ 422 — `content` must be a string |
| **Image generation** ("draw me…") | `/v1/images/generations` | ❌ 404 — endpoint absent |
| **Embeddings / audio** | `/v1/embeddings`, `/v1/audio/*` | ❌ No endpoints |

### Why this is a boundary, not immaturity

The product's value story — cost + privacy routing across a **substitutable** pool
(cheap local vs. frontier cloud, with `onprem_only` residency) — assumes candidates are
interchangeable *for the same request*. Text chat models are substitutable that way; a
text model and an image-generation model are not (you can't downgrade an image request to
a cheap text model to save money). Modality is therefore a **different routing axis** than
the tier/cost/residency axis the product is built around.

**The most natural extension is vision *input on the chat path*** (medical imaging,
document understanding) — same endpoint, same `onprem_only` privacy story — which would
need (a) `Message.content` to accept content-parts and (b) a `capabilities`/`vision` flag
on onboarded models so the router can filter the pool. That's noted as a gap, not a plan;
image *generation* is a further and less aligned step. Neither is in scope until a
customer use case pulls for it.

---

## Relationship to NVIDIA NeMo Switchyard

None of the above uses NVIDIA NeMo Switchyard — the current router is RouteForge's own
native cost/risk scorer. (Our step-6 module carries the internal codename "Switchyard,"
which predates and is unrelated to NVIDIA's project — see
[`nemo-switchyard-comparison.md`](../comparisons/nemo-switchyard-comparison.md) for the name-collision
note.) The one mechanism that *rhymes* with Switchyard is the confidence escalation in
Part 1 — but it is native and **reactive** (fires on low confidence / step-9 failure),
not Switchyard's turn-by-turn *progress-driven* escalation. Of Switchyard's genuinely
distinct ideas, several have since **shipped**: the pluggable strategy family
(`app/routing/strategy.py`), the accuracy-tradeoff axis (the cost dashboard's quality
axis), and Stage Router signals (`app/routing/signals.py`, default OFF). Progress-driven
escalation remains backlog and the Prefill Router stays *rejected* as Improvement-Plane —
all tracked in that doc and in
[`switchyard-features-to-incorporate.md`](../comparisons/switchyard-features-to-incorporate.md).
