# Tool-call redaction + policy-doc corrections — Implementation Plan

**Goal:** Close three verified privacy gaps where OpenAI `tool_calls` arguments bypass the
policy gate and step-9 egress verification, loosen one over-strict validator rule, and fix the
doc contradictions found while reviewing `docs/archive/design-handoffs/policy-protection-redesign-handoff.md`.

**Spec:** none separate — the binding authority is `CLAUDE.md` non-negotiable rules 1, 2, 3, 5
and 8, plus the verified findings below. This is a bug-fix plan, NOT the policy redesign in the
handoff doc (independent model/client/telemetry axes, tokenization broker — explicitly out of scope).

## Verified findings (reproduced on main `d484a50`)

1. **Ingress redaction gap.** `Pipeline._sanitize_msg` (`app/pipeline.py`) redacts `m.content`
   but forwards `m.tool_calls` untouched. A client replaying an assistant turn whose
   `function.arguments` contains an SSN sends the raw SSN to the model under `policy_mode: enforced`.
2. **Ingress classification gap.** `PolicyGate.evaluate` (`app/policy/gate.py`) builds the scan
   text from `m.content` only. An SSN appearing ONLY in tool-call arguments is never detected, so
   sensitivity/`onprem_only` escalation never fires — the request can route to cloud.
3. **Egress gap.** In `Pipeline.run`, `verify(det, ctx, result.text)` checks only the text; the
   model-generated `result.tool_calls` are returned to the client un-verified.
4. **Over-strict validator.** `set_policy` in `app/api/admin_routes.py` rejects
   `capture_io='redacted'` for `pii_detection` `off` OR `regex`, claiming the sanitized prompt "is
   raw". Under `regex` the gate's RegexDetector DOES redact (SSN/card/email/custom recognizers), so
   the sanitized prompt is not raw. Only `off` justifies the rejection. `ui/src/admin/AppSettings.tsx`
   (`redactedBlockedFor`) mirrors the same over-strict rule.

Failing repro tests already exist (uncommitted) in `tests/test_tool_call_redaction.py`.

## Global Constraints

- Run tests with `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest -q -p no:cacheprovider -W ignore <files>`
  (without that env var pytest hangs). Always pass a Bash `timeout` of 600000 for the full suite.
- Contract tests drive the real `/v1/chat/completions` boundary via the `client` fixture and the
  recording backend (`tests/_litellm_fake.py`: `CALLS`, `make_recording_acompletion`,
  `make_response`) and assert on `CALLS[-1]` (what actually reached the model) and on the HTTP
  response body — never only on trace claims. Use a realistic SSN such as `482-73-1946`.
- No application names in symbols, tests, comments, or config.
- `policy_mode: passthrough` must remain byte-for-byte unaffected: its detector is the null
  detector, so redaction of tool-call arguments must naturally be a no-op there. Add one test proving
  a passthrough app's tool-call arguments reach the model AND the client unchanged.
- Tool-call arguments are JSON strings. Redact the `function.arguments` STRING in place with the
  request's effective detector (`det.anonymize(text, det.analyze(text))`); do not parse/re-serialize
  JSON. Do not mutate the caller's objects — build new dicts/Messages. Preserve `id`, `type`,
  `function.name` and every other key exactly.
