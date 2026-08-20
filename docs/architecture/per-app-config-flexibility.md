# Per-application configuration flexibility

**Status:** decision (2026-08-20)
**Related:** per-app privacy rules (PR #3, `app/apps/models.py`); MillWise integration
assessment; CLAUDE.md §Configuration; `.claude/skills/privacy-gate`, `.claude/skills/output-controls`

This note answers: *which Serving-Plane settings can safely become per-application
overrides, and why `pii_detection` qualifies but a raw per-app `reject_stream_requests`
toggle does not — including, from first principles, what it would actually take to give
an application streaming.*

Motivating goal: make RouteForge flexible enough at the **per-application** level to host
many kinds of AI application (dense-document generators like MillWise, low-latency chat,
agentic tools) on one deployment — without letting one app's convenience silently defeat
another's guarantee.

---

## The governing rule: expose a per-app override only when an *independent* mechanism holds the floor

RouteForge's hard guarantees (residency, fail-closed, output verification) are **floors**.
A per-application override is safe to expose when the floor it might threaten is enforced
by a **different** mechanism — so no value an app sets can lower that floor. It is unsafe
when the knob *is* the only thing holding the floor.

Two shapes are safe; a third is not:

- **Monotonic-tightening.** The app can only make itself *more* restrictive; a deployment
  floor caps how permissive it can get. This is exactly how residency already works:
  `_stricter(rule.residency, app.residency)` in `app/policy/context.py` — an app can force
  `onprem_only` but can never widen below the tenant floor.
- **Free-but-floored.** The app may set a *looser* value, but a separate clamp/lint enforces
  the deployment ceiling and guards cross-knob interactions. Precedent already in the code:
  `capture_io` and `compression_enabled` are `serving.yaml` infra switches that apps override
  per-app (`AppPolicy.capture_io`, `.compression_enabled`), and `capture_io='full'` is
  *clamped* for `onprem_only` tenants (admin write-time lint).
- **NOT safe: the knob is the floor.** If flipping the setting is itself the breach, with no
  independent mechanism to catch it, it must stay global (or tighten-only). A naked
  `reject_stream_requests` per-app flag is this case — see Decision 2.

Every candidate per-app knob must be classified into one of these before it is exposed.

---

## Decision 1 — `pii_detection` becomes a per-application override ✅

**Decision:** allow each application to select its base PII-detection mode
(`presidio | regex | off`) instead of inheriting only the single global
`serving.yaml` value. Global stays the default; per-app overrides it.

**Why it is safe.** The obvious fear — "an app sets `off` and PII leaks to cloud" —
**cannot happen**, because detection and residency are enforced by *separate* mechanisms.
Detection drives *classification*; residency is enforced by `_stricter(rule.residency,
app.residency)` regardless of what (or whether) entities were detected. An `onprem_only`
app with detection `off` classifies as `public` but still routes on-prem. The privacy
floor survives detection being turned off. That independence is precisely what makes this
a *free-but-floored* knob rather than a floor-defining one.

**Why it is wanted.** The gate redacts **every** detected entity into placeholders before
the model sees the text (`app/policy/gate.py:34-35`). For document-generation apps
(MillWise setup sheets: customer names, S.O. numbers, `REV A: … 4/5/21 TR` revision lines)
that is destructive — the generator must see the real values. The shipped per-app privacy
rules only remap *classification*, not *what is masked*; there is no per-app way to exclude
built-in entity types (`PresidioDetector.analyze()` runs Presidio with no `entities=`
allow-list, and `EffectiveDetector` is add-only). A per-app base-mode selector is the
missing lever: such an app picks `off` (or a scoped `regex`) and keeps its content intact.

**Guardrails (all have precedent, all cheap):**

1. **Singleton detectors.** Instantiate the three detectors once (`presidio` / `regex` /
   `null`); per-app selection just *picks* one. Never build a spaCy-loaded Presidio engine
   per application.
2. **Cross-knob clamp — the real trap.** With detection `off`, egress redaction has nothing
   to mask, so an app with `capture_io != off` (or an `onprem_only` tenant) would write
   **raw** prompts to Langfuse believing they are redacted. Reject/clamp `off` (and likely
   `regex`) whenever the app is `onprem_only` or has capture enabled — same class of lint as
   the existing `capture_io='full'` clamp.
3. **Fail closed on validation.** An unknown/invalid mode at admin-write time → 422. Never
   silently fall back to `off`.
4. **Deployment ceiling.** A global switch (e.g. `allow_app_pii_downgrade`, or a per-tenant
   floor) so a locked-down deployment (hospital) can forbid apps from weakening detection at
   all, while a MillWise-owned deployment permits it.
5. **Invariant test.** Assert an `onprem_only` app with `pii_detection: off` still receives
   `onprem_only` residency — pin the floor in a test.

**Shape:** new `AppPolicy.pii_detection` column, admin API validation + clamp, singleton
selection at request build, invariant test. Follow the `capture_io` override end-to-end as
the template.

---

## Decision 2 — per-app streaming: the capability is desirable; the current flag is not the way ❌ → 🔜

**Decision:** do **not** expose `reject_stream_requests` as a per-application boolean now.
Instead, when streaming is prioritized, implement a per-app streaming **mode** gated on the
app's verification needs. The reason is mechanical, not a rule appeal.

### Why streaming fights output verification (first principles)

Step 9 is a **terminal gate on the finished output**: it scans the *complete* response for
leaked PII/secrets and can reject or rewrite it **before any of it reaches the client**.
Token streaming ships tokens to the client *as they are generated*. So the instant you
stream, the leakage check on token *N* runs only after tokens 1…*N*−1 have already left —
if token 5 was a leaked SSN, it is already at the client and cannot be recalled. Streaming
and "verify the whole artifact before releasing it" are incompatible by construction: you
cannot check a thing you have already partially handed over. This is ordering, not policy.

Unlike Decision 1, **no independent mechanism holds this floor** — the floor is step-9
whole-response verification, and streaming *is* the thing that breaches it. That is what
puts a naked toggle in the "knob is the floor" (unsafe) bucket.

### Why the current flag specifically is a footgun

Flipping `reject_stream_requests: false` today does **none** of the work below — the handler
still builds and returns a single complete JSON body (`app/api/routes.py`). A `stream: true`
client receives one non-streamed blob and breaks. The flag would advertise a capability the
engine does not implement.

### What "an app wants streaming" actually resolves to

Three legitimate implementations; which is right depends on whether the app needs output
verification:

| Mode | How | Guarantee | TTFT | Fits |
|---|---|---|---|---|
| **`verified_then_stream`** | Generate full response server-side, run step 9 on the complete text unchanged, *then* emit SSE chunks to the client | **Fully intact** | None (waits for full generation) | Dense-doc generators (MillWise), any app that wants OpenAI-compatible streaming wire format but not low latency |
| **`stream_unverified`** | True passthrough streaming from the backend | **Waived** for this app's traffic | Low | Non-sensitive apps (coding helper on internal data, public chatbot, no PII) |
| **`incremental_verify`** | Verify chunks before release using a hold-back/sliding buffer (catches entities split across chunk boundaries) | Approximate, preserved | Low | Future work — apps that need *both* low TTFT and verification |

Notes that make this honest:

- **`verified_then_stream`** is the safe default and the right first build: OpenAI-compatible
  wire format, guarantee untouched. Its only cost is time-to-first-token — which is a
  non-issue for long generations and a dealbreaker for latency-motivated chat. It gives
  "streaming-shaped output," not "streaming latency."
- **`stream_unverified`** must be **explicit and floored** (free-but-floored, per the
  governing rule): an app cannot combine it with `onprem_only` or `require_verification`.
  Choosing it is choosing to give up the verification differentiator for that app's traffic —
  a declared trade, never a silent side effect.
- **`incremental_verify`** is the genuinely hard case: partial-text leakage detection is
  unreliable (an SSN split `123-45-` / `6789` matches on neither chunk), some checks are
  inherently whole-response, and Presidio-per-chunk is costly. Real component, not a flag.

### Decision

Keep inbound token streaming rejected globally until a streaming *mode* exists. When
prioritized, add `AppPolicy.stream_mode` (`off | verified_then_stream | stream_unverified`),
build `verified_then_stream` first, gate `stream_unverified` behind the clamp above, and
leave `incremental_verify` as tracked future work. At that point streaming becomes a safe
per-app knob for the same reason `pii_detection` is — an independent mechanism (the mode +
clamp) holds the floor.

The separate **decision stream** (SSE of DecisionRecords, `stream_decisions` / `/api/stream`)
is unaffected by any of this and stays on — it is a different channel and never carries chat
tokens. An application's own progress/phase SSE (e.g. MillWise's `ProgressReporter`) likewise
runs independently of RouteForge's inbound-streaming policy.

