# B17 — Client-Workflow Request Grouping (Design Spec)

**Date:** 2026-08-06
**Backlog item:** B17 (`docs/bugs-backlog.md`)
**Status:** Approved for planning

## Problem

A client that runs a multi-call workflow against RouteForge produces N **independent**
top-level `/v1/chat/completions` requests. Today those land as N flat, unrelated rows in
the Requests tab and N unrelated traces in Langfuse — there is nothing tying "the three
calls of one workflow run" together.

Concrete reproduction (the reference sample):
`test_app/routeforge_sample` `SupportInvestigator.investigate(ticket_id)` runs a 3-stage
chain (`_plan` → `_gather_evidence` → `_synthesize`/`_review`); **each stage is one
`self.model.invoke(...)` = one RouteForge call.** One investigation ⇒ 3 rows + 3 traces.
Running it twice ⇒ 6 rows + 6 traces, indistinguishable — you cannot tell which three
belong to run A vs run B.

This is distinct from a **server-side** agent/multi-step request, which is already **one**
row with N internal hops. B17 is for the other case: separate top-level calls the *client*
knows are one workflow and wants stitched together.

## Solution overview

The client tags every call of one workflow run with a **correlation id** it mints once per
run. RouteForge captures that id, carries it opaquely, and:

1. stores it on each in-memory `RequestSummary`,
2. exposes it on `/requests` (the list) and `/requests/{id}` (the drilldown),
3. groups tagged rows into an **expandable parent → children tree** in the Requests tab,
4. stamps the same id as Langfuse's OTel session attribute so the N traces group into one
   **Langfuse Session**.

**It is a label only.** No policy, routing, gate, or cost behavior changes. RouteForge does
not infer grouping — the client is the only thing that knows N calls are "one workflow," so
the client is what tags them; RouteForge faithfully carries and displays whatever id it is
given.

**Granularity:** one session id = one workflow *run*. Never one app, never one end user. Two
runs of the same ticket are two different sessions (the id includes a per-run nonce).

## Locked decisions

Three design decisions were locked with the user before this spec:

1. **Carrier = a dedicated header, `X-RouteForge-Session`.** Not the OpenAI `user` body
   field — that field means end-user identity, and conflating per-workflow-run grouping
   with user identity is wrong (one user runs many workflows; one workflow may span
   "users"). A dedicated header keeps the two orthogonal.
2. **Persistence = in-memory only.** `session_id` lives on `RequestSummary` and in the
   response dict; **no DB column.** This matches where the Requests tab already reads
   (the in-memory `HistoryStore`) and sidesteps the no-migrations `create_all` gotcha
   (see memory `routeforge-schema-no-migrations`). Durable, cross-replica grouping is
   explicitly **B3's** job (shared history/bus), not B17's.
3. **Langfuse = yes, both surfaces.** Stamp `langfuse.session.id` on the `serving.request`
   root span so the console tree *and* Langfuse Sessions both group. (Confirmed: the
   Langfuse OTel session attribute key is `langfuse.session.id`, set on the trace's root
   span.)

## Data flow

```
client (sample app)                 RouteForge                          surfaces
──────────────────                  ──────────                          ────────
mint id once per run  ──header──▶   routes.chat_completions
X-RouteForge-Session: <id>            read + sanitize header
  (same id on all N calls)            ▼
                                    resp["session_id"] = <id|None>
                                      ├─▶ export_trace(session_id=…) ──▶ langfuse.session.id  ──▶ Langfuse Session
                                      └─▶ history.record(resp,…)      ──▶ RequestSummary.session_id
                                                                            ├─▶ GET /requests        ──▶ RequestList tree
                                                                            └─▶ GET /requests/{id}    ──▶ drilldown
```

## Server changes

### 1. Header capture + sanitization (`app/api/routes.py`)

In `chat_completions`, read the header and sanitize it into a bounded, opaque id:

```python
def _read_session_id(request: Request) -> str | None:
    """Opaque, client-supplied workflow-run correlation id (B17).

    A label only — never a policy control — so it FAILS OPEN: an absent,
    over-long, or malformed value yields None (the request is still served,
    just ungrouped). Bounded at intake because it is displayed in the console
    and stamped onto telemetry; we never trust its length or charset.
    """
    raw = request.headers.get("x-routeforge-session", "").strip()
    if not raw:
        return None
    if len(raw) > 128:
        return None
    if not _SESSION_ID_RE.fullmatch(raw):  # ^[A-Za-z0-9._:-]+$
        return None
    return raw
```

