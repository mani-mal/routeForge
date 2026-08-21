# Per-application configuration flexibility

**Status:** implemented (2026-08-20) — both decisions below shipped on
`feat/per-app-flexibility` (see `docs/superpowers/plans/2026-08-20-per-app-flexibility-plan.md`
for the task-by-task build). See `docs/per-app-config.md` for the operator how-to
(field values, floors, manual `ALTER` for already-deployed DBs).
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
   to mask, so an app with `capture_io="redacted"` (or an `onprem_only` tenant) would write
   **raw** prompts to Langfuse *believing* they are redacted — `_set_io`'s `redacted` branch
   reuses the gate's `sanitized_prompt`, which is raw when detection is off. Reject/clamp
   `off` (and `regex`) whenever the app is `onprem_only` or its effective `capture_io` is
   `"redacted"`. `capture_io="off"` is always safe with detection off.

   **Deliberate, narrower exception — operator-gated raw eval capture (2026-08-21,
   product-owner approved, relaxes CLAUDE.md rule 2):** `capture_io="full"` is *not*
   clamped away by the rule above. A **non-`onprem_only`** app may combine
   `pii_detection=off` with `capture_io="full"`: `app/obs/export.py`'s `full` branch sends
   RAW (unredacted) I/O, routed via a per-app tracer to the app's **own** provisioned
   Langfuse project — never the shared/default one — and fails closed to `redacted` if that
   project is missing or the tracer build fails. This is safe under the same governing rule
   as everything else here: residency is *still* enforced by an independent mechanism
   (`onprem_only` apps cannot reach `pii_detection=off` in the first place — the residency
   clamp above fires first, so the two floors compose instead of one undermining the other),
   and the raw-capture blast radius is contained to one app's own Langfuse project by the
   existing `capture_io='full'` eligibility clamp (`load_app_langfuse(app) is not None`).
   `allow_app_pii_downgrade` gates this combination exactly like any other PII downgrade —
   an operator who sets that ceiling `false` cannot reach raw capture either.
3. **Fail closed on validation.** An unknown/invalid mode at admin-write time → 422. Never
   silently fall back to `off`.
4. **Deployment ceiling.** A global switch — shipped as `policy.allow_app_pii_downgrade`
   in `config/serving.yaml` (default `true`, env override `SP_ALLOW_APP_PII_DOWNGRADE`) —
   so a locked-down deployment (hospital) sets it `false` to forbid apps from weakening
   detection at all, while a MillWise-owned deployment leaves it `true`.
5. **Invariant test.** Assert an `onprem_only` app with `pii_detection: off` still receives
   `onprem_only` residency — pin the floor in a test.

**Shape:** new `AppPolicy.pii_detection` column, admin API validation + clamp, singleton
selection at request build, invariant test. Follow the `capture_io` override end-to-end as
the template.

**Shipped as:** `AppPolicy.pii_detection` (`app/apps/models.py`, `String(16)`, one of
`app/policy/enums.py:PII_MODES`); write-time 422 clamp in `app/api/admin_routes.py:set_policy`
for `onprem_only` residency or `capture_io == "redacted"`, plus the `allow_app_pii_downgrade`
ceiling; singleton per-mode detector memoized in `Pipeline._detector_for`
(`app/pipeline.py`) so an app-selected mode never rebuilds Presidio per request; invariant
test pins that an `onprem_only` app with `pii_detection: off` still gets `onprem_only`
residency and unredacted-but-on-prem-only routing.

**Task 9 addendum (2026-08-21):** two follow-ups landed together, because the first made
the second's guardrails trustworthy:

- **Fixed a Critical:** `PUT /admin/applications/{app_id}/policy` used full-replace
  semantics (`body.model_dump()` with no `exclude_none`), so a *partial* PUT (e.g. just
  `{"pii_detection": "off"}`) wiped every omitted field — including `capture_io` and
  `residency` — to `None`, which then inherited global/default values at runtime. The
  write-time clamps read the *stored* row before the write, so they could pass against a
  value the write was about to blank. Fixed at the call site
  (`body.model_dump(exclude_none=True)`, `store.upsert_policy` itself untouched) plus
  restructuring both the pii-downgrade clamp and the `capture_io=='full'` clamp to key on
  one resolved *effective* post-write residency/`capture_io` pair, computed once, so a
  partial PUT is judged on the state it will actually produce.
