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
from typing import TYPE_CHECKING, Callable, Literal, Mapping

from app.config.schema import Settings
from app.policy.enums import (PII_MODES, POLICY_MODES, RESIDENCY, STREAM_MODES,
                              TOOL_ARGS_BAR_TYPES_DEFAULT)
from app.policy.trajectory import resolve_trajectory_mode

if TYPE_CHECKING:
    from app.apps.policy import AppPolicyView

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
    "content_integrity": "exact",
    "stream_mode": "off",
}

#: Every axis this module resolves. Kept beside `_DENY` so a future axis cannot
#: be added to one and forgotten in the other.
_AXES = ("residency", "policy_mode", "model_input", "client_output",
         "telemetry_io", "detection", "content_integrity", "stream_mode")
assert set(_AXES) == set(_DENY), "every axis needs a fail-closed deny value"

#: The legal vocabulary of each axis, so `validate_effective` can tell an
#: operator what they *may* write instead of only that their value was wrong.
#: Sorted at use, because two of these are frozensets.
_LEGAL = {
    "residency": RESIDENCY,
    "policy_mode": POLICY_MODES,
    "model_input": MODEL_INPUTS,
    "client_output": CLIENT_OUTPUTS,
    "telemetry_io": TELEMETRY_IOS,
    "detection": PII_MODES,
    "content_integrity": CONTENT_INTEGRITY,
    "stream_mode": STREAM_MODES,
}
assert set(_AXES) == set(_LEGAL), "every axis needs its legal vocabulary"

# Source labels, so every resolved value can name the rung of §2.2 it came from.

#: `Protection.sources[axis]` when the APP ITSELF named the axis (rung 2), as
#: opposed to a preset, a deployment ceiling or a deployment default. Public
#: because rules outside this module branch on it: a guardrail that fires only
#: for an explicitly-declared value — `validate_effective`'s `stream_unverified`
#: + `client_output: redact` refusal, and the matching runtime floor in
#: `Pipeline.run_stream` — must compare against the same literal the compiler
#: writes, or the write-time rule and the runtime backstop drift apart.
APP_SOURCE = "app_policy"

_APP = APP_SOURCE
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
    #: B69: the EFFECTIVE tool-argument bar -- the minimum detector score a
    #: span inside tool-call arguments needs to be rewritten:
    #: `max(app pii_score_threshold or 0, resolved tool_args_min_score)`.
    #: Rewriting only: detection, classification and message text never read
    #: it. `sources["tool_args_min_score"]` names where the resolved axis
    #: value came from (`app_policy` / `deployment_default` / `ceiling` when
    #: an unparseable stored value denied to 0.0), or `pii_score_threshold`
    #: when the app's threshold is what raised the effective bar.
    tool_args_min_score: float = 0.0
    #: B69: the resolved `tool_args_min_score` axis value BEFORE the app's
    #: `pii_score_threshold` is folded in -- what the save-time monotonicity
    #: invariant compares, since the effective value above can sit at the
    #: threshold for both extremes.
    tool_args_min_score_axis: float = 0.0
    #: B69: the entity types the bar applies to at all -- the deployment's
    #: `serving.yaml` `policy.tool_args_bar_types` (deployment-level only, no
    #: per-app list). Every other type is always rewritten in tool arguments.
    tool_args_bar_types: frozenset[str] = frozenset()


@dataclass(frozen=True)
class EffectivePolicy:
    """The compiled policy every consumer reads instead of re-deriving."""

    placement: Placement
    protection: Protection
    escalate_residency_on_detection: bool
    require_verification: bool
    version: str  # app_policy_version
    # --- P1b app-side data-handling requirements (design doc §3).
    # Placement-ADJACENT, not a protection axis: never read while computing
    # `placement` above, and never widens it. Resolved explicit-or-null --
    # neither has a `policy_mode` preset or a deployment default. `None` =
    # no regional constraint; `False` = no retention requirement. Tasks 3-5
    # apply the three exclusion rules against these; this module only
    # resolves and validates the shape.
    allowed_regions: frozenset[str] | None = None
    require_no_retention: bool = False
    #: B72: `off` | `header` | `sticky` (app/policy/trajectory.py). Raise-only
    #: like the cross-axis rule it feeds: it can add inherited entity types to
    #: a request's rule match, never remove one, and reads no protection axis.
    trajectory_mode: str = "off"
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
    #: `(axis, requested_value)` for every value that was **legal** but that a
    #: deployment ceiling refused, so the compiler substituted the protective
    #: one. `rejected`'s sibling, and needed for exactly the same reason:
    #: `sources[axis] == "ceiling"` is ambiguous three ways — the app asked for
    #: something the ceiling forbids, a *preset* implied it, or the deployment's
    #: own default simply sits at the value the ceiling names. Only the first is
    #: an operator error worth a 422; the last one is every app on a deployment
    #: whose `pii_detection` default is `regex` while `allow_app_pii_downgrade`
    #: is false (this repo's own test config), which is why a validator that
    #: keys on the *effective* value would refuse every write.
    #:
    #: So an entry is recorded only when the **app itself** asked (rung 2 of
    #: §2.2) — with one deliberate exception, `policy_mode`, which is recorded
    #: whether the app or the deployment default supplied `passthrough`. That
    #: asymmetry is not a slip: it is the one `set_policy` has always had, and
    #: it is load-bearing. `pii_detection` unset means "inherit the live global
    #: detector", which is not an opt-out of anything; `policy_mode` unset on a
    #: deployment with `default_policy_mode: passthrough` genuinely puts the app
    #: into the no-protection posture, and the ceiling must see that — an app is
    #: exactly as unprotected whether the passthrough came from its own row or
    #: from the default.
    clamped: tuple[tuple[str, str], ...] = ()
    #: B31: the app resolved to `capture_io: full` WITHOUT a declared
    #: no-protection posture (`has_no_protection_posture`), so the compiler
    #: denied telemetry to `redacted` -- the request still runs, it just never
    #: exports raw I/O. Kept as evidence for `validate_effective`, which refuses
    #: the WRITE with `CAPTURE_FULL_NEEDS_NO_PROTECTION`; a row stored before
    #: the rule applied at every residency is served as `redacted` until it is
    #: re-saved, never failed and never exported raw.
    capture_full_refused: bool = False


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


