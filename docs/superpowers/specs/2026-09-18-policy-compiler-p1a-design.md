# P1a — Constraint-isolated policy compiler and noninterference harness

**Status:** Approved design, 2026-09-18. Not yet implemented.
**Supersedes, in part:** `docs/new/policy-protection-redesign-handoff.md` §§4–6, 8, 9, 13
(the independent-axis redesign). Tokenization (§7), authorized rehydration, and the
per-tool axes remain deferred — see §9 below.
**Implements:** Priority 1, slice A of `docs/new/RouteForge-Priorities-1-3-LLM-Handoff.md`.
**Depends on:** the 2026-09-17 removal of the internal NeMo agent (merged, PR #51) —
there is one request path per model call, so the compiler has one seam to own.

## 1. Problem

`policy_mode` controls six independent decisions at once: whether the detector runs,
whether input is redacted before the model, whether custom recognizers run, whether
sensitivity escalates placement, whether egress redaction runs, and which telemetry
capture values are legal. Consequences visible in the code today:

- **The main missing configuration.** "Trusted model and tool see the real value; the
  client response is redacted; telemetry stores a redacted copy or nothing" cannot be
  expressed. `admin_routes.py:631` rejects `passthrough + capture_io: redacted`
  outright, because the exporter's "redacted" view is just whatever the gate handed the
  model (`app/obs/export.py:79`).
- **A placement setting silently rewrites telemetry protection.** `app/api/routes.py:599`
  turns `full` into `redacted` for any `onprem_only` app regardless of `policy_mode`, so
  an app the admin API accepted as `onprem_only + passthrough + full` exports content
  that is raw apart from a regex pass, under a "redacted" label.
- **A protection setting silently loosens placement.** Choosing `passthrough` disables
  the detector, so an SSN that would force `onprem_only` under `enforced` no longer does.
- **`capture_io` is never validated** against its enum in `set_policy`
  (`admin_routes.py:424-431` checks residency, pii_detection, stream_mode, policy_mode
  and stops there).

These are not four unrelated bugs. They are what happens when placement and protection
share one control and no component can state what the effective policy is.

## 2. Approach

One pure function compiles the effective policy; everything else consumes its output.

```python
# app/policy/compile.py
compile_policy(app_policy: AppPolicyView, deployment: Settings) -> EffectivePolicy
apply_signals(eff: EffectivePolicy, detected: tuple[str, ...], rules) -> EffectivePolicy
```

`compile_policy` is static: no DB, no clock, no request. It is what `set_policy`
validates, what `app_policy_version` hashes, and what the harness calls hundreds of
thousands of times. `apply_signals` is **raise-only**: it may tighten placement and set
`require_verification`; it can never widen either, and it never touches a protection
axis.

### 2.1 Types

```python
Residency        = Literal["any", "private_cloud_ok", "onprem_only"]
ModelInput       = Literal["raw", "redact"]                      # "tokenize" rejected, §9
ClientOutput     = Literal["raw", "redact"]                      # "authorized_rehydrate" rejected, §9
TelemetryIO      = Literal["off", "redacted", "full"]
Detection        = Literal["off", "regex", "presidio"]
ContentIntegrity = Literal["exact", "transformations_allowed"]

@dataclass(frozen=True)
class Placement:
    residency: Residency
    sources: tuple[str, ...]        # e.g. ("app_policy",) or ("app_policy", "rule:restricted")

@dataclass(frozen=True)
class Protection:
    model_input: ModelInput
    client_output: ClientOutput
    telemetry_io: TelemetryIO
    detection: Detection
    content_integrity: ContentIntegrity
    stream_mode: StreamMode
    sources: Mapping[str, str]      # field -> "app_policy" | "preset:enforced" | "ceiling" | "deployment_default"

@dataclass(frozen=True)
class EffectivePolicy:
    placement: Placement
    protection: Protection
    escalate_residency_on_detection: bool
    require_verification: bool
    version: str                    # app_policy_version
```

### 2.2 Precedence

1. Deployment ceilings (`allow_app_passthrough`, `allow_app_pii_downgrade`)
2. Explicit per-app axis value
3. `policy_mode` preset
4. Deployment default

A ceiling that cannot be parsed **denies**; it never opens a permission.

### 2.3 Detection runs when a consumer needs it

`Pipeline._gate_inputs` stops branching on `policy_mode`. The detector is built when
**any** of these is true: `model_input == "redact"`, `client_output == "redact"`,
`telemetry_io == "redacted"`, or `escalate_residency_on_detection` is true — and its
kind comes from the `detection` axis. When none of them holds, the NullDetector is used
and nothing is scanned, which is what a router-only app gets.

### 2.4 Explicit representations replace one mutable `sanitized_prompt`

The gate already computes both the original text and the redacted text; today callers
bind the redaction map to `_map` and discard it, and the exporter reuses whatever the
model received. Instead the gate returns a small frozen record:

```python
@dataclass(frozen=True)
class Representations:
    raw: <messages>            # never leaves the gate's trust boundary except by policy
    redacted: <messages> | None   # None only when no detector ran
    detected_entity_types: tuple[str, ...]
```

Each consumer selects its own view: the model by `model_input`, the client by
`client_output`, the exporter by `telemetry_io`. No consumer reads another's view.

### 2.5 Egress becomes independent of ingress

`app/output/verify.py` runs the real detector when `client_output: redact`, even when
the model received raw text. When `client_output: raw`, no egress redaction runs (what
`passthrough` does today via the NullDetector). The `UNVERIFIABLE` leakage check and the
`require_verification` fail-closed behavior are unchanged in both cases.

### 2.6 Telemetry projects independently

`app/obs/export.py::_set_io` builds its own redacted copy from the raw values using the
app's detector, instead of reusing the gate's model-facing view. Fail-closed rule when
the app's own Langfuse project is missing or a per-app tracer cannot be built:

- a genuinely redacted copy exists (a detector ran) → export `redacted`;
- otherwise → export `off`.

Never raw under a "redacted" label. This replaces the clamp at `app/api/routes.py:599`.

## 3. Schema

Four new nullable columns on `AppPolicy` (null = inherit, as with every existing field):

| Column | Type | Values |
|---|---|---|
| `model_input` | str \| None | `raw` \| `redact` |
| `client_output` | str \| None | `raw` \| `redact` |
| `content_integrity` | str \| None | `exact` \| `transformations_allowed` |
| `escalate_residency_on_detection` | bool \| None | default true when null |

`create_all` never alters an existing table, so each deployment runs four
`ALTER TABLE app_policies ADD COLUMN …` statements **before** the new image rolls.
All four are added to `store.POLICY_FIELDS`, to `PolicyIn`, to the `reset` field list,
and to `AppPolicyView.version()`'s hash input.

## 4. Presets and the compatibility contract

```
policy_mode: enforced      -> model_input: redact,  client_output: redact
policy_mode: passthrough   -> model_input: raw,     client_output: raw
```

`passthrough` does **not** force `detection: off` by itself; the `pii_detection` axis
does. But an app whose stored policy is `passthrough` with `pii_detection` null resolves
to `off`, which is what those apps do today. `kit-gtm` on the Spark (`passthrough`,
`capture_io: full`) is a golden-table row and must be byte-identical.

**The golden table** asserts, for every currently-legal combination of
`policy_mode × pii_detection × capture_io × residency × stream_mode`, that the compiled
effective policy produces today's behavior — including the combinations that must keep
being **rejected**:

- `onprem_only + enforced + full`
- `pii_detection: off + capture_io: redacted` — nothing exists to redact with
- `stream_unverified + onprem_only`
- `capture_io: full` with no app-specific Langfuse project

**The two deliberate unlocks**, each a documented row change:

1. `telemetry_io: redacted` is legal with `model_input: raw`, because §2.6 gives
   telemetry its own sanitizer. So the pair that `admin_routes.py:631` rejects today —
   `passthrough + capture_io: redacted` — becomes legal **when a detector is
   configured**, and stays rejected under `detection: off`, which is the case the
   existing rule was really protecting against.
2. `client_output: redact` is legal with `model_input: raw` — the missing scenario.

**Raw capture rules, preserved:** `full` requires the app's own Langfuse project;
`onprem_only + full` requires the declared no-protection posture, now spelled
`model_input: raw` **and** `client_output: raw`; non-`onprem_only + enforced + full`
stays legal. This keeps CLAUDE.md rule 2 true with its 2026-09-05 amendment intact.

**Ceilings:** `allow_app_passthrough` gates `raw` on `model_input` and on
`client_output` independently — both are declared no-protection postures.
`allow_app_pii_downgrade` continues to gate `detection` ∈ {`off`, `regex`}. No new
config keys.

## 5. Cross-axis rules

The one cross-axis effect RouteForge has is `config/policy.yaml`'s `sensitivity_rules`:
detected entities raise sensitivity, residency, `risk_class` and `require_verification`.
It stays, and becomes **declared and switchable**:

- `escalate_residency_on_detection` (default **true**) decides whether a rule may raise
  placement for that app.
- A rule may only **tighten**. It can never lower the app's configured residency floor,
  and `false` only prevents tightening.
- Every rule that fires is named in the certificate (`sp.cross_axis_rules_fired`) and its
  effect appears in `sp.placement_sources`.

This is what makes noninterference checkable: placement changes only via the declared
rule, never as a side effect of a protection setting.

## 6. The decision certificate

Built by the compiler, surfaced through three existing channels — the response `policy`
block, the trace attributes, and the console drill-down (which reads the stored request
snapshot). No new table, no new endpoint.

```
sp.placement, sp.placement_sources
sp.model_input_mode, sp.client_output_mode, sp.telemetry_io_mode
sp.detection_mode, sp.content_integrity_mode, sp.stream_mode
sp.cross_axis_rules_fired, sp.detected_entity_types        # types only, never values
sp.candidates (already emitted), sp.app_policy_version, sp.routing_policy_version
sp.checks: {chosen_in_eligible_set, fallback_in_eligible_set}
```

`sp.checks` carries only what is verifiable for one request. Policy-wide invariants are
vouched for by `app_policy_version`, which passed the save-time check (§7). Raw values
never appear in the certificate — entity types and counts are the evidence.

## 7. The harness

`tests/policy_compiler/`, using Hypothesis to generate app policies, deployment ceilings
and request signals. Invariants:

1. **Monotonicity** — tightening any axis never enlarges the eligible candidate set.
2. **Noninterference** — changing a protection axis never changes placement unless a
   declared cross-axis rule fired and is named in the certificate.
3. **Placement never weakens protection** — changing residency leaves every protection
   axis unchanged.
4. **Ceilings win; malformed ceilings close** the permission.
5. **Empty eligible set rejects** — never a silent relaxation (this is IP-9's derived
   reject, and `has_healthy_onprem()` false is one of its inputs).
6. **Preset fidelity** — the golden table of §4.

**Save-time check.** `set_policy` runs the compiler over a fixed grid for that one app
(the axes are small enums) and returns a 422 naming the violated invariant, so a policy
that breaks an invariant cannot be persisted. It must add no unbounded work to the write
path.

**The three counterexample regression tests**, each named for the bug it encodes:

| id | Counterexample |
|---|---|
| `CE-1` | `onprem_only + passthrough + full` exported content labelled `redacted` that was raw apart from a regex pass (`app/api/routes.py:599`) |
| `CE-2` | `set_policy` accepted any string as `capture_io` |
| `CE-3` | selecting `passthrough` silently disabled placement escalation for a detected SSN |

## 8. Non-negotiables this must not break

Re-assert by test, not by inspection: nothing external sees the raw request before the
gate (CLAUDE.md rule 1); `onprem_only` filters the candidate pool before scoring, by
construction (rule 3); the gate fails closed (rule 5); CCR and the redaction map stay
inside the residency boundary and out of telemetry (rule 6); compression runs after the
gate (rule 7); residency beats the risk floor (rule 8). `content_integrity: exact`
disables lossy compression for that app and is recorded on the trace; it is a data
control, not a privacy control, and never changes placement.

## 9. Out of scope, and rejected rather than faked

`model_input: tokenize`, `client_output: authorized_rehydrate`, the per-tool axes
(`tool_arguments`, `tool_results_to_model`), the protected-reference broker, and
endpoint data-handling metadata (region, retention — P1b). A policy naming any of them
gets a 422 that says it is not implemented. The handoff permits deferring the broker
only on this condition: RouteForge must reject the strict SSN-dependent tool scenario
clearly rather than pretend `[SSN]` is a usable lookup key.

The privacy-gate skill currently documents a "detokenize on egress" mode. The redaction
map is discarded (`app/pipeline.py`, the `_map` binding) and nothing detokenizes
anywhere; that section gets corrected to say so.

## 10. Documentation

`docs/reference/per-app-config.md` becomes the authoritative page for the axes and the
precedence order. The four guides named in the handoff (`choosing-app-settings.md`,
`creating-a-sample-application.md`, `faq.md`, `getting-started.md`) are reconciled
against it, resolving the contradictions the handoff lists: the full-capture rule, what
`regex` actually detects and redacts, what `pii_detection: off` disables, and the
difference between a deployment **default** and a deployment **ceiling**. CLAUDE.md
rule 2a gains the axis vocabulary. `pii_entity_denylist` keeps its name; renaming it is
a separate change with an API-compatibility cost.

The handoff's §14.10 instruction to relabel the semantic cache as unimplemented is
**wrong** and must not be followed: the cache shipped in PR #23 and is wired in
`app/main.py` and `app/pipeline.py`, defaulting off.

## 11. Research artifact

`docs/research/policy-compiler-noninterference.md`, written as the code lands: the
formal model (axes, ordering, meet), the invariants of §7, every counterexample with its
commit SHA, the harness setup and how to reproduce it, related work to verify (the
arXiv IDs in `docs/roadmap/ip-and-research-opportunities.md` are flagged unverified),
and the file-before-publish note — P1a overlaps IP-1, IP-4 and IP-9, and public
disclosure forecloses patents on the same mechanism.

## 12. Implementation sequencing

One spec, one branch, but the plan sequences in two waves so each half is reviewable:

- **Wave 1 — the seam.** `compile.py` and its types, the four columns, preset
  resolution, ceilings, validation (including `capture_io`), the golden table, and the
  save-time check. Behavior-preserving except the two unlocks.
- **Wave 2 — consumers and evidence.** The representations record, independent egress
  redaction, independent telemetry projection (replacing the `routes.py:599` clamp),
  `content_integrity: exact`, the certificate, the Hypothesis harness with `CE-1..3`,
  the console, the docs, and the research artifact.

## 13. Definition of done

- An operator can configure: raw model input, redacted client response, redacted or no
  telemetry, `onprem_only` placement — and a request carrying an SSN demonstrably shows
  the model the real value while the HTTP response and the Langfuse trace contain none.
- A router-only app (`passthrough`, `detection: off`, `capture_io: off` or `full`) is
  byte-identical to today.
- The harness finds `CE-1`, `CE-2` and `CE-3` from a clean checkout of the pre-fix code,
  and passes after the fixes.
- Every golden-table row holds; the two unlocks are the only changed rows.
- `capture_io` is validated; no code path can export raw content labelled redacted.
- The four `ALTER` statements are documented in the PR body, ordered before the image
  roll.
