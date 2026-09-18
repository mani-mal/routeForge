"""Hypothesis strategies and shared helpers for the noninterference harness
(design P1a §7).

What is generated, and why each part of the space is in it:

* **App policies across every axis, including values that are not legal.** The
  compiler's fail-closed behaviour (`_DENY`) is only interesting for inputs a
  validator should have refused, and `CE-2` is precisely a value that was never
  validated. Restricting the generators to legal enums would test the compiler
  only on inputs that cannot hurt anyone.
* **Deployments across both ceilings, including malformed ones.** Design §2.2:
  "a ceiling that cannot be parsed denies". A ceiling is read with `getattr`
  off a Pydantic model that other code may `model_copy(update=...)` without
  validation, so `"false"`, `None` and `1` are all reachable states of the
  process, not hypotheticals.
* **Detected-entity tuples drawn from `config/policy.yaml`'s own
  `sensitivity_rules`.** The one declared cross-axis path (design §5) is keyed
  on those entity names; inventing entity types would exercise only the
  fallback rule.

**The deterministic profile.** `derandomize=True` fixes the seed per test, so a
failure recorded in the research artifact reproduces exactly from the same
checkout, and a green run is green for a reason and not by luck of the draw.
The cost is that repeated runs re-explore the same inputs — so a wider,
randomised sweep is available on demand:

    HYPOTHESIS_PROFILE=deep uv run pytest tests/policy_compiler

Settings objects are memoised (`_settings`), because a Pydantic `model_copy`
chain per example dominates the runtime of an otherwise pure-function suite.
"""
from __future__ import annotations

import asyncio
import os

from hypothesis import HealthCheck, settings as hyp_settings
from hypothesis import strategies as st

from app.apps.policy import AppPolicyView
from app.config.loader import load_settings
from app.models import Entity
from app.policy.compile import compile_policy, apply_signals
from app.policy.context import build_context, match_sensitivity_rule

BASE = load_settings()

# --- the deterministic profile ----------------------------------------------

hyp_settings.register_profile(
    "policy_compiler",
    max_examples=200,
    deadline=None,          # the eligible-set properties call the real Router
    derandomize=True,       # reproducible: the research artifact cites inputs
    print_blob=True,        # a failure prints a @reproduce_failure blob
    suppress_health_check=[HealthCheck.too_slow],
)
hyp_settings.register_profile(
    "deep",
    max_examples=2000,
    deadline=None,
    derandomize=False,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
hyp_settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "policy_compiler"))


# --- axis vocabularies -------------------------------------------------------
#
# Deliberately spelled out here rather than imported from app.policy.compile:
# a harness that draws its alphabet from the module under test cannot notice
# the module dropping a value from an enum.

RESIDENCIES = ("any", "private_cloud_ok", "onprem_only")
POLICY_MODES = ("enforced", "passthrough")
MODEL_INPUTS = ("raw", "redact")
CLIENT_OUTPUTS = ("raw", "redact")
CAPTURE_IOS = ("off", "redacted", "full")
DETECTIONS = ("off", "regex", "presidio")
CONTENT_INTEGRITIES = ("exact", "transformations_allowed")
STREAM_MODES = ("off", "verified_then_stream", "stream_unverified",
                "incremental_verify")

#: Values no axis accepts. `"sometimes"` is CE-2's own counterexample; the rest
#: are the near-misses an operator or a migration script actually produces
#: (case drift, a YAML bool that stringified, an empty column).
INVALID_VALUES = ("sometimes", "", "ONPREM_ONLY", "Off", "true", "redacted ",
                  "null", "1")

#: Named in design §9 and deliberately not built. They must fail closed like
#: any other unparseable value AND be refused at write time with their own code.
UNIMPLEMENTED_VALUES = ("tokenize", "authorized_rehydrate", "resolve_tokens",
                        "trusted_raw", "inherit_model")