def resolve_residency(value) -> tuple[str, bool]:
    """Resolve one stored residency to `(residency, parsed)` — the single
    authority on what a stored residency MEANS.

    * `None` (unset) inherits `any`, exactly as every other nullable axis
      inherits, and counts as parsed.
    * A legal member of the axis resolves to itself.
    * **Anything else denies**, to `onprem_only` — the most restrictive
      placement — and reports `parsed=False` so the caller can record it in
      `rejected` and refuse the write (CLAUDE.md rule 5).

    Shared with `app/policy/context.py`, deliberately. Placement decided in two
    places is placement that can disagree with itself, and it did: the compiler
    denied `residency="ONPREM_ONLY"` to `onprem_only` while
    `_placement_providers`'s `if residency == "onprem_only" ... else: allowed =
    enabled` treated the same value as the LOOSEST placement and handed the
    request every enabled cloud provider. One function, one answer, so a value
    the compiler refuses can never be routed as `any`.
    """
    if value is None:
        return "any", True
    if value in RESIDENCY:
        return value, True
    return _DENY["residency"], False


def resolve_allowed_regions(value) -> tuple[frozenset[str] | None, bool]:
    """Resolve one stored `allowed_regions` to `(regions, parsed)` (design
    §3). There is no ceiling, preset or deployment default for this axis --
    it is explicit-or-null:

    * `None` (unset) means "no regional constraint" and counts as parsed.
    * A non-empty list/tuple/set of non-empty string tokens resolves to the
      stripped, lowercased set of those tokens -- matching the endpoint-side
      normalisation `ModelRegistry._resolve_data_handling` already applies to
      a declared `region` (Task 1), so an app's requirement and an endpoint's
      declaration compare on the same casing.
    * Anything else -- not list-like, empty, or containing a non-string /
      blank element -- **denies**: `parsed=False`, resolves to `frozenset()`
      (matches no region, the most restrictive reading available for an axis
      whose unset value is "no constraint"), and the caller records the
      offending value so the write can be refused (CLAUDE.md rule 5).
    """
    if value is None:
        return None, True
    if not isinstance(value, (list, tuple, frozenset, set)):
        return frozenset(), False
    tokens: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return frozenset(), False
        tokens.append(item.strip().lower())
    if not tokens:
        return frozenset(), False
    return frozenset(tokens), True


def resolve_tool_args_min_score(value, default) -> tuple[float, bool]:
    """Resolve one stored `tool_args_min_score` (B69) to `(score, parsed)`.

    * `None` (unset) inherits the deployment default and counts as parsed.
    * A real number in [0.0, 1.0] resolves to itself.
    * Anything else -- a bool, a string, NaN, out of range -- **denies** to
      0.0, this axis's protective end (a bar of 0 rewrites every detected
      span, today's pre-B69 behaviour), with `parsed=False` so the caller
      records it in `rejected` and the write is refused (CLAUDE.md rule 5).

    The deployment default is range-checked by the config lint at boot; a
    malformed one reaching here denies the same way.
    """
    def _ok(v) -> bool:
        return (isinstance(v, (int, float)) and not isinstance(v, bool)
                and 0.0 <= v <= 1.0)
    if value is None:
        return (float(default), True) if _ok(default) else (0.0, False)
    if _ok(value):
        return float(value), True
    return 0.0, False


# --- the compiler ------------------------------------------------------------

def _declares_no_protection(model_input: str, client_output: str,
                            detection: str, detection_source: str) -> bool:
    """The PLACEMENT-FREE half of `has_no_protection_posture`: raw/raw, or PII
    detection off by the app's own choice (its explicit `pii_detection: off`,
    or the `passthrough` preset's -- not an inherited deployment default).

    `compile_policy` uses only this half to deny an unpostured `full` to
    `redacted`, because a compiled protection value may never depend on
    residency (rule 2a; the noninterference invariant, design §7.3). The
    stricter `onprem_only` half is a write-time rule in `validate_effective`."""
    if model_input == "raw" and client_output == "raw":
        return True
    return detection == "off" and (detection_source == _APP
                                   or detection_source.startswith("preset:"))


def has_no_protection_posture(model_input: str, client_output: str,
                              detection: str, detection_source: str,
                              residency: str) -> bool:
    """Has the app DECLARED the no-protection posture raw telemetry capture
    (`capture_io: full`) requires? (CLAUDE.md rule 2.) Two cases:

    * **`onprem_only`**: `model_input == raw` AND `client_output == raw` --
      exactly the pre-B31 rule. Detection off alone does NOT qualify: residency
      beats eval (rule 8), and with detection off an app's custom recognizers
      can still redact the model input, so it has not declared itself
      unprotected.
    * **any other residency**: the same raw/raw, OR PII detection off by the
      app's own choice (`_declares_no_protection`).

    B31 only ever TIGHTENED: it added the check for non-`onprem_only` apps and
    left `onprem_only` exactly as it was. Like the pre-B31 rule, the residency
    half is enforced at WRITE time only (`validate_effective`); the compiled
    protection never reads residency."""
    if residency == "onprem_only":
        return model_input == "raw" and client_output == "raw"
    return _declares_no_protection(model_input, client_output, detection,
                                   detection_source)


