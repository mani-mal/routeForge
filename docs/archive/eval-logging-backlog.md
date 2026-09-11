# Backlog: capture full request/response I/O in Langfuse for eval

**Status:** BUILT 2026-07-29 (`capture_io: off|redacted|full`). Both sub-items below
are now done. Logged 2026-07-28; the raw-leak gap (item 2) was closed by E1 and the
eval-capture switch (item 1) landed in the `:0.4.0` batch (uncommitted at time of
writing). See "What was built" at the bottom.

**Decision (owner):** we want Langfuse to store the **complete** input and output of
every request — raw prompt, tool inputs/outputs, final completion — so the traces are
usable as an **evaluation dataset** (regression sets, quality/routing eval, replay).
Today most of that is deliberately withheld. This documents the current behaviour, why
it's withheld, and how to turn it on safely later.

## Current behaviour (why the Langfuse "Input" column is mostly blank)

Two export paths, two behaviours — see the trace list where `serving.request` rows show
no Input but `<workflow>` rows do:

| Trace name | Built by | Input/Output captured? |
|---|---|---|
| `serving.request` (single/direct requests) | `app/obs/export.py` (hand-built spans) | **No.** Sets only redacted attributes: `sp.request_id`, `sp.tenant_id`, `sp.model`, `sp.cost_*`, `sp.sensitivity`, `sp.detected_entities` (entity **types** only). Never sets the OTel `input`/`output` attribute Langfuse reads. |
| `<workflow>` (multi-step / agent) | NeMo Agent Toolkit's own `LangfuseTelemetryExporter` (`app/agent/runtime.py → enable_nat_telemetry`) | **Yes, raw.** NAT auto-captures the workflow function input (the prompt) + per-hop tool I/O. This path does **not** pass through `app/obs/redact.py`, so it emits raw text. |

So the blank Input on single requests is **by design** (CLAUDE.md rule #2, "telemetry is
redacted on egress"). It is NOT a traffic-generator bug and is NOT changeable via the
request/curl body — it's server-side instrumentation.

## Two things to fix when we build this

1. ~~**Single-request path emits no I/O at all.**~~ **BUILT (2026-07-29).** The
   `serving.request` root span now attaches `input.value`/`output.value` when
   `capture_io` is `redacted` or `full` (both direct and multi-step, since both flow
   through `app/obs/export.py`). `off` keeps today's blank-Input behaviour. See
   "What was built" below.
2. ~~**Multi-step path already emits RAW I/O, unredacted.**~~ **FIXED (2026-07-29).** The
   NAT-native `LangfuseTelemetryExporter` (which bypassed our egress redaction and per-app
   routing) is no longer wired — see `app/agent/runtime.py` `_configure_nat_telemetry`
   and the guard `tests/test_agent_telemetry_redaction.py`. Multi-step observability now
   comes solely from our own redacted, per-app, per-hop `serving.request` trace
   (`app/obs/export.py`), same as direct requests. NOTE this **removed the raw firehose**,
   which is correct for rule #2 — but it is NOT the eval-capture feature; item 1 (attach
   *redacted* prompt/completion I/O deliberately) is still the work to do below.

   Ground truth verified against the live nvidia-nat install (2026-07-29): NAT builds its
   own OTLP export pipeline from `config.general.telemetry.tracing`, separate from our
   global tracer provider; its `OTLPSpanHeaderRedactionAdapterExporter` is not wired to any
   `@register_telemetry_exporter` config type, so it can't be selected via config — which
   is why the fix is "don't use NAT's exporter" rather than "make NAT's exporter redact".

## The tension to resolve (don't just flip it on)

Storing full I/O collides head-on with the demo's central privacy claim and hard rules:

- **Rule #2 — redact on egress.** Full-I/O capture is the opposite of this.
- **Rule #6 — CCR/residency.** An eval store holding raw prompts is another copy of
  sensitive data; it must stay **inside the residency boundary** (a residency-local,
  access-controlled Langfuse), never a shared/cloud project, with a retention TTL.