#: The per-app field name for each protection axis, paired with the attribute
#: it resolves to on `EffectivePolicy.protection`. The names differ on purpose
#: (`capture_io` -> `telemetry_io`, `pii_detection` -> `detection`), which is
#: itself a thing worth pinning: a rename on one side would break these tests.
PROTECTION_FIELDS = {
    "model_input": "model_input",
    "client_output": "client_output",
    "capture_io": "telemetry_io",
    "pii_detection": "detection",
    "content_integrity": "content_integrity",
    "stream_mode": "stream_mode",
}

PROTECTION_VALUES = {
    "model_input": MODEL_INPUTS,
    "client_output": CLIENT_OUTPUTS,
    "capture_io": CAPTURE_IOS,
    "pii_detection": DETECTIONS,
    "content_integrity": CONTENT_INTEGRITIES,
    "stream_mode": STREAM_MODES,
}

#: Tightening order per axis: rank 0 is the most disclosing / least protective
#: end, the highest rank the most protective. This is the partial order
#: invariant 1 ("tightening never enlarges what is permitted") is stated over,
#: and it is a claim in its own right:
#:
#: * `telemetry_io`: `full` (raw I/O leaves the boundary) < `redacted` < `off`.
#: * `detection`: `off` < `regex` < `presidio` — more entities found, more
#:   redacted, and (via the declared rule) more escalation.
#: * `stream_mode`: `stream_unverified` explicitly waives step 9;
#:   `incremental_verify` holds back entity text but cannot enforce a
#:   whole-response verdict; `verified_then_stream` keeps the guarantee whole;
#:   `off` emits nothing until the response is verified AND refuses the
#:   streaming contract entirely. (CLAUDE.md, "Inbound streaming".)
#: * `content_integrity`: `exact` disables lossy compression, so fewer
#:   transformations touch the data. It is a data control, not a privacy
#:   control — included so that a compiler that started letting it move
#:   something else would be caught, not because it protects anyone.
RANK = {
    "residency": {"any": 0, "private_cloud_ok": 1, "onprem_only": 2},
    "policy_mode": {"passthrough": 0, "enforced": 1},
    "model_input": {"raw": 0, "redact": 1},
    "client_output": {"raw": 0, "redact": 1},
    "capture_io": {"full": 0, "redacted": 1, "off": 2},
    "pii_detection": {"off": 0, "regex": 1, "presidio": 2},
    "content_integrity": {"transformations_allowed": 0, "exact": 1},
    "stream_mode": {"stream_unverified": 0, "incremental_verify": 1,
                    "verified_then_stream": 2, "off": 3},
}

#: `EffectivePolicy` attribute ranks, keyed by the compiled attribute name.
COMPILED_RANK = {
    "model_input": RANK["model_input"],
    "client_output": RANK["client_output"],
    "telemetry_io": RANK["capture_io"],
    "detection": RANK["pii_detection"],
    "content_integrity": RANK["content_integrity"],
    "stream_mode": RANK["stream_mode"],
}

#: Every entity type `config/policy.yaml`'s own `sensitivity_rules` names, in
#: file order. Read from the loaded config rather than hard-coded: a rule set
#: edited in YAML changes what this harness generates, which is the point.
RULE_ENTITIES = tuple(dict.fromkeys(
    e for rule in BASE.policy.sensitivity_rules for e in (rule.entities or ())))

#: The two entity types the rules escalate on, at each declared severity —
#: `US_SSN` -> restricted / onprem_only (CE-3's own case), `PERSON` ->
#: confidential / private_cloud_ok.
ESCALATING_ENTITY = "US_SSN"


# --- deployments -------------------------------------------------------------