def _posture_requirement(residency: str) -> str:
    """The posture `capture_io: full` needs at this residency, for messages."""
    if residency == "onprem_only":
        return ("model_input='raw' AND client_output='raw' (for an onprem_only "
                "app pii_detection='off' alone does not qualify)")
    return ("model_input='raw' AND client_output='raw', or pii_detection='off' "
            "set explicitly on the app (an 'off' inherited from the deployment "
            "default does not count)")


def compile_policy(app_policy, deployment: Settings) -> EffectivePolicy:
    """Compile one application's stored policy plus the deployment's settings
    into the single `EffectivePolicy` every consumer reads.

    Pure: given the same two inputs it returns an equal value forever. It
    touches no DB, no clock and no request, and it never inspects a protection
    axis while computing placement.
    """
    rejected: list[tuple[str, str]] = []
    clamped: list[tuple[str, str]] = []

    # -- placement. Residency, and only residency. -----------------------------
    # `None` means "unset" and inherits `any`, exactly as every other nullable
    # axis inherits. Anything else is parsed, and an unparseable value DENIES
    # like the rest of them. The distinction is load-bearing and used to be
    # missing: `_get(...) or "any"` resolved every FALSY stored value -- an
    # empty string from a migration, a `default_residency: ""` on an
    # application row (`CreateAppIn.default_residency` is a plain `str` with no
    # enum check) -- to the LOOSEST placement, silently, with nothing in
    # `rejected` for a validator to refuse the write on. Found by the Task 10
    # Hypothesis harness (tests/policy_compiler/test_counterexamples.py::
    # test_ce2_every_axis_fails_closed_and_is_refused).
    #
    # The resolution itself lives in `resolve_residency` below, because
    # `app/policy/context.py` needs the identical answer: the compiler and the
    # placement path must not be able to disagree about where one app's
    # compute may run (see that function's docstring).
    stored_residency = _get(app_policy, "residency")
    residency, parsed = resolve_residency(stored_residency)
    if parsed:
        placement_source = _APP if app_policy is not None else _DEFAULT
    else:
        rejected.append(("residency", str(stored_residency)))
        placement_source = _CEILING
    placement = Placement(residency=residency, sources=(placement_source,))

    # -- protection. Ceilings first (rung 1), then preset, then defaults. ------
    allow_passthrough = _flag(deployment, "allow_app_passthrough")
    allow_pii_downgrade = _flag(deployment, "allow_app_pii_downgrade")

    mode = _get(app_policy, "policy_mode") or deployment.serving.default_policy_mode
    if mode not in POLICY_MODES:
        rejected.append(("policy_mode", str(mode)))
        mode = _DENY["policy_mode"]            # unparseable posture denies
    if mode == "passthrough" and not allow_passthrough:
        # Recorded whether the app or `default_policy_mode` supplied it — see
        # the `clamped` docstring for why this axis, alone, ignores the source.
        clamped.append(("policy_mode", "passthrough"))
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
            if sources["model_input"] == _APP:
                clamped.append(("model_input", "raw"))
            model_input, sources["model_input"] = "redact", _CEILING
        if client_output == "raw":
            if sources["client_output"] == _APP:
                clamped.append(("client_output", "raw"))
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
        if sources["detection"] == _APP:
            # Only an actual per-app override is a downgrade the operator asked
            # for. Inheriting a non-presidio deployment default is not an
            # opt-out of anything, and a `preset:passthrough` "off" is governed
            # by `allow_app_passthrough`, not by this ceiling. Recording either
            # here would 422 writes that have always been legal.
            clamped.append(("detection", detection))
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
    # Raw capture needs a declared no-protection posture (B31). Without one,
    # deny to `redacted`. That normally has a detector to redact with, since
    # detection the APP turned off would itself be the posture. The exception
    # is detection off only by inheriting an "off" deployment default: then
    # `redacted` has no detector, and the export boundary
    # (`resolve_capture_io`) steps it down to `off` -- still never raw, and
    # the write is refused all the same. Only the placement-free half
    # is applied here (a compiled protection value never reads residency);
    # `validate_effective` adds the stricter onprem_only half at write time.
    # A write is refused on `capture_full_refused`; a stored row is served
    # redacted.
    capture_full_refused = False
    if telemetry_io == "full" and not _declares_no_protection(
            model_input, client_output, detection, sources["detection"]):
        capture_full_refused = True
        telemetry_io, sources["telemetry_io"] = "redacted", _CEILING

    content_integrity, sources["content_integrity"] = _resolve(
        "content_integrity", _get(app_policy, "content_integrity"),
        CONTENT_INTEGRITY, None, None, "transformations_allowed", rejected)

    stream_mode, sources["stream_mode"] = _resolve(
        "stream_mode", _get(app_policy, "stream_mode"), STREAM_MODES,
        None, None, "off", rejected)

    # B69: the tool-argument bar. A float axis with no preset and no ceiling:
    # explicit per-app value, else the deployment default. The EFFECTIVE bar
    # folds in the app's `pii_score_threshold`, which already culls every
    # scored match below it everywhere (FilteredDetector) -- so the bar the
    # certificate reports is the one that actually applied.
    stored_bar = _get(app_policy, "tool_args_min_score")
    bar, bar_parsed = resolve_tool_args_min_score(
        stored_bar, getattr(deployment.serving, "tool_args_min_score", 0.5))
    if not bar_parsed:
        rejected.append(("tool_args_min_score", str(stored_bar)))
        sources["tool_args_min_score"] = _CEILING
    else:
        sources["tool_args_min_score"] = _APP if stored_bar is not None else _DEFAULT
    bar_axis = bar
    threshold = _get(app_policy, "pii_score_threshold")
    if (isinstance(threshold, (int, float)) and not isinstance(threshold, bool)
            and 0.0 <= threshold <= 1.0 and threshold > bar):
        bar = float(threshold)
        sources["tool_args_min_score"] = "pii_score_threshold"
    # Deployment-level only; the config lint guarantees no PROTECTED type is
    # in it, and `below_tool_arg_bar` never exempts one regardless.
    bar_types = frozenset(getattr(deployment.serving, "tool_args_bar_types",
                                  TOOL_ARGS_BAR_TYPES_DEFAULT) or ())

    protection = Protection(
        model_input=model_input,
        client_output=client_output,
        telemetry_io=telemetry_io,
        detection=detection,
        content_integrity=content_integrity,
        stream_mode=stream_mode,
        sources=MappingProxyType(dict(sources)),
        tool_args_min_score=bar,
        tool_args_min_score_axis=bar_axis,
        tool_args_bar_types=bar_types,
    )

    escalate = _get(app_policy, "escalate_residency_on_detection")
    version = app_policy.version() if hasattr(app_policy, "version") else ""

    # -- P1b app-side data-handling requirements (design §3). Explicit-or-null,
    # read AFTER placement/protection are already decided and never fed back
    # into either -- keeps them placement-adjacent rather than a placement
    # input (CLAUDE.md rule 3 is about providers/onprem_only; these are a
    # narrower, separate axis Tasks 3-5 enforce against the registry).
    stored_allowed_regions = _get(app_policy, "allowed_regions")
    allowed_regions, ar_parsed = resolve_allowed_regions(stored_allowed_regions)
    if not ar_parsed:
        rejected.append(("allowed_regions", str(stored_allowed_regions)))

    require_no_retention = bool(_get(app_policy, "require_no_retention"))

    stored_trajectory_mode = _get(app_policy, "trajectory_mode")
    trajectory_mode, tm_parsed = resolve_trajectory_mode(stored_trajectory_mode)
    if not tm_parsed:
        rejected.append(("trajectory_mode", str(stored_trajectory_mode)))

    return EffectivePolicy(
        placement=placement,
        protection=protection,
        escalate_residency_on_detection=True if escalate is None else bool(escalate),
        require_verification=False,
        version=version,
        rejected=tuple(rejected),
        clamped=tuple(clamped),
        capture_full_refused=capture_full_refused,
        allowed_regions=allowed_regions,
        require_no_retention=require_no_retention,
        trajectory_mode=trajectory_mode,
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


# --- validation (design §4: the compatibility contract) ----------------------

class PolicyViolation(Exception):
    """One named reason an effective policy may not be persisted.

    `code` is the stable identifier the golden table asserts on; `message` is
    what the operator reads in the admin API's 422. Every message names the
    axis, why it is refused, and the legal alternatives — these strings have
    been in front of operators since the per-app config shipped, so their
    wording is API surface, not prose.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


#: Axis name -> the name the operator types in the admin API / console. The
#: compiler's internal names (`telemetry_io`, `detection`) are deliberately
#: different from the stored column names (`capture_io`, `pii_detection`); an
#: error message that used the internal one would name a field the operator
#: cannot find.
_OPERATOR_NAME = {
    "residency": "residency",
    "policy_mode": "policy_mode",
    "model_input": "model_input",
    "client_output": "client_output",
    "telemetry_io": "capture_io",
    "detection": "pii_detection",
    "content_integrity": "content_integrity",
    "stream_mode": "stream_mode",
}


def _capture_alternatives(posture: bool) -> str:
    """The legal `capture_io` values to offer when `redacted` is refused.

    `full` is offered only when the app has declared a no-protection posture
    (`has_no_protection_posture`) -- raw capture needs it at every residency,
    so naming `full` otherwise would send the operator straight into the next
    422.
    """
    if not posture:
        return "capture_io='off'."
    return "capture_io='full' with the app's own Langfuse project, or capture_io='off'."


def validate_effective(eff: EffectivePolicy, deployment: Settings, *,
                       has_own_langfuse_project: bool | Callable[[], bool]
                       ) -> None:
    """Refuse an effective policy that may not be persisted, or return None.

    This is the single authority the admin API and the golden table both call,
    replacing the hand-written clamps `set_policy` used to carry. Two properties
    of those clamps are preserved deliberately and must survive any edit here:

    * **It judges the compiled, post-write state**, never the request body. A
      partial PUT that never mentions `stream_mode` still gets judged on the
      stream mode the app will have afterwards, and a `reset` is judged on what
      the app falls back to. `set_policy` builds the post-write view; this
      function never sees the body at all.
    * **Each violation is its own named code**, so the operator is told which
      guardrail fired. `capture_io='full'` failing the own-project condition and
      failing the posture condition are different problems with different fixes.

    Order matters where two rules can fire at once, and it follows the golden
    table: unparseable values first (the operator's typo is the most useful
    thing to report), then deployment ceilings, then the capture rules, then the
    streaming rules.

    `has_own_langfuse_project` may be a bool or a zero-argument callable, and a
    callable is **only invoked when `telemetry_io` resolves to `full`** — the
    one rule that needs it. That laziness is not a micro-optimisation: the
    caller's answer comes from decrypting stored credentials, which can raise on
    ciphertext left behind by a key rotation. Resolving it eagerly would turn
    every unrelated policy write (a `max_rpm` edit, say) into a 500 for that
    app. Whatever the caller passes stays DB-derived; this function never infers
    it from the policy.
    """
    p = eff.protection
    residency = eff.placement.residency

    # --- 1. values that could not be parsed. `compile_policy` already failed
    # closed to the protective value; refusing the *write* is what stops a typo
    # from being answered 200 and stored as a policy the operator never asked
    # for (CE-2: `capture_io` was never validated at all, so "sometimes" was
    # persisted and silently behaved as "off").
    for axis, value in eff.rejected:
        name = _OPERATOR_NAME.get(axis, axis)
        if axis == "tool_args_min_score":
            raise PolicyViolation(
                "TOOL_ARGS_MIN_SCORE_INVALID",
                f"bad tool_args_min_score {value!r}; legal values: a number "
                "from 0.0 to 1.0. To inherit the deployment default send "
                'reset: ["tool_args_min_score"].')
        if axis == "allowed_regions":
            # No enum to offer (design §3 has no _LEGAL vocabulary for this
            # axis, unlike the P1a rungs below) -- the message names the
            # field and spells out the legal SHAPE instead.
            raise PolicyViolation(
                "ALLOWED_REGIONS_INVALID",
                f"bad allowed_regions {value!r}; legal shape: a non-empty "
                "list of non-empty region tokens (strings), e.g. "
                '["us", "eu"]. Tokens are normalised to lowercase. An empty '
                'list is refused; to remove the constraint send '
                'reset: ["allowed_regions"].')
        if value in UNIMPLEMENTED:
            # Design §9: named in the design, deliberately not built. Rejected
            # rather than faked — a policy that asks for tokenization must not
            # be accepted and then silently run as plain redaction.
            raise PolicyViolation(
                "AXIS_VALUE_NOT_IMPLEMENTED",
                f"{name}={value!r} is not implemented in this release. It is a "
                "named part of the design, but nothing behind this API does it "
                "yet, so accepting it would promise a protection that does not "
                "run. Use one of: "
                f"{', '.join(repr(v) for v in sorted(_LEGAL[axis]))}.")
        raise PolicyViolation(
            f"{name.upper()}_UNKNOWN_VALUE",
            f"bad {name} {value!r}; legal values: "
            f"{', '.join(repr(v) for v in sorted(_LEGAL[axis]))}.")

    # --- 2. deployment ceilings. A locked-down deployment forbids the posture
    # install-wide, so no app may reach it — including by way of the
    # deployment's own default (see `EffectivePolicy.clamped` for why
    # `policy_mode` keys on the effective value while `pii_detection` keys on
    # the app's own override; both halves of that asymmetry are load-bearing).
    for axis, value in eff.clamped:
        if axis in ("policy_mode", "model_input", "client_output"):
            raise PolicyViolation(
                "PASSTHROUGH_FORBIDDEN",
                "per-app passthrough policy_mode disabled on this deployment "
                f"(refused {_OPERATOR_NAME[axis]}={value!r}; "
                "serving.policy.allow_app_passthrough is false).")
        if axis == "detection":
            raise PolicyViolation(
                "PII_DOWNGRADE_FORBIDDEN",
                "per-app PII downgrade disabled on this deployment "
                f"(refused pii_detection={value!r}; "
                "serving.policy.allow_app_pii_downgrade is false).")

    # --- 3. telemetry capture. `compile_policy` has already denied a `full`
    # without the posture to `redacted`; `capture_full_refused` is how this
    # rule still sees what the app asked for.
    posture = has_no_protection_posture(p.model_input, p.client_output,
                                        p.detection, p.sources.get("detection", ""),
                                        residency)
    if p.telemetry_io == "full" or eff.capture_full_refused:
        # (a) Own provisioned Langfuse project — NEVER relaxed, by anything.
        # Raw I/O stays tenant-isolated: one customer's raw prompts must never
        # land in another's traces or in the shared project (CLAUDE.md rule 2).
        # No posture, and no residency, touches this condition. Resolved here
        # and nowhere else, so a caller that has to decrypt something to answer
        # is only asked when the answer can change the verdict.
        own_project = (has_own_langfuse_project() if callable(has_own_langfuse_project)
                       else has_own_langfuse_project)
        if not own_project:
            raise PolicyViolation(
                "CAPTURE_FULL_NEEDS_OWN_PROJECT",
                "capture_io='full' stores raw telemetry and is allowed only "
                "for an application with its own provisioned Langfuse project "
                "(raw I/O must never reach the shared project; CLAUDE.md rule 2). "
                "Use 'redacted'.")
        # (b) Posture, at ANY residency (B31). Before B31 only `onprem_only`
        # apps were checked, which left an `enforced` app elsewhere free to
        # hold raw capture. B31 only tightens: `onprem_only` keeps its stricter
        # raw/raw rule, every other residency now needs a posture too
        # (`has_no_protection_posture` holds both cases).
        if eff.capture_full_refused or not posture:
            where = ("an onprem_only application" if residency == "onprem_only"
                     else "an application")
            raise PolicyViolation(
                "CAPTURE_FULL_NEEDS_NO_PROTECTION",
                "capture_io='full' stores raw telemetry and is allowed for "
                f"{where} only once it has declared a no-protection posture — "
                f"{_posture_requirement(residency)} (CLAUDE.md rules 2/8). Use "
                "'redacted', or set policy_mode='passthrough' (which resolves "
                "both axes to 'raw') to declare the app unprotected.")

    if p.telemetry_io == "redacted" and p.detection == "off":
        # Redacted telemetry needs something to redact with. This is the one
        # rule design §4 narrows rather than keeps whole: it used to refuse
        # `redacted` for any no-protection posture, because the exporter reused
        # whatever view the gate handed the MODEL. §2.6 gives telemetry its own
        # sanitizer, so raw model input no longer implies raw telemetry — what
        # is left, and all that was ever really being protected against, is
        # having no detector at all.
        if p.sources.get("detection", "").startswith("preset:"):
            # The posture, not an explicit axis, is what turned detection off —
            # name that, so the operator fixes the right field.
            raise PolicyViolation(
                "CAPTURE_REDACTED_NEEDS_A_DETECTOR",
                "capture_io='redacted' cannot be combined with "
                "policy_mode='passthrough': passthrough leaves PII detection "
                "off, so there is nothing to redact telemetry with. Set "
                "pii_detection='regex' or 'presidio' to keep redacted "
                "telemetry, or use " + _capture_alternatives(posture))
        raise PolicyViolation(
            "CAPTURE_REDACTED_NEEDS_A_DETECTOR",
            "capture_io='redacted' cannot be combined with pii_detection "
            "off: redacted telemetry needs a detector to redact with, and "
            "none is configured. Use " + _capture_alternatives(posture))

    # --- 4. streaming. `stream_unverified` streams real backend tokens and
    # explicitly waives step 9, so it can only ever be an app's own, narrow
    # opt-in.
    if p.stream_mode == "stream_unverified":
        if residency == "onprem_only":
            raise PolicyViolation(
                "STREAM_UNVERIFIED_FORBIDDEN_ONPREM",
                "stream_unverified not allowed for onprem_only apps: the "
                "step-9 floor is absolute for them and can never be waived "
                "(CLAUDE.md rule 8). Use stream_mode='verified_then_stream' or "
                "'incremental_verify'.")
        if p.sources.get("client_output") == _APP and p.client_output == "redact":
            # Design §2.5. Scoped to an EXPLICIT client_output, deliberately:
            # `redact` is also what the `enforced` preset resolves to, and
            # `enforced` + `stream_unverified` is legal today for every app that
            # has opted into it. Widening this to the effective value would
            # refuse that whole population — a contract change, not this rule.
            # An app that named the axis has asked for an egress guarantee this
            # stream mode cannot keep, and that contradiction is worth a 422.
            raise PolicyViolation(
                "STREAM_UNVERIFIED_FORBIDDEN_REDACTED_OUTPUT",
                "stream_mode='stream_unverified' cannot be combined with an "
                "explicit client_output='redact': unverified streaming emits "
                "backend tokens as they arrive, so no egress redaction can run "
                "over the complete response. Use "
                "stream_mode='incremental_verify' (hold-back redaction) or "
                "'verified_then_stream', or set client_output='raw'.")


# --- save-time invariant check (design §7) ------------------------------------
#
# `validate_effective` above judges ONE compiled policy against the
# compatibility contract. It cannot, by itself, tell whether the *compiler*
# that produced that one `EffectivePolicy` still respects the properties the
# whole design rests on -- monotonicity, noninterference, and "placement never
# weakens protection" (design §7, invariants 1-3). Those are properties of
# `compile_policy`/`apply_signals` as functions, not of a single output, so
# checking them means calling the compiler again, several times, over nearby
# points -- a small, fixed, bounded neighbourhood around the policy an
# operator is about to save, never the whole input space (that is the
# Hypothesis harness's job, off the write path).
#
# `check_invariants` is deliberately NOT a re-implementation of
# `compile_policy`/`apply_signals` that would trivially agree with them. Two
# of its three checks compare the compiler's actual output against an
# expectation `check_invariants` derives independently:
#   * the residency/entity check re-derives which `sensitivity_rules` entry
#     must fire by reading `deployment.policy.sensitivity_rules` directly
#     (not via `apply_signals`), so a broken `apply_signals` that fails to
#     raise placement disagrees with it;
#   * the "unnamed placement change" check inspects `cross_axis_rules_fired`
#     and `placement.sources`, which a compiler that moves placement without
#     declaring why would fail regardless of what value it moved to.
# The one check that does compare two `compile_policy` outputs to each other
# (protection-axis noninterference, residency-sweep monotonicity) is still
# meaningful: it fails if two DIFFERENT calls disagree with each other, not
# just with themselves.

#: Residency values in tightening order -- `any` is the loosest placement,
#: `onprem_only` the strictest. Reused from `_RESIDENCY_RANK` above.
_RESIDENCY_BY_RANK: tuple[str, ...] = tuple(
    sorted(_RESIDENCY_RANK, key=_RESIDENCY_RANK.get))

#: `(AppPolicyView field, loose value, strict value)` for every protection
#: axis this module resolves. "Loose" is the more-disclosing/less-protective
#: end; "strict" the more-protective end. Used two ways: (a) to probe that a
#: protection axis alone can never move placement, and (b) to probe that
#: asking for the strict end never resolves to something less protective than
#: asking for the loose end.
_PROTECTION_EXTREMES: tuple[tuple[str, str | float, str | float], ...] = (
    ("model_input", "raw", "redact"),
    ("client_output", "raw", "redact"),
    ("capture_io", "full", "off"),
    ("pii_detection", "off", "presidio"),
    ("content_integrity", "transformations_allowed", "exact"),
    ("stream_mode", "stream_unverified", "off"),
    # B69: a higher bar rewrites fewer spans inside tool-call arguments, so
    # 1.0 is the loose end and 0.0 (rewrite everything detected) the strict.
    ("tool_args_min_score", 1.0, 0.0),
)

#: `AppPolicyView` field name -> the `Protection` attribute it resolves to.
#: Needed because the stored column names and the compiler's internal axis
#: names deliberately differ (see `_OPERATOR_NAME` above).
_FIELD_TO_PROTECTION_ATTR = {
    "model_input": "model_input",
    "client_output": "client_output",
    "capture_io": "telemetry_io",
    "pii_detection": "detection",
    "content_integrity": "content_integrity",
    "stream_mode": "stream_mode",
    # The raw resolved axis, not the effective bar: the effective value folds
    # in `pii_score_threshold`, which can pin both extremes to the threshold
    # and make the monotonicity check below compare nothing.
    "tool_args_min_score": "tool_args_min_score_axis",
}

#: A fixed, small set of detected-entity signals, ordered from no signal to
#: the two entities `config/policy.yaml`'s own `sensitivity_rules` name at
#: increasing severity (`PERSON` -> confidential/private_cloud_ok, `US_SSN`
#: -> restricted/onprem_only) -- exercising exactly the escalation path
#: counterexample CE-3 broke.
_ENTITY_TUPLES: tuple[tuple[str, ...], ...] = ((), ("PERSON",), ("US_SSN",))

#: The fixed, bounded grid the save-time check compiles over for the one app
#: being saved: the full residency axis (3, "the residency neighbours" of
#: any point on it), both extremes of every protection axis (14), and the
#: three detected-entity signals (3) -- 20 points total. A module-level
#: constant, built once at import time from literals: no I/O, no dependence
#: on the app or deployment being checked, so the write path's cost is the
#: same for every app and every write. `test_policy_save_validation.py`
#: asserts `len(_INVARIANT_GRID) <= 64`.
_INVARIANT_GRID: tuple[Mapping[str, object], ...] = (
    tuple({"residency": r} for r in _RESIDENCY_BY_RANK)
    + tuple({"axis": axis, "value": value}
            for axis, loose, strict in _PROTECTION_EXTREMES
            for value in (loose, strict))
    + tuple({"entities": e} for e in _ENTITY_TUPLES)
)


def _match_rule_for_check(deployment: Settings, entity_types: tuple[str, ...]):
    """The declared cross-axis rule for a set of detected entity types, read
    directly off `deployment.policy.sensitivity_rules` -- independent of
    `apply_signals`. This is what makes the escalation check below able to
    catch a broken `apply_signals`: if it instead asked `apply_signals` which
    rule should apply, a compiler bug in `apply_signals` would agree with
    itself by construction, and the check would prove nothing. Pure: no I/O,
    only reads the already-loaded config."""
    rules = deployment.policy.sensitivity_rules
    if not rules:
        return None
    etypes = set(entity_types)
    for rule in rules:
        if rule.entities and etypes & set(rule.entities):
            return rule
    return rules[-1]  # the public/empty rule


def check_invariants(view: "AppPolicyView", deployment: Settings) -> None:
    """Compile one application's policy over the fixed `_INVARIANT_GRID` and
    raise `PolicyViolation(code="INVARIANT_...")` the instant a save would
    persist a policy that breaks monotonicity, noninterference, or "placement
    never weakens protection" (design §7, invariants 1-3). Called from
    `set_policy` immediately after `validate_effective`, on the same
    post-write `AppPolicyView` -- so a policy that is individually legal by
    the compatibility contract but would make the *compiler* misbehave for
    this app still cannot be saved.

    Bounded and I/O-free by construction: `_INVARIANT_GRID` is a module-level
    constant (20 points), and every grid point is one `compile_policy` call
    (pure, no DB/clock/request) plus, for the three entity points, one
    `apply_signals` call. The admin write path pays a small, fixed number of
    extra pure-function calls, never unbounded work.
    """
    base = compile_policy(view, deployment)
    base_rank = _RESIDENCY_RANK.get(base.placement.residency)

    for point in _INVARIANT_GRID:
        if "residency" in point:
            requested = point["residency"]
            variant = compile_policy(replace(view, residency=requested), deployment)
            # Invariant 1 (monotonicity): a *legal, explicit* residency value
            # must resolve to itself -- compile_policy only ever denies a
            # residency it cannot parse, and every grid value is a legal
            # member of the axis, so a resolved value at a looser rank than
            # requested would mean a residency request got silently widened.
            got_rank = _RESIDENCY_RANK.get(variant.placement.residency)
            if got_rank is None or got_rank < _RESIDENCY_RANK[requested]:
                raise PolicyViolation(
                    "INVARIANT_RESIDENCY_NOT_HONORED",
                    f"requesting residency={requested!r} compiled to "
                    f"{variant.placement.residency!r}, which is a looser "
                    "placement than requested -- tightening residency must "
                    "never enlarge what is permitted (design §7.1).")
            # Invariant 3: changing residency must leave every protection
            # axis exactly as it was.
            if variant.protection != base.protection:
                raise PolicyViolation(
                    "INVARIANT_RESIDENCY_ALTERED_PROTECTION",
                    f"changing residency to {requested!r} changed protection "
                    f"({base.protection!r} -> {variant.protection!r}); "
                    "placement must never alter a protection value "
                    "(design §7.3).")
            continue

        if "axis" in point:
            axis, value = point["axis"], point["value"]
            variant = compile_policy(replace(view, **{axis: value}), deployment)
            # Invariant 2 (noninterference): a protection axis alone -- no
            # detected entities in play -- must never move placement.
            if variant.placement.residency != base.placement.residency:
                raise PolicyViolation(
                    "INVARIANT_PROTECTION_MOVED_PLACEMENT",
                    f"setting {axis}={value!r} changed placement.residency "
                    f"from {base.placement.residency!r} to "
                    f"{variant.placement.residency!r}; a protection axis "
                    "must never move placement (design §7.2).")
            continue

        # "entities" point: the one declared cross-axis path (design §5).
        entities = point["entities"]
        rule = _match_rule_for_check(deployment, entities)
        if rule is None:
            continue
        after = apply_signals(base, entities, rule)

        expected_rank = base_rank
        if base.escalate_residency_on_detection:
            rule_rank = _RESIDENCY_RANK.get(getattr(rule, "residency", None))
            if rule_rank is not None:
                expected_rank = max(expected_rank, rule_rank)
        after_rank = _RESIDENCY_RANK.get(after.placement.residency)

        # Invariant 1 (monotonicity), grounded in the DECLARED rule rather
        # than in `apply_signals`'s own opinion of itself: detecting a more
        # severe entity must never resolve to a looser placement than the
        # rule requires. This is the check that catches a compiler bug that
        # silently drops a tightening rule (a broken `apply_signals` that
        # returns its input unchanged, for instance) -- such a bug agrees
        # with itself on every other invariant here, because nothing else in
        # this function recomputes "what should have happened" without
        # calling `apply_signals`.
        if after_rank is None or after_rank < expected_rank:
            raise PolicyViolation(
                "INVARIANT_ESCALATION_NOT_APPLIED",
                f"detected entities {entities!r} must escalate placement to "
                f"at least {_RESIDENCY_BY_RANK[expected_rank]!r} per the "
                f"declared sensitivity_rules entry (sensitivity="
                f"{getattr(rule, 'sensitivity', None)!r}), but the compiled "
                f"policy resolved to {after.placement.residency!r} "
                "(design §5, §7.1).")

        # Invariant 2: a placement change must be named -- both in
        # `cross_axis_rules_fired` and in `placement.sources`, never a
        # silent side effect of a protection setting or anything else.
        if after_rank > base_rank:
            sensitivity = getattr(rule, "sensitivity", None)
            fired_name = f"rule:{sensitivity}"
            if (fired_name not in after.cross_axis_rules_fired
                    or fired_name not in after.placement.sources):
                raise PolicyViolation(
                    "INVARIANT_UNNAMED_PLACEMENT_CHANGE",
                    f"placement moved from {base.placement.residency!r} to "
                    f"{after.placement.residency!r} for detected entities "
                    f"{entities!r} without the firing rule named in both "
                    "cross_axis_rules_fired and placement.sources (design "
                    "§5, §7.2).")

        # Invariant 3: applying the entity signal must never alter
        # protection -- the declared cross-axis rule moves placement only.
        if after.protection != base.protection:
            raise PolicyViolation(
                "INVARIANT_PLACEMENT_ALTERED_PROTECTION",
                f"applying detected entities {entities!r} changed protection "
                f"({base.protection!r} -> {after.protection!r}); a placement "
                "change must never alter a protection value (design §7.3).")

    # Invariant 1, protection axes: asking for the strict end of an axis must
    # never resolve to a less protective value than asking for the loose end.
    for axis, loose, strict in _PROTECTION_EXTREMES:
        attr = _FIELD_TO_PROTECTION_ATTR[axis]
        loose_eff = compile_policy(replace(view, **{axis: loose}), deployment)
        strict_eff = compile_policy(replace(view, **{axis: strict}), deployment)
        loose_value = getattr(loose_eff.protection, attr)
        strict_value = getattr(strict_eff.protection, attr)
        # Both resolved values must land on the axis's own (loose, strict)
        # pair for rank to be comparable; a legal, explicit request that a
        # ceiling clamps still lands on `strict` (ceilings only ever deny
        # towards the protective end -- see `_resolve`/`_DENY` above), so
        # this holds even for a deployment whose ceilings are locked down.
        if isinstance(loose, float):
            # A numeric axis (B69's tool-argument bar): lower is stricter.
            # Compared directly rather than by rank, so no resolved value can
            # fall outside the pair and be skipped silently.
            less_protective = float(strict_value) > float(loose_value)
        else:
            rank = {loose: 0, strict: 1}
            if loose_value not in rank or strict_value not in rank:
                continue
            less_protective = rank[strict_value] < rank[loose_value]
        if less_protective:
            raise PolicyViolation(
                "INVARIANT_PROTECTION_AXIS_NOT_MONOTONIC",
                f"requesting {axis}={strict!r} (the strict end) resolved to "
                f"{attr}={strict_value!r}, less protective than "
                f"{axis}={loose!r} resolving to {attr}={loose_value!r}; "
                "tightening an axis must never enlarge what is permitted "
                "(design §7.1).")