- **Shipped the operator-gated raw eval capture described above** (`pii_detection=off` +
  `capture_io=full` for a non-`onprem_only` app with its own Langfuse project), which
  depends on that same effective-value fix — the `capture_io=='full'` clamp now
  re-validates on every write instead of trusting a stored value that a partial PUT might
  otherwise have left stale.

`app/obs/*`, the streaming code, and `store.upsert_policy` were not modified by this
follow-up — the exporter's `full`/fail-closed-to-`redacted` behavior was already correct.

**Fix round 1 (2026-08-21, same day):** review found the pii-downgrade/redacted-seam
clamp still keyed on `body.pii_detection` alone — asymmetric with the `eff_residency`/
`eff_capture` values used everywhere else, and reachable: `PUT {"pii_detection":"off"}`
then a later `PUT {"capture_io":"redacted"}` (which never mentions `pii_detection`)
skipped the whole downgrade branch, silently landing the app at `pii=off` +
`capture_io=redacted` — the exact sanitized-prompt leak the clamp exists to prevent, with
no operator opt-in and no runtime backstop. Fixed by resolving `eff_pii` the same way
and keying the clamp on it: `eff_pii = body.pii_detection if body.pii_detection is not
None else (stored.pii_detection if stored else None)` — **deliberately with no further
fallback to the global default**, unlike `eff_residency`/`eff_capture`. Per this doc and
`docs/per-app-config.md`, unset/`None` means "inherit the deployment's live global
detector" and is explicitly *not itself a downgrade* — only an actual per-app override
(this write's body, or a value already stored on this app's row) is what the clamp
guards. Falling back further to `settings.serving.pii_detection` was tried and reverted:
it made the clamp misfire for every app whenever the deployment's own global default is
non-`presidio` (this repo's own test suite runs with `SP_PII_DETECTION=regex` for
package-free CI, which made *every* app "effectively regex" and broke the pre-existing,
correct `test_policy_full_capture_rejected_for_ineligible_app` invariant that `redacted`
is always allowed for an app with no PII override). A non-`presidio` *global* default
combined with `capture_io=redacted` deployment-wide is a boot-time config-lint concern
(CLAUDE.md's cross-file lint), not this per-app write clamp's job.

---

## Decision 2 — per-app streaming: the capability is desirable; the current flag is not the way ❌ → ✅

**Decision:** do **not** expose `reject_stream_requests` as a per-application boolean.
Instead, implement a per-app streaming **mode** gated on the
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
| **`incremental_verify`** | Verify chunks before release using a hold-back/sliding buffer (catches entities split across chunk boundaries) | Approximate, preserved | Low | Apps that need *both* low TTFT and verification |

Notes that make this honest:

- **`verified_then_stream`** is the safe default and the right first build: OpenAI-compatible
  wire format, guarantee untouched. Its only cost is time-to-first-token — which is a
  non-issue for long generations and a dealbreaker for latency-motivated chat. It gives
  "streaming-shaped output," not "streaming latency."
- **`stream_unverified`** must be **explicit and floored** (free-but-floored, per the
  governing rule): an app cannot combine it with `onprem_only` or `require_verification`.
  Choosing it is choosing to give up the verification differentiator for that app's traffic —
  a declared trade, never a silent side effect.
- **`incremental_verify`** was the genuinely hard case: partial-text leakage detection is
  unreliable in general (an SSN split `123-45-` / `6789` matches on neither chunk alone).
  Shipped as `StreamingVerifier` (`app/output/streaming_verify.py`): a `HOLDBACK_CHARS=48`
  window plus a trailing-entity-run guard, so raw entity text is never released across a
  chunk boundary. **Documented limitation:** this bounds *per-entity* leakage only — it
  cannot enforce a *holistic* whole-response verdict (the `UNVERIFIABLE` marker
  `app/output/verify.py` fails closed on) once tokens have already streamed to the client.
  `incremental_verify` is therefore a deliberately **weaker** guarantee than
  `verified_then_stream`, and the trace records it with `reason="incremental"` so the
  console/Langfuse show the difference, not just a bare "verified: true".

### Decision — shipped

All three modes are built. `AppPolicy.stream_mode`
(`off | verified_then_stream | stream_unverified | incremental_verify`, canonical tuple
`app/policy/enums.py:STREAM_MODES`). Streaming becomes a safe per-app knob for the same
reason `pii_detection` is — an independent mechanism (the mode + the floor below) holds
the guarantee, not the app's own choice:

- **`verified_then_stream`** is post-hoc: the pipeline runs the full request exactly as
  the non-stream path does (including step 9), then `app/api/streaming.py:chunk_text`
  emits the already-verified text as OpenAI-compatible SSE. No real backend streaming.
- **`stream_unverified`** and **`incremental_verify`** are the opposite: REAL backend
  token streaming (`litellm` `stream=True`, `app/gateway/backends/litellm_backend.py:
  generate_stream` → `app/gateway/gateway.py:run_stream` → `app/pipeline.py:run_stream`).
  Neither buffers the whole answer first — `stream_unverified` forwards deltas as they
  arrive (verification recorded as waived); `incremental_verify` pipes every delta
  through `StreamingVerifier.feed()` before emitting.
- **The fail-closed floor lives in one place:** `app/pipeline.py:run_stream` raises
  `StreamFallback` — caught by `app/api/routes.py` — whenever the request routes
  `multi_step`, or `ctx.onprem_only`/`ctx.require_verification` is set. A floored request
  always falls back to `verified_then_stream`; it is never streamed unverified or
  incrementally. `stream_unverified` additionally gets an admin write-time 422 for
  `onprem_only` apps (`app/api/admin_routes.py:set_policy`), so the unsafe combination is
  rejected before it can even be configured, not just caught at runtime.

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
| `pii_detection` | free-but-floored (clamp for `onprem_only`/capture) | ✅ shipped (Decision 1) |
| `stream_mode` | free-but-floored (`StreamFallback` + write-time clamp hold the floor, not the app's choice) | ✅ shipped (Decision 2) |
| `fail_closed` | the floor itself | never per-app |

**Rule of thumb for any future knob:** if it is monotonic-tightening or free-but-floored,
it can be per-app; if it is the floor itself, it stays global. Classify before exposing.

---

## Status / next steps

Both decisions are **implemented** (`feat/per-app-flexibility`, 2026-08-20).

- **Decision 1 (`pii_detection` per-app):** ✅ shipped. `AppPolicy.pii_detection` column +
  admin write-time 422 clamp (`onprem_only` residency, `capture_io != "off"`) + the
  `allow_app_pii_downgrade` deployment ceiling + singleton per-mode detector selection +
  the invariant test pinning the residency floor. See `docs/per-app-config.md`.
- **Decision 2 (streaming):** ✅ shipped, all three `stream_mode` values.
  `verified_then_stream` chunks a finished, fully-verified answer — no real backend
  streaming, no TTFT win. `stream_unverified` and `incremental_verify` are REAL backend
  token streaming; they do NOT buffer-then-chunk. The fail-closed floor
  (`app/pipeline.py:run_stream` → `StreamFallback`) is enforced independently of the
  app's chosen mode, at both admin write-time and runtime — see the "Decision — shipped"
  section above and `docs/per-app-config.md` for the operator-facing summary.
- **Open follow-up:** none tracked for this workstream. A future holistic
  streaming-verification scheme (rejecting an in-flight stream on a whole-response
  verdict, not just per-entity redaction) remains explicitly out of scope — see the
  `incremental_verify` limitation above.
</content>
</invoke>