---

## Where each knob lands

| Knob | Class | Per-app? |
|---|---|---|
| `residency` | monotonic-tightening (`_stricter`) | already per-app |
| `allowed_tiers` / `allowed_models` / `allowed_providers` | monotonic-tightening | already per-app |
| `capture_io` | free-but-floored (clamp for `onprem_only`) | already per-app |
| `compression_enabled` | free-but-floored | already per-app |
| `pii_detection` | free-but-floored (clamp for `onprem_only`/capture) | **Decision 1 — add** |
| streaming | knob-is-the-floor until a *mode* exists | **Decision 2 — via `stream_mode`, not a raw flag** |
| `fail_closed` | the floor itself | never per-app |

**Rule of thumb for any future knob:** if it is monotonic-tightening or free-but-floored,
it can be per-app; if it is the floor itself, it stays global. Classify before exposing.

---

## Status / next steps

- **Decision 1 (`pii_detection` per-app):** approved; ready to implement following the
  `capture_io` override as template (column + admin validation + clamp + singleton selection
  + invariant test).
- **Decision 2 (streaming):** inbound token streaming stays globally rejected for now; the
  path to per-app streaming is `stream_mode` with `verified_then_stream` built first, not a
  per-app `reject_stream_requests` boolean.
</content>
</invoke>