- `_SESSION_ID_RE = re.compile(r"[A-Za-z0-9._:-]+")` (module-level constant).
- Charset is deliberately narrow (alphanumerics + `._:-`) so the id is safe to render in the
  UI and to place in an OTel attribute without escaping concerns. Anything outside it is
  dropped, not rejected — B17 never fails a request over its own label.
- `session_id` is **not** added to `app/obs/otel.py` `REDACT_KEYS`. It is operator/client
  supplied and opaque; intake sanitization is what bounds it, and it must survive to the UI
  and to Langfuse intact (redacting it would defeat the grouping). Treated as an opaque,
  non-PII label by contract: the client must not place PII in the correlation id. The
  charset alone does not exclude PII (e.g. an SSN is digit-and-hyphen only and would pass),
  so this is a contract, not a filter — and the value is deliberately not redacted so
  grouping works.

Wire it in after a successful `pipeline.run`, before the telemetry/history blocks:

```python
session_id = _read_session_id(request)
resp["session_id"] = session_id
```

Because `history.record` and the drilldown both read the `resp` dict, setting it there
threads it to both surfaces with no signature change to `HistoryStore`.

### 2. `RequestSummary.session_id` (`app/api/history.py`)

Add the field and populate it from the resp dict in `record`:

```python
class RequestSummary(BaseModel):
    ...
    session_id: str | None = None   # B17: client workflow-run correlation id (opaque)
```

```python
summary = RequestSummary(
    ...
    session_id=resp.get("session_id"),
)
```

`.list()` already returns `RequestSummary` objects, so `/requests` carries `session_id`
automatically. `.get()` returns the full resp dict, which now has `session_id`, so the
drilldown carries it too. No changes to `record`'s signature, `list`, `get`, `fleet`, or
`stats` (grouping is a pure UI concern; aggregates stay per-request).

### 3. Langfuse session stamp (`app/obs/export.py`)

Add a `session_id` keyword param to `export_trace` and stamp it on the root span:

```python
def export_trace(*, request_id: str, tenant_id: str, model: str,
                 cost: dict, policy: dict, trace: list[dict],
                 application_id: str = "", app_langfuse=None,
                 capture_io: str = "off", prompt: str = "",
                 completion: str = "", recognizers=None,
                 session_id: str | None = None) -> str | None:
    ...
        with tracer.start_as_current_span("serving.request") as root:
            ...
            if session_id:
                # Langfuse groups traces sharing this attribute into one Session
                # (B17). Opaque, client-supplied; NOT in REDACT_KEYS so it passes
                # through the RedactingSpanExporter intact.
                root.set_attribute("langfuse.session.id", session_id)
```

Thread it from the route's existing `export_trace(...)` call:

```python
lf_url = export_trace(
    ...,
    recognizers=profile.recognizers,
    session_id=session_id,
)
```

Setting it on the root span of each request-trace is sufficient — Langfuse reads the
trace-level session from the root span, and RouteForge emits one trace per request, so the
N request-traces of a run collapse into one Session.

## UI changes

### 4. `RequestSummary` type (`ui/src/api.ts`)

Add `session_id?: string | null;` to the `RequestSummary` interface.

### 5. Requests tab grouping (`ui/src/components/RequestList.tsx`)

Group rows sharing a non-null `session_id` under one **expandable parent**; ungrouped rows
(null `session_id`) render flat exactly as today.

- **Grouping** is derived from the already-newest-first list: walk the list, bucket by
  `session_id`; a group's position follows its newest member. A group with only one member
  still renders as a group (a workflow can legitimately be one call), but a null id is never
  grouped.
- **Parent row** shows: the session id (truncated with the full value on hover/title),
  a call-count badge (`N calls`), summed `total_usd`, summed `latency_ms`, and a chevron.
  Default **expanded**, collapsible via local component state (no server round-trip).
- **Child rows** reuse the existing per-request row rendering (TierDot, `model_display`,
  on-prem badge, hops badge, cost, sensitivity, latency), indented under the parent, each
  still clickable → the existing drilldown (`RequestDrilldown`), unchanged.
- The parent's summed figures are display-only client-side reductions over its children;
  no new API. Percentile/stats views (`stats`, `fleet`) are untouched — they stay
  per-request, since a "session" is a client-side visual grouping, not a metering unit.