- Detected entity TYPES only may enter records/traces — never values.
- Commit with a conventional message ending in the trailer
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`. Never commit `.superpowers/`.

---

## Task 1: Ingress — classify and redact tool-call arguments

**Files:** `app/policy/gate.py`, `app/pipeline.py`, `tests/test_tool_call_redaction.py`.

1. Add one small shared helper (e.g. in `app/policy/gate.py` or a tiny module under
   `app/policy/`) that returns the concatenated text of a message's tool-call arguments
   (`tool_calls[*].function.arguments`, skipping missing/non-string values), and one that returns a
   copy of a `tool_calls` list with each `function.arguments` string redacted by a given detector.
   Reuse the redaction helper in Task 2 — keep it importable.
2. `PolicyGate.evaluate`: include each message's tool-call argument text in the scan text used for
   `det.analyze` / classification, so an SSN only in arguments sets the same sensitivity and
   `onprem_only` it would in `content`. The returned `redacted` string is only consumed by the
   flattened (non-tool) path; make sure adding argument text does not change the flattened prompt
   for ordinary requests (tool_calls only exist on tool conversations — assert with an existing
   non-tool test staying green).
3. `Pipeline._sanitize_msg`: also redact `tool_calls` arguments via the helper.
4. Tests in `tests/test_tool_call_redaction.py`:
   - keep `test_replayed_tool_call_arguments_are_redacted_before_model` (must now pass);
   - add a test that an SSN present ONLY in replayed tool-call arguments escalates the request the
     same way an SSN in content does (assert `onprem_only`/sensitivity via the response's trace or
     policy record, whichever the existing tests use — look at `tests/test_openai_compat.py` and
     `tests/test_pii_entity_filter.py` for the pattern);
   - add the passthrough no-op test (Global Constraints) for the ingress direction;
   - mark `test_model_generated_tool_call_arguments_are_verified_on_egress` with
     `@pytest.mark.xfail(strict=True, reason="egress tool_calls verification — Task 2")`.
5. Run `tests/test_tool_call_redaction.py tests/test_openai_compat.py tests/test_pii_entity_filter.py`
   plus any gate tests (`tests/test_gate*.py`, `tests/test_policy*.py`), then commit.

## Task 2: Egress — verify model-generated tool-call arguments

**Files:** `app/pipeline.py`, `app/api/routes.py`, `tests/test_tool_call_redaction.py`.

1. In `Pipeline.run`, after `verify(det, ctx, result.text)`, redact `result.tool_calls` arguments
   with the same `det` using Task 1's helper. The client-facing `assistant` message must carry the
   redacted tool_calls. Entity types found in arguments must be reflected in the verification
   record's `entities_found` (merge, sorted, de-duplicated) so the trace is honest.
2. `capture_io: full` must still see the RAW model tool_calls (full = raw, own project only). The
   response dict already carries an internal `raw_completion` that `app/api/routes.py` pops before
   serialization. Add an internal `raw_tool_calls` the same way: set it in `run()`, pop it in
   routes.py alongside `raw_completion` (search for `resp.pop("raw_completion"`), and have
   `_full_capture_io` prefer it over the (now redacted) `message.tool_calls`. It must NEVER appear in
   the public response — add an assertion to the egress test that neither `raw_completion` nor
   `raw_tool_calls` is a key anywhere in the JSON body. Check every other place that builds a
   response from `run()` output (streaming fallback in `routes.py` uses `resp["choices"]`) pops it too.
3. Tests:
   - remove the xfail from `test_model_generated_tool_call_arguments_are_verified_on_egress`;
   - add a `stream: true` variant for an app whose streaming is enabled (find how
     `tests/test_streaming.py` / the PR #41 tool-call streaming tests enable it) asserting the SSN is
     absent from every SSE frame;
   - add the passthrough no-op test for the egress direction;
   - extend or add a full-capture test (see `tests/test_trace_full_io.py`) asserting the exported
     output still contains the raw tool_calls arguments for a full-capture app.
4. Run `tests/test_tool_call_redaction.py tests/test_openai_compat.py tests/test_streaming.py
   tests/test_trace_full_io.py tests/test_raw_completion_capture.py tests/test_run_stream_pipeline.py`,
   then commit.

## Task 3: Validator rule for `regex` + docs corrections

**Files:** `app/api/admin_routes.py`, `ui/src/admin/AppSettings.tsx`, `tests/test_admin_api.py`,
`docs/guides/choosing-app-settings.md`, `docs/guides/getting-started.md`, `docs/guides/faq.md`,
`docs/guides/ui-guide.md`, `docs/reference/per-app-config.md`, `docs/reference/api-reference.md`.

1. `set_policy`: the `capture_io == "redacted"` rejection under the PII-downgrade branch must fire
   only for effective `pii_detection == "off"`, not `regex`. Update the 422 message to say `off`
   only. Keep the `passthrough` + `redacted` rejection unchanged. Update the comment block above it.
2. `ui/src/admin/AppSettings.tsx` `redactedBlockedFor`: block only `pii_detection === "off"`. Type-check
   with `cd ui && ./node_modules/.bin/tsc -b --noEmit` (do not use `npm run`).
3. `tests/test_admin_api.py`: add tests that `regex` + `redacted` is accepted (with
   `allow_app_pii_downgrade` on, as the existing regex tests arrange) and `off` + `redacted` is still 422.
   Search for any existing test asserting the regex rejection and update it.
4. Docs — make every statement match the code:
   - Remove every claim that `regex` cannot be combined with `capture_io: redacted`
     (`getting-started.md` around the pii_detection table, the "rejected if capture_io is redacted"
     bullet, and the `regex pairs only with off or full` note; `per-app-config.md`; `choosing-app-settings.md`).
     Say instead: `off` + `redacted` is rejected; `regex` + `redacted` is allowed and telemetry is
     redacted at regex-detector quality (misses entities without a pattern, e.g. names).
   - `choosing-app-settings.md` (~line 41) says raw capture requires a no-protection posture. Correct
     it to the code's rule: `capture_io: full` requires the app's own Langfuse project, and is refused
     only for `onprem_only` + `enforced`; a non-`onprem_only` `enforced` app may use it.
   - Replace "don't touch my data" phrasing (`faq.md`, `getting-started.md`, `ui-guide.md`,
     `per-app-config.md`) with wording that says passthrough disables PII detection/redaction and
     output verification; context compression (if enabled) can still transform content — disable
     compression for byte-exact content.
   - `api-reference.md` / `faq.md` where `raw_completion` is mentioned: state it is internal-only,
     never serialized in the public response, and reaches only the app's own Langfuse project under
     `capture_io: full`.
   - Add a short note to `per-app-config.md` (policy/redaction section) that under `enforced`,
     tool-call arguments in both replayed assistant turns and model-generated tool calls are
     scanned, classified and redacted like message content.
5. Run `tests/test_admin_api.py` and any test file grepping for `sanitizes nothing` / the old 422 text,
   then commit.