def _settings(allow_passthrough, allow_pii_downgrade, default_policy_mode,
              global_pii, global_capture, cloud_enabled):
    """One `Settings` per distinct ceiling/default combination, memoised.

    `model_copy(update=...)` skips validation on purpose: that is exactly how a
    malformed ceiling reaches `compile_policy` in production (a mounted YAML
    key that stringified, an env override, a migration), and a strategy that
    could only produce validated values could never generate the case design
    §2.2 legislates for.

    The cache key carries each value's TYPE. An `@lru_cache` here is silently
    wrong for exactly the inputs this harness exists to generate: `1 == True`
    and `hash(1) == hash(True)`, so a cache keyed on the values alone hands the
    `allow_app_passthrough = 1` deployment the settings built for `True` — and
    the malformed-ceiling property then "passes" against a well-formed one.
    Hypothesis found that on the first run.
    """
    key = tuple((type(v).__name__, v) for v in
                (allow_passthrough, allow_pii_downgrade, default_policy_mode,
                 global_pii, global_capture, cloud_enabled))
    hit = _SETTINGS_CACHE.get(key)
    if hit is None:
        hit = _SETTINGS_CACHE[key] = _build_settings(
            allow_passthrough, allow_pii_downgrade, default_policy_mode,
            global_pii, global_capture, cloud_enabled)
    return hit


_SETTINGS_CACHE: dict = {}


def _build_settings(allow_passthrough, allow_pii_downgrade, default_policy_mode,
                    global_pii, global_capture, cloud_enabled):
    s = BASE
    langfuse = s.serving.observability.langfuse.model_copy(
        update={"capture_io": global_capture})
    obs = s.serving.observability.model_copy(update={"langfuse": langfuse})
    providers = {
        name: cfg.model_copy(update={"enabled": True if name == "onprem"
                                     else bool(cloud_enabled)})
        for name, cfg in s.serving.providers.items()}
    serving = s.serving.model_copy(update={
        "allow_app_passthrough": allow_passthrough,
        "allow_app_pii_downgrade": allow_pii_downgrade,
        "default_policy_mode": default_policy_mode,
        "pii_detection": global_pii,
        "observability": obs,
        "providers": providers,
    })
    return s.model_copy(update={"serving": serving})


#: Ceiling values that are not the literal `True` a ceiling must be to open a
#: permission. `"false"`/`"true"` are what a stringified YAML/env value looks
#: like; `1`/`0` what an int column gives; `None` a key the mounted config
#: never had.
MALFORMED_CEILINGS = (None, "true", "false", "yes", 1, 0, "")


def ceilings(*, malformed: bool = True):
    good = st.booleans()
    if not malformed:
        return good
    return st.one_of(good, good, st.sampled_from(MALFORMED_CEILINGS))


@st.composite
def deployments(draw, *, malformed_ceilings: bool = False,
                cloud_enabled: bool = True):
    """A deployment: both ceilings, the default posture, the global detector,
    the global capture mode, and whether the cloud providers are switched on."""
    return _settings(
        draw(ceilings(malformed=malformed_ceilings)),
        draw(ceilings(malformed=malformed_ceilings)),
        draw(st.sampled_from(POLICY_MODES)),
        draw(st.sampled_from(DETECTIONS)),
        draw(st.sampled_from(CAPTURE_IOS)),
        cloud_enabled,
    )


# --- app policies ------------------------------------------------------------

def _axis(good, *, invalid: bool = True, none: bool = True):
    values = list(good) + ([None] if none else [])
    legal = st.sampled_from(values)
    if not invalid:
        return legal
    # Three legal branches to one illegal one: invalid values are ~25% of
    # draws, enough to hit every fail-closed path without spending the whole
    # budget on inputs a validator refuses.
    bad = st.sampled_from(INVALID_VALUES + UNIMPLEMENTED_VALUES)
    return st.one_of(legal, legal, legal, bad)


@st.composite
def app_policies(draw, *, invalid: bool = True, explicit: bool = False):
    """An `AppPolicyView` across every axis the compiler resolves.

    `explicit=True` drops the `None` (inherit) option, which is what the
    monotonicity properties need: `None` has no position in the tightening
    order, so a comparison that included it would be comparing "inherit" with
    a value, not a looser setting with a tighter one.
    """
    none = not explicit
    return AppPolicyView(
        residency=draw(_axis(RESIDENCIES, invalid=invalid, none=False)),
        policy_mode=draw(_axis(POLICY_MODES, invalid=invalid, none=none)),
        model_input=draw(_axis(MODEL_INPUTS, invalid=invalid, none=none)),
        client_output=draw(_axis(CLIENT_OUTPUTS, invalid=invalid, none=none)),
        capture_io=draw(_axis(CAPTURE_IOS, invalid=invalid, none=none)),
        pii_detection=draw(_axis(DETECTIONS, invalid=invalid, none=none)),
        content_integrity=draw(_axis(CONTENT_INTEGRITIES, invalid=invalid,
                                     none=none)),
        stream_mode=draw(_axis(STREAM_MODES, invalid=invalid, none=none)),
        escalate_residency_on_detection=draw(
            st.sampled_from([True, False] + ([None] if none else []))),
    )