Reference mock (matches the two-runs example above):

```
▾ inv-tck1001-ab12cd34 · 3 calls   confidential   $0.0342   11.6s
     ├─ nemotron-nano-8b  cheap  $0.0097  2716 ms
     ├─ nemotron-nano-8b  cheap  $0.0134  3327 ms
     └─ nemotron-nano-8b  cheap  $0.0111  5271 ms
▾ inv-tck1001-9f3c1d20 · 3 calls   confidential   $0.0349   12.1s
     ├─ …
   direct-qa            public   $0.0021   0.9s        ← ungrouped, flat
```

## Reference integration (`test_app/routeforge_sample`)

The sample is the proof that a real client can drive grouping.

### 6. `RouteForgeChatModel` header (`routeforge.py`)

Give the model a mutable `session_id: str | None = None` attribute; when set, `_generate`
adds the header on the outbound POST:

```python
if self.session_id:
    headers["X-RouteForge-Session"] = self.session_id
```

(Added where `Authorization` is set in `_post_with_rate_limit_retry`.)

### 7. Per-run id in `SupportInvestigator.investigate` (`agent.py`)

Mint one id per investigation run and set it on the model for the duration of the chain:

```python
def investigate(self, ticket_id: str) -> ...:
    self.model.session_id = f"inv-{ticket_id.lower()}-{uuid4().hex[:8]}"
    try:
        return self._chain.invoke(...)   # all 3 model.invoke calls share the id
    finally:
        self.model.session_id = None
```

The `uuid4().hex[:8]` nonce makes each run a distinct session (two runs of the same ticket
are two sessions, per the granularity rule).

## Testing

**Backend (`tests/`):**
- `_read_session_id` sanitization: valid id passes; absent/empty → None; >128 chars → None;
  bad charset (spaces, `@`, control chars, an embedded `123-45-6789` inside disallowed
  punctuation) → None. Fail-open: a bad header never raises.
- Route: a request with a valid `X-RouteForge-Session` yields `resp["session_id"]` set and
  a `RequestSummary.session_id` equal to it via `history.record`; a request without the
  header yields `session_id=None`.
- `/requests` and `/requests/{id}` expose `session_id`.
- `export_trace` stamps `langfuse.session.id` on the root span when `session_id` is given,
  and omits it when None — asserted via a fake tracer/span that records `set_attribute`
  calls (the existing export tests' pattern). Confirm `session_id` is not redacted (still
  equals the input after the RedactingSpanExporter).

**Sample (`test_app/.../tests`):**
- `investigate()` sends the **same** `X-RouteForge-Session` on all three outbound calls, and
  a second `investigate()` of the same ticket sends a **different** id (assert via a stubbed
  HTTP layer capturing request headers).

**UI:** no test runner in this repo — verify with `./node_modules/.bin/tsc -b --noEmit` and
`vite build`, plus manual check that a shared-session set collapses into one expandable
parent and ungrouped rows stay flat.

## Out of scope (explicit)

- **DB persistence** of `session_id` — deferred; durable/cross-replica grouping is **B3**.
- **Server-side multi-step / agent requests** — already one row + one trace; unchanged.
- **Any policy, routing, gate, cost, or metering behavior** — B17 is a label only.
- **Server-inferred grouping** — RouteForge never guesses a session; the client tags it.
- **Session-level aggregates in `stats`/`fleet`** — those stay per-request.

## Files touched

| File | Change |
|---|---|
| `app/api/routes.py` | `_read_session_id` + `_SESSION_ID_RE`; set `resp["session_id"]`; pass `session_id` to `export_trace` |
| `app/api/history.py` | `RequestSummary.session_id` field; populate in `record` |
| `app/obs/export.py` | `export_trace` `session_id` param; stamp `langfuse.session.id` on root span |
| `ui/src/api.ts` | `session_id?` on `RequestSummary` |
| `ui/src/components/RequestList.tsx` | group-by-session expandable parent→children tree |
| `test_app/routeforge_sample/routeforge.py` | `session_id` attr + `X-RouteForge-Session` header |
| `test_app/routeforge_sample/agent.py` | per-run id in `investigate` |
| `tests/…`, `test_app/…/tests` | coverage per Testing section |
| `docs/bugs-backlog.md` | mark B17 resolved |