- **Scope.** The Improvement Plane (eval gates, training, promotion) is explicitly
  **out of scope** for this prototype (CLAUDE.md). Capturing the *data* is fine and
  cheap; building eval *pipelines* on top is the out-of-scope part — keep them separate.

## Proposed design (for whoever builds it)

- **A single switch, default OFF (redacted).** e.g. `observability.langfuse.capture_io`
  in `serving.yaml` — owned there (infra/observability concern). OFF = today's redacted
  behaviour (the demo default, keeps the privacy story intact). ON = full I/O for eval.
- **Make BOTH paths obey it.**
  - `serving.request`: when ON, set `input.value`/`output.value` on the root span. When
    OFF, set nothing (as today). Consider a middle mode: `redacted` = run through
    `redact()` so entity **values** are masked (`"My SSN is <US_SSN>"`) but the trace is
    still eval-usable for routing/structure — a good default for a shared project.
  - `<workflow>` / NAT: when OFF or `redacted`, insert a redaction span-processor before
    the Langfuse exporter (or post-process) so NAT stops emitting raw text. **This is the
    gap to close regardless of the eval feature** — the multi-step path should not leak
    raw PII today.
- **Guardrail:** if `capture_io: full`, require the target Langfuse to be residency-local
  and refuse to enable it against a shared/cloud project for an `onprem_only` tenant
  (fail-closed, consistent with rule #3/#6). Log a clear boot warning that raw I/O
  capture is on.
- **Three-state, not boolean, is cleaner:** `off | redacted | full`. `redacted` is the
  sweet spot for most eval (structure + types, no raw PII); `full` only for a locked-down
  in-boundary project.

## Where to change it (pointers)

- `app/obs/export.py` — single-request spans; add I/O attributes here.
- `app/agent/runtime.py` (`enable_nat_telemetry`) — NAT exporter wiring; add the
  redaction processor / gate here.
- `app/obs/redact.py` — reuse `redact()` for the `redacted` mode (already maps values to
  `<TYPE>`).
- `config/serving.yaml` — new `observability.langfuse.capture_io: off|redacted|full`.
- Config lint (`app/config/lint.py`) — reject `full` for an `onprem_only`/shared-project
  combination.

## What was built (2026-07-29)

`observability.langfuse.capture_io: off | redacted | full`, owned in `serving.yaml`
(quoted — bare `off` is a YAML boolean), env-overridable via `SP_CAPTURE_IO`.

- **`off` (default):** unchanged — no I/O attributes; the demo's privacy story is intact.
- **`redacted`:** `app/obs/export.py:_set_io` runs the prompt/completion through the same
  egress `redact()` (values → `<TYPE>`) before `set_attribute`, and the
  `RedactingSpanExporter` re-redacts the same keys on egress (idempotent, defence in
  depth). Eval-usable structure, zero raw PII. Safe for a shared project.
- **`full`:** authored raw. `app/obs/otel.py` sets a module flag `_capture_io_full` in
  `init_tracing`; when set, the exporter skips redacting *only* `input.value`/`output.value`
  (every other key still redacts). If the flag is somehow not armed, the exporter still
  scrubs the I/O keys — full is unsafe only when deliberately enabled.
- **Guardrail (fail-closed):** the config lint (`app/config/lint.py`) rejects `full`
  whenever ANY tenant is `onprem_only` (the demo's `hospital` is — so `full` is a boot
  failure in the default config), because raw PHI would land in the single shared
  Langfuse and leave the residency boundary (rule 6). `redacted` is never blocked. A boot
  warning fires when `full` is armed with no onprem tenant.
- **Both paths obey it for free:** direct and multi-step both export via
  `app/obs/export.py`, so no separate NAT wiring was needed (E1 already removed NAT's own
  raw exporter). The "add a redaction processor to NAT" pointer above is therefore moot.
- **Tests:** `tests/test_capture_io.py` (11) — loader default/override, lint enum +
  onprem guardrail (reject `full`, allow `redacted`, allow `full` when no onprem tenant),
  export off/redacted/full attribute behaviour, exporter flag on/off, and an end-to-end
  redacted request asserting `input.value` present but PII masked.