def valid_app_policies(**kw):
    return app_policies(invalid=False, **kw)


# --- detected entities -------------------------------------------------------

@st.composite
def entity_types(draw, *, allow_unknown: bool = True):
    """A tuple of detected entity TYPES (never values — the certificate carries
    types only, design §6). Drawn from `config/policy.yaml`'s own rules, plus
    an occasional unknown type so the fallback ("the public/empty rule") is
    exercised too."""
    pool = list(RULE_ENTITIES) + (["UNKNOWN_ENTITY"] if allow_unknown else [])
    return tuple(draw(st.lists(st.sampled_from(pool), max_size=3, unique=True)))


def protection_axes():
    """One protection axis and one legal value for it — the perturbation
    invariants 2 and 3 apply."""
    return st.sampled_from([(f, v) for f, vs in PROTECTION_VALUES.items()
                            for v in vs])


# --- the eligible candidate set ---------------------------------------------
#
# Invariants 1, 5 and 7 are about what the request path may REACH, not about a
# dataclass. They are therefore asserted through `build_context` and the real
# `Router` — the production placement path — rather than through a
# reimplementation of it here. Two registries, built once (each needs an
# asyncio run and an in-memory SQLite), because building one per example would
# dominate the suite's runtime.

CLOUD = _settings(True, True, "enforced", "regex", "off", True)


def _make_registries():
    from tests._litellm_fake import make_test_registry
    from app.apps.db import Database
    from app.apps.registry import ModelRegistry

    async def _empty():
        db = Database(url="sqlite+aiosqlite://")
        await db.ensure_ready()
        reg = ModelRegistry(CLOUD)
        await reg.refresh(db)
        return reg

    return make_test_registry(CLOUD), asyncio.run(_empty())


#: `ONPREM_REGISTRY` has the two seeded on-prem rows (local-8b/cheap,
#: local-32b/mid) healthy and enabled; `EMPTY_REGISTRY` has none, so
#: `has_healthy_onprem()` is false — IP-9's derived reject.
ONPREM_REGISTRY, EMPTY_REGISTRY = _make_registries()

CLOUD_PROVIDERS = frozenset({"azure", "bedrock"})


def context_for(view: AppPolicyView, deployment, entity_types_: tuple[str, ...],
                registry=ONPREM_REGISTRY, tenant: str = "acme"):
    """The `ExecutionPolicyContext` the request path would build for this app,
    this deployment and these detected entity types — i.e. the eligible
    candidate set, derived the way production derives it.

    `escalate_residency` is threaded from the COMPILED policy, exactly as
    `app/policy/gate.py` threads it, so a protection axis that leaked into the
    compiled escalation switch would show up here as a placement change.
    """
    eff = compile_policy(view, deployment)
    entities = [Entity(type=t, start=0, end=1, value="x") for t in entity_types_]
    return build_context(
        deployment, request_id="harness", tenant_id=tenant, entities=entities,
        redaction_map_id=None, app_policy=view, registry=registry,
        escalate_residency=eff.escalate_residency_on_detection)


def compiled_placement(view: AppPolicyView, deployment,
                       entity_types_: tuple[str, ...] = ()):
    """`compile_policy` + `apply_signals` over the SAME matched rule the
    context path uses, so the two can be compared."""
    eff = compile_policy(view, deployment)
    if not entity_types_:
        return eff
    rule = match_sensitivity_rule(deployment, None, set(entity_types_))
    return apply_signals(eff, entity_types_, rule)
