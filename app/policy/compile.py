"""The pure effective-policy compiler (design P1a §2).

**The rule the whole design rests on: placement is computed only from the
application's `residency` and from declared cross-axis rules. No protection
axis may ever appear in a placement computation.** `residency` says *where*
compute may run; `model_input`, `client_output`, `telemetry_io`, `detection`
and `content_integrity` say *how much is done to the data*. A protection
setting must never move placement, and placement must never weaken protection.
The only path from a protection observation to placement is the declared
`sensitivity_rules` cross-axis rule, applied by `apply_signals`, gated by the
app's `escalate_residency_on_detection`, raise-only, and named in
`Placement.sources`.

`compile_policy` is static and total: no DB, no clock, no request, no I/O, no
randomness. It is what the admin API validates, what `app_policy_version`
hashes, and what the noninterference harness calls hundreds of thousands of
times. Every value it returns is frozen.

Precedence (design §2.2), highest first:

1. deployment ceilings (`allow_app_passthrough`, `allow_app_pii_downgrade`)
2. the explicit per-app axis value
3. the `policy_mode` preset
4. the deployment default

A ceiling — or any axis value — that cannot be parsed **denies**: it resolves
to the protective value and is attributed to `"ceiling"`. It never opens a
permission (CLAUDE.md rule 5, fail closed).

This module wires nothing into the request path; consumers land in later tasks.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Literal, Mapping

from app.config.schema import Settings
from app.policy.enums import PII_MODES, POLICY_MODES, RESIDENCY, STREAM_MODES

# --- axis vocabularies -------------------------------------------------------

Residency = Literal["any", "private_cloud_ok", "onprem_only"]
ModelInput = Literal["raw", "redact"]
ClientOutput = Literal["raw", "redact"]
TelemetryIO = Literal["off", "redacted", "full"]
Detection = Literal["off", "regex", "presidio"]
ContentIntegrity = Literal["exact", "transformations_allowed"]
StreamMode = Literal["off", "verified_then_stream", "stream_unverified",
                     "incremental_verify"]

MODEL_INPUTS = ("raw", "redact")
CLIENT_OUTPUTS = ("raw", "redact")
TELEMETRY_IOS = ("off", "redacted", "full")
CONTENT_INTEGRITY = ("exact", "transformations_allowed")

#: Axis values the design names but deliberately does not implement (design §9).
#: They are rejected rather than faked — a policy naming one gets a 422 from the
#: admin API (a later task); here they simply fail closed like any other
#: unparseable value, so no request can run under a mode that does not exist.
UNIMPLEMENTED = {"tokenize", "authorized_rehydrate", "resolve_tokens",
                 "trusted_raw", "inherit_model"}

#: Ordering for "tighten only". Higher rank = stricter placement.
_RESIDENCY_RANK = {"any": 0, "private_cloud_ok": 1, "onprem_only": 2}

#: What each axis resolves to when a value cannot be parsed, or when a ceiling
#: forbids the requested one. Always the protective end of the axis. Every axis
#: `_resolve` is ever called for MUST have an entry — a missing one turns a
#: malformed stored value into a KeyError (a 500) instead of a denial, which is
#: exactly the fail-open this module exists to prevent. `_AXES` below is the
#: guard: it asserts the two stay in step at import time.
_DENY = {
    "residency": "onprem_only",
    "policy_mode": "enforced",
    "model_input": "redact",
    "client_output": "redact",
    "telemetry_io": "off",
    "detection": "presidio",
    "content_integrity": "transformations_allowed",
    "stream_mode": "off",
}

#: Every axis this module resolves. Kept beside `_DENY` so a future axis cannot
#: be added to one and forgotten in the other.
_AXES = ("residency", "policy_mode", "model_input", "client_output",
         "telemetry_io", "detection", "content_integrity", "stream_mode")
assert set(_AXES) == set(_DENY), "every axis needs a fail-closed deny value"

# Source labels, so every resolved value can name the rung of §2.2 it came from.
_APP = "app_policy"
_CEILING = "ceiling"
_DEFAULT = "deployment_default"


# --- types (design §2.1) -----------------------------------------------------

@dataclass(frozen=True)
class Placement:
    """Where compute may run, and what decided that. Nothing else."""

    residency: Residency
    sources: tuple[str, ...]  # ("app_policy",) | ("app_policy", "rule:restricted")


@dataclass(frozen=True)
class Protection:
    """What is done to the data. Never an input to a placement computation."""

    model_input: ModelInput
    client_output: ClientOutput
    telemetry_io: TelemetryIO
    detection: Detection
    content_integrity: ContentIntegrity
    stream_mode: StreamMode
    #: axis name -> "app_policy" | "preset:<mode>" | "ceiling" | "deployment_default"
    sources: Mapping[str, str]


@dataclass(frozen=True)
class EffectivePolicy:
    """The compiled policy every consumer reads instead of re-deriving."""

    placement: Placement
    protection: Protection
    escalate_residency_on_detection: bool
    require_verification: bool
    version: str  # app_policy_version
    #: Sensitivity label, raised by a cross-axis rule in `apply_signals`.
    sensitivity: str | None = None
    #: Every declared cross-axis rule that fired, whether or not it was allowed
    #: to move placement — the certificate's `sp.cross_axis_rules_fired`.
    cross_axis_rules_fired: tuple[str, ...] = ()
    #: `(axis, offending_value)` for every stored value that could not be
    #: parsed and was denied to the safe default. Compilation still fails
    #: closed; this is the evidence that survives it, so a validator can tell a
    #: typo apart from a deliberate safe setting and reject the write.
    rejected: tuple[tuple[str, str], ...] = ()


# --- helpers -----------------------------------------------------------------

def _get(app_policy, name: str):
    """Read one per-app axis. Task 2 adds four of these columns to
    `AppPolicyView`; reading them defensively is what lets this module ship
    first and stand alone."""
    if app_policy is None:
        return None
    return getattr(app_policy, name, None)


def _flag(deployment: Settings, name: str) -> bool:
    """Read a deployment ceiling. A ceiling that is missing or is not a real
    bool cannot be parsed, so it **denies** (design §2.2)."""
    value = getattr(deployment.serving, name, None)
    return value is True


def _resolve(axis: str, app_value, legal: tuple[str, ...],
             preset: str | None, preset_label: str | None,
             default: str, rejected: list[tuple[str, str]]) -> tuple[str, str]:
    """Rungs 2-4 of the precedence order for one axis: the explicit per-app
    value, else the `policy_mode` preset, else the deployment default. Returns
    `(value, source)`.

    An explicit value that is unimplemented (design §9) or simply not a legal
    member of the axis **denies**: it resolves to `_DENY[axis]`, is attributed
    to the ceiling, and — crucially — is appended to `rejected` as
    `(axis, offending_value)`.

    That last part is why compilation can fail closed without losing evidence.
    `source == "ceiling"` alone is ambiguous: it also means "a legal `raw` was
    clamped by `allow_app_passthrough`". Without `rejected`, `capture_io:
    "sometimes"` and `capture_io: "off"` compile to the same
    `telemetry_io: "off"` and the typo is unrecoverable — so the admin API
    could only answer 200 to a policy the operator never asked for. Keeping the
    offending value lets a later task's validator raise
    `CAPTURE_IO_UNKNOWN_VALUE` / `AXIS_VALUE_NOT_IMPLEMENTED` (design §9's
    "a 422 that says it is not implemented") off the compiled result.
    """
    if app_value is not None:
        if app_value in legal:
            return app_value, _APP
        rejected.append((axis, str(app_value)))
        return _DENY[axis], _CEILING
    if preset is not None:
        return preset, preset_label or _DEFAULT
    return default, _DEFAULT


# --- the compiler ------------------------------------------------------------

def compile_policy(app_policy, deployment: Settings) -> EffectivePolicy:
    """Compile one application's stored policy plus the deployment's settings
    into the single `EffectivePolicy` every consumer reads.

    Pure: given the same two inputs it returns an equal value forever. It
    touches no DB, no clock and no request, and it never inspects a protection
    axis while computing placement.
    """
    rejected: list[tuple[str, str]] = []

    # -- placement. Residency, and only residency. -----------------------------
    residency = _get(app_policy, "residency") or "any"
    if residency in RESIDENCY:
        placement_source = _APP if app_policy is not None else _DEFAULT
    else:
        rejected.append(("residency", str(residency)))
        residency, placement_source = _DENY["residency"], _CEILING
    placement = Placement(residency=residency, sources=(placement_source,))

    # -- protection. Ceilings first (rung 1), then preset, then defaults. ------
    allow_passthrough = _flag(deployment, "allow_app_passthrough")
    allow_pii_downgrade = _flag(deployment, "allow_app_pii_downgrade")

    mode = _get(app_policy, "policy_mode") or deployment.serving.default_policy_mode
    if mode not in POLICY_MODES:
        rejected.append(("policy_mode", str(mode)))
        mode = _DENY["policy_mode"]            # unparseable posture denies
    if mode == "passthrough" and not allow_passthrough:
        mode = "enforced"                      # deployment ceiling wins
    preset_label = f"preset:{mode}"
    preset_raw = "raw" if mode == "passthrough" else "redact"

    sources: dict[str, str] = {}

    model_input, sources["model_input"] = _resolve(
        "model_input", _get(app_policy, "model_input"), MODEL_INPUTS,
        preset_raw, preset_label, "redact", rejected)
    client_output, sources["client_output"] = _resolve(
        "client_output", _get(app_policy, "client_output"), CLIENT_OUTPUTS,
        preset_raw, preset_label, "redact", rejected)

    # `raw` on either side is a declared no-protection posture, and
    # `allow_app_passthrough` gates the two of them independently (design §4).
    if not allow_passthrough:
        if model_input == "raw":
            model_input, sources["model_input"] = "redact", _CEILING
        if client_output == "raw":
            client_output, sources["client_output"] = "redact", _CEILING

    # Detection: explicit axis; else off under `passthrough` (what those apps
    # do today); else the deployment default.
    detection, sources["detection"] = _resolve(
        "detection", _get(app_policy, "pii_detection"), PII_MODES,
        "off" if mode == "passthrough" else None, preset_label,
        deployment.serving.pii_detection, rejected)
    if detection not in PII_MODES:                     # malformed deployment value
        rejected.append(("detection", str(detection)))
        detection, sources["detection"] = _DENY["detection"], _CEILING
    if not allow_pii_downgrade and detection in ("off", "regex"):
        # The ceiling clamps a LEGAL value — not a rejection.
        detection = deployment.serving.pii_detection
        sources["detection"] = _CEILING
        if detection not in PII_MODES:                 # ...and the default is junk too
            rejected.append(("detection", str(detection)))
            detection = _DENY["detection"]

    telemetry_io, sources["telemetry_io"] = _resolve(
        "telemetry_io", _get(app_policy, "capture_io"), TELEMETRY_IOS,
        None, None, deployment.serving.observability.langfuse.capture_io,
        rejected)
    if telemetry_io not in TELEMETRY_IOS:              # malformed deployment value
        rejected.append(("telemetry_io", str(telemetry_io)))
        telemetry_io, sources["telemetry_io"] = _DENY["telemetry_io"], _CEILING

    content_integrity, sources["content_integrity"] = _resolve(
        "content_integrity", _get(app_policy, "content_integrity"),
        CONTENT_INTEGRITY, None, None, "transformations_allowed", rejected)

    stream_mode, sources["stream_mode"] = _resolve(
        "stream_mode", _get(app_policy, "stream_mode"), STREAM_MODES,
        None, None, "off", rejected)

    protection = Protection(
        model_input=model_input,
        client_output=client_output,
        telemetry_io=telemetry_io,
        detection=detection,
        content_integrity=content_integrity,
        stream_mode=stream_mode,
        sources=MappingProxyType(dict(sources)),
    )

    escalate = _get(app_policy, "escalate_residency_on_detection")
    version = app_policy.version() if hasattr(app_policy, "version") else ""

    return EffectivePolicy(
        placement=placement,
        protection=protection,
        escalate_residency_on_detection=True if escalate is None else bool(escalate),
        require_verification=False,
        version=version,
        rejected=tuple(rejected),
    )


def apply_signals(eff: EffectivePolicy, detected_entity_types: tuple[str, ...],
                  rule) -> EffectivePolicy:
    """Apply the one declared cross-axis effect — `config/policy.yaml`'s
    `sensitivity_rules` — to an already-compiled policy (design §5).

    **Raise-only.** It may tighten `placement.residency` and it may set
    `require_verification` true; it can never loosen either, and it never
    touches a protection axis. With no detected entities it is the identity.
    When the app has `escalate_residency_on_detection: false`, the rule still
    applies `require_verification` and sensitivity but must not move placement
    — the app's own residency floor stays exactly where the operator set it.
    """
    if not detected_entity_types or rule is None:
        return eff

    sensitivity = getattr(rule, "sensitivity", None)
    fired = eff.cross_axis_rules_fired + (f"rule:{sensitivity}",)

    placement = eff.placement
    if eff.escalate_residency_on_detection:
        rule_residency = getattr(rule, "residency", None)
        if (rule_residency in _RESIDENCY_RANK
                and _RESIDENCY_RANK[rule_residency]
                > _RESIDENCY_RANK[placement.residency]):
            # Only a rule that actually MOVED residency is named in
            # placement.sources — otherwise sources would claim an influence
            # that never happened. A rule that fired without moving placement
            # (it was already stricter, or escalation is off) is recorded in
            # cross_axis_rules_fired instead, which is what that field is for.
            placement = Placement(
                residency=rule_residency,   # tighten only; never the other way
                sources=placement.sources + (f"rule:{sensitivity}",),
            )

    return replace(
        eff,
        placement=placement,
        require_verification=(eff.require_verification
                              or bool(getattr(rule, "require_verification", False))),
        # SET from the matched rule, not max()'d against the current value: this
        # mirrors how app/policy/context.py resolves sensitivity today (one
        # matched rule decides the label). Not a monotonicity bug.
        sensitivity=sensitivity if sensitivity is not None else eff.sensitivity,
        cross_axis_rules_fired=fired,
    )
