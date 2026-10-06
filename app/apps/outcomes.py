"""Pure, content-free builders for `routing_outcomes` rows (P2b-2 Task 2).

CONTENT-FREE BY CONTRACT: every value a function in this module puts into an
outcome row is an id, an enum member, a number, an entity TYPE (never an
entity VALUE), or a version hash. Nothing here ever reads or copies
prompt/completion text, tool-call arguments, or any DecisionRecord /
RejectedCandidate / VerificationRecord `reason` string / upstream error
text -- `rejected` rows keep only `name` and `code` (never `reason`,
`provider`, `tier` or `display_name`). This module must never import
`app/eval`.

Every function here is a pure dict-in/scalar-in, dict-out/scalar-out
transform: no I/O, no database, no clock reads. `app/apps/models.py`'s
`RoutingOutcome` owns the column names the returned dicts key on -- read it
before changing a key here. Callers (a later task) are responsible for
calling `parse_workload` once per request and threading its result into
`build_ok_outcome`/`build_failure_outcome`, and for handing `write_outcome`
(app/apps/store.py) the dict this module returns.
"""
from __future__ import annotations

import math
import re

# --- constants --------------------------------------------------------

STATUSES = ("ok", "backend_failed", "verify_failed", "no_eligible", "context_overflow")
VERIFY_MODES = ("not_required", "verified", "failed", "waived_stream", "incremental")
BUCKETS = ("<1k", "1k-4k", "4k-16k", "16k-64k", ">64k")

# Client-declared workload tag: lower-cased, 1-64 chars, alnum plus
# `._-`, first char alnum. A 65-char (or longer) tag, or anything with
# whitespace/markup, fails to match and is dropped rather than truncated.
_WORKLOAD_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# `outcome_feedback.value_cat` shape (P2b-2 Task 5): the same shape a
# Langfuse categorical score value is expected to have -- an unparseable
# value is refused outright, never truncated or coerced.
# `/` is allowed so a `preferred_model` score can name a slashed model slug
# (e.g. `nvidia/llama-3.1-nemotron-nano-8b-v1`); 100 chars matches
# `OutcomeFeedback.value_cat` and `RoutingOutcome.chosen`.
_FEEDBACK_VALUE_CAT_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,100}$")


def feedback_values(score_name: str, value: float | str
                    ) -> tuple[float | None, str | None] | None:
    """`(value_num, value_cat)` for one `outcome_feedback` row from a
    `/v1/feedback`-shaped `value`. A string becomes `value_cat` -- checked
    against `_FEEDBACK_VALUE_CAT_RE`; a value that fails is refused (`None`
    is returned for the whole pair, not a truncated/coerced string) rather
    than stored. Anything else becomes `value_num` as a plain float --
    refused (`None`) when it is not finite (NaN or +/-inf), which no score
    means and which would poison every mean the report takes.

    `score_name` is accepted for callers' symmetry with `post_score`/
    `SCORE_NAMES` but does not change the parse -- whether a value is
    numeric or categorical is read off `value`'s own Python type, matching
    `post_score`'s own `isinstance(value, str)` split."""
    if isinstance(value, str):
        return (None, value) if _FEEDBACK_VALUE_CAT_RE.fullmatch(value) else None
    num = float(value)
    return (num, None) if math.isfinite(num) else None


def _validate_tag(raw: str) -> str | None:
    """Lower-case `raw` and return it if it matches `_WORKLOAD_RE`, else
    `None`. The single place both the header and metadata sources run their
    value through, so they can never accept different shapes."""
    lowered = raw.lower()
    return lowered if _WORKLOAD_RE.fullmatch(lowered) else None


def parse_workload(header: str | None, metadata: object) -> tuple[str | None, str]:
    """`(workload, workload_source)` from a client-supplied header and/or a
    request `metadata` value, where `workload_source` is `client`, `none` or
    `invalid`.

    Precedence: a non-`None` header is authoritative -- if present it is
    validated (lower-cased, matched against `_WORKLOAD_RE`) and that is the
    whole decision; metadata is never consulted once a header was supplied,
    valid or not. With no header, `metadata` is consulted only when it is a
    dict: a present-but-malformed `metadata` (not a dict at all, or a
    `"workload"` value that is not a string) is `invalid` -- it was
    *supplied* but unusable, the same verdict a malformed header gets. A
    dict with no `"workload"` key, or `metadata is None`, means nothing was
    supplied at all -- `none`. Never raises: any malformed input is either
    `invalid` or `none`, never an exception.
    """
    if header is not None:
        tag = _validate_tag(header)
        return (tag, "client") if tag is not None else (None, "invalid")
    if metadata is None:
        return (None, "none")
    if not isinstance(metadata, dict):
        return (None, "invalid")
    if "workload" not in metadata:
        return (None, "none")
    value = metadata["workload"]
    if not isinstance(value, str):
        return (None, "invalid")
    tag = _validate_tag(value)
    return (tag, "client") if tag is not None else (None, "invalid")


def prompt_bucket(tokens: int) -> str:
    """One of `BUCKETS` for `tokens`. Upper bound exclusive: exactly 1000
    tokens is `"1k-4k"`, not `"<1k"`."""
    if tokens < 1_000:
        return "<1k"
    if tokens < 4_000:
        return "1k-4k"
    if tokens < 16_000:
        return "4k-16k"
    if tokens < 64_000:
        return "16k-64k"
    return ">64k"


def outcome_context(ctx, *, max_tokens: int | None = None) -> dict:
    """The content-free policy features of an `ExecutionPolicyContext`,
    shared verbatim by `build_ok_outcome` and `build_failure_outcome` since
    both are known the moment the gate has run, win or lose downstream.
    `detected_entity_types` is sorted and de-duplicated -- TYPES only, never
    the entity values `ctx.detected_entities` never carries either."""
    return {
        "sensitivity": ctx.sensitivity,
        "risk_class": ctx.risk_class,
        "onprem_only": ctx.onprem_only,
        "detected_entity_types": sorted(set(ctx.detected_entities)),
        "app_policy_version": ctx.app_policy_version,
        # B55: the client's model pin (None = unpinned; the gate already maps
        # `model: "auto"` to None) and its output-token cap (None = no cap;
        # the caller reads it with `app/routing/router.py::client_output_cap`).
        # A model slug and an int -- content-free.
        "requested_model": ctx.requested_model,
        "max_tokens": max_tokens,
    }


# --- trace parsing (build_ok_outcome helpers) --------------------------

def outcome_trace(records, vrec) -> list[dict]:
    """The content-free trace a `verify_failed` outcome row is built from:
    each gateway DecisionRecord and the VerificationRecord, dumped WITHOUT
    any free-text `reason` (the record's own and every rejected candidate's).
    What remains is ids, enums, numbers and scoring provenance -- enough for
    `trace_features` to read, nothing a leak could ride on. Takes the
    pydantic records (duck-typed on `model_dump`) so the stash never holds a
    reason string even transiently."""
    dropped = {"reason": True, "rejected": {"__all__": {"reason"}}}
    return ([r.model_dump(exclude=dropped) for r in records]
            + [vrec.model_dump(exclude={"reason"})])


def _router_hops(trace: list[dict]) -> list[dict]:
    return [e for e in trace if e.get("step") == "router"]


def _is_failed_decision_hop(e: dict) -> bool:
    """A failed gateway hop: the router picked a candidate (its `scoring` is
    real) but the backend call failed, so nothing was served."""
    return (e.get("step") == "gateway" and e.get("chosen") is None
            and bool(e.get("scoring")))


def _rejected_rows(rejected) -> list[dict]:
    """Normalize any iterable of RejectedCandidate-shaped mappings down to
    `{name, code}` only -- never `reason`, which is free text an operator or
    a client can put anything in (CLAUDE.md rule 2 applies to this table
    too, per the module docstring)."""
    return [{"name": r["name"], "code": r.get("code")} for r in rejected]


def _attempts(trace: list[dict]) -> list[str]:
    """Every model tried, in hop order: a failed gateway hop's own first-
    scored candidate name, interleaved with each router hop's served
    `chosen`, sorted by `hop_index` (defaulting to 0 for a hand-built
    fixture that omits it -- real trace entries always carry one)."""
    ordered: list[tuple[int, str]] = []
    for e in trace:
        if e.get("step") == "gateway" and e.get("chosen") is None and e.get("scoring"):
            candidates = (e.get("scoring") or {}).get("candidates") or []
            if candidates:
                ordered.append((e.get("hop_index", 0), candidates[0]["name"]))
        elif e.get("step") == "router" and e.get("chosen") is not None:
            ordered.append((e.get("hop_index", 0), e["chosen"]))
    ordered.sort(key=lambda pair: pair[0])
    return [name for _, name in ordered]


def _verify_mode(entry: dict | None) -> str | None:
    """Map a `step == "verify"` trace entry to one of `VERIFY_MODES`, or
    `None` when no verify entry exists at all (unreachable on a real `ok`
    response, defensive for a hand-built fixture).

    Reason prefixes are checked BEFORE `required`. On the real streaming
    paths (`app/pipeline.py` `run_stream`, ~line 1166) `onprem_only`/
    `require_verification` requests are floored out before any unverified
    stream starts, so the record `run_stream` builds always carries
    `required=False` (see `app/pipeline.py` ~1286 and
    `app/api/routes.py::_finalize_stream`'s `verify_reason`/`verify_performed`
    overwrite, ~1275-1278) -- `stream_unverified`'s
    `reason="waived: stream_unverified"` and `incremental_verify`'s
    `reason="incremental"` are BOTH `required=False` in every real row. If
    `required` were checked first, every real `waived_stream`/`incremental`
    outcome would be misrecorded `not_required`, losing the distinction the
    column exists to carry."""
    if entry is None:
        return None
    reason = entry.get("reason") or ""
    if reason.startswith("waived"):
        return "waived_stream"
    if reason == "incremental":
        return "incremental"
    if not entry.get("required"):
        return "not_required"
    if not entry.get("passed"):
        return "failed"
    return "verified"


def build_ok_outcome(resp: dict, *, request_id: str, application_id: str,
                      outcome_ctx: dict, routing_policy_version: str,
                      workload: str | None, workload_source: str) -> dict:
    """A `routing_outcomes` row for a request that was served (`status ==
    "ok"`), built entirely from `resp` (a finished `Pipeline.run` response
    dict) and the caller-supplied ids/versions/workload tag. Reads only the
    content-free keys documented on the module (and the task-2 brief):
    `trace_id`, `usage.{prompt,completion}_tokens`, `cost.total_usd`,
    `policy.endpoint_registry_version`, and the trace (through
    `trace_features`, which owns the hop semantics). Never reads
    `resp["choices"]`, `resp["raw_completion"]`, `resp["sanitized_prompt"]`,
    or any DecisionRecord `reason` field.

    `complexity` is read off `outcome_ctx.get("complexity")` (`None` when
    absent): it is computed inside `Pipeline.run` (`comp.tokens_after /
    2000`) but never placed on `resp`, so it is not a content-free key this
    function can read off `resp` itself -- a later task threads it into
    `outcome_ctx` instead (e.g. by adding it alongside `outcome_context`'s
    other fields) and it flows through here unchanged.

    `eligible` falls back to `[chosen]` only when a chosen model exists
    (i.e. some router hop actually served one); with no scoring AND no
    chosen model it is `[]`, never `[None]`.
    """
    usage = resp.get("usage") or {}
    cost = resp.get("cost") or {}
    policy = resp.get("policy") or {}
    trace = resp.get("trace") or []
    prompt_tokens = usage.get("prompt_tokens", 0)

    row = {
        "request_id": request_id,
        "application_id": application_id,
        "trace_id": resp.get("trace_id"),
        "status": "ok",
        "app_policy_version": outcome_ctx.get("app_policy_version"),
        "endpoint_registry_version": policy.get("endpoint_registry_version"),
        "routing_policy_version": routing_policy_version,
        "workload": workload,
        "workload_source": workload_source,
        "complexity": outcome_ctx.get("complexity"),
        "prompt_tokens_bucket": prompt_bucket(prompt_tokens),
        **_policy_features(outcome_ctx),
        "cost_usd": cost.get("total_usd", 0.0),
        "latency_ms": sum(e.get("latency_ms", 0.0) for e in trace),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": usage.get("completion_tokens", 0),
        # P3b: only a `no_eligible` row has a conflicting core.
        "conflicting_core": None,
    }
    row.update(trace_features(trace))
    return row


def _policy_features(outcome_ctx: dict) -> dict:
    """The request features both builders copy off `outcome_ctx`, so a
    served and a rejected row can never disagree about them. `routing_tokens`
    (B60) is the token count the router was given -- a count, never text --
    and is `None` when the request never reached compression."""
    return {
        "sensitivity": outcome_ctx.get("sensitivity"),
        "risk_class": outcome_ctx.get("risk_class"),
        "onprem_only": outcome_ctx.get("onprem_only"),
        "detected_entity_types": list(outcome_ctx.get("detected_entity_types") or []),
        "requested_model": outcome_ctx.get("requested_model"),
        "max_tokens": outcome_ctx.get("max_tokens"),
        "routing_tokens": outcome_ctx.get("routing_tokens"),
    }


def trace_features(trace: list[dict]) -> dict:
    """The routing and verification columns of an outcome row, read from a
    content-free trace (a list of DecisionRecord / VerificationRecord /
    CacheRecord dumps). The ONE place a trace is read -- `build_ok_outcome`
    and a `verify_failed` `build_failure_outcome` both use it. Never reads a
    `reason` except the verify record's, and only to match the pipeline's own
    fixed `waived*`/`incremental` markers (see `_verify_mode`).

    Hop semantics (spec §13, final review ruling #4):

    - `eligible`, `scoring`, `tier_target`, `rejected` describe the request's
      FIRST routing decision -- the first trace entry that is a router hop or
      a failed gateway hop carrying `scoring` (the decision that picked the
      first attempt). After a failover that is the failed hop; after a
      confidence escalation it is the first, unserved router hop.
      `tier_target` is that hop's own (a failed gateway hop records it too);
      it never falls back to a later router hop.
    - `circuit_bypass` is True when ANY decision hop -- router or failed
      gateway -- bypassed an open circuit.
    - `chosen` is the SERVED model: the last router hop with a `chosen`.
    - `attempts` is every model tried, in hop order.
    - `cache_hit` is True when a semantic-cache entry served the request (no
      router ran, so `chosen` stays null)."""
    router_hops = _router_hops(trace)
    chosen_hops = [h for h in router_hops if h.get("chosen") is not None]
    chosen = chosen_hops[-1]["chosen"] if chosen_hops else None
    first = next((e for e in trace
                  if e.get("step") == "router" or _is_failed_decision_hop(e)), None)
    scoring = first.get("scoring") if first else None
    if scoring:
        eligible = [c["name"] for c in scoring.get("candidates", [])]
    else:
        eligible = [chosen] if chosen is not None else []
    # The first decision's OWN tier target -- a failed gateway hop records it
    # too. Never borrowed from a later router hop, whose target may differ
    # after an escalation; an older trace without it reads null.
    tier_target = first.get("tier_target") if first else None
    decision_hops = [e for e in trace
                     if e.get("step") == "router" or _is_failed_decision_hop(e)]
    verify_entry = next((e for e in trace if e.get("step") == "verify"), None)
    return {
        "tier_target": tier_target,
        "eligible": eligible,
        "rejected": _rejected_rows(first.get("rejected") or []) if first else [],
        "attempts": _attempts(trace),
        "chosen": chosen,
        "scoring": scoring,
        "circuit_bypass": any(h.get("circuit_bypass") for h in decision_hops),
        "cache_hit": any(e.get("step") == "cache" and e.get("hit") for e in trace),
        "verify_required": verify_entry.get("required") if verify_entry else None,
        "verify_performed": verify_entry.get("performed") if verify_entry else None,
        "verify_passed": verify_entry.get("passed") if verify_entry else None,
        "verify_mode": _verify_mode(verify_entry),
    }


def build_failure_outcome(*, status: str, request_id: str, application_id: str,
                           outcome_ctx: dict, routing_policy_version: str,
                           endpoint_registry_version: str | None,
                           workload: str | None, workload_source: str,
                           attempts: tuple[str, ...] = (),
                           rejected: tuple = (),
                           latency_ms: float = 0.0,
                           trace: list[dict] | None = None,
                           prompt_tokens: int = 0,
                           completion_tokens: int = 0,
                           cost_usd: float = 0.0,
                           conflicting_core: dict | None = None) -> dict:
    """A `routing_outcomes` row for a request that never reached (or never
    completed) a served response. Fills what it is given -- `status`, the
    ids/versions, the policy-context features (known once the gate ran,
    regardless of what happened after), and whatever `attempts`/`rejected`
    the caller collected before the failure -- and leaves everything that
    presumes a served response (`chosen`, `scoring`, `eligible`,
    `prompt_tokens`/`completion_tokens`, `cost_usd`, the `verify_*` fields,
    `tier_target`, `prompt_tokens_bucket`) null or empty. `complexity` and
    `routing_tokens` (B60) are read off `outcome_ctx` like `build_ok_outcome`
    -- known the moment the gate/compression ran, independent of whether the
    request went on to fail. `prompt_tokens` stays 0 here: it is the
    backend's billed count, and no backend answered. `rejected` is normalized through the same
    `{name, code}`-only rule `build_ok_outcome` uses, regardless of what
    shape the caller's entries carry.

    `trace` (a `verify_failed` row only): the content-free trace the
    pipeline stashed before refusing a response step 9 failed -- a model WAS
    served, so the routing and verify columns are read from it through
    `trace_features`, exactly as for an `ok` row, and `prompt_tokens`/
    `completion_tokens`/`cost_usd` are the served attempt's real figures
    (`prompt_tokens_bucket` follows). `attempts`/`rejected` are then ignored.

    `conflicting_core` (a `no_eligible` row, P3b): the minimal conflicting
    core in `ConflictingCore.to_record()` shape -- requirement ids and their
    content-free details, endpoint state, and a count. Stored as given; the
    caller passes `None` for every other status.
    """
    row = {
        "request_id": request_id,
        "application_id": application_id,
        "trace_id": None,
        "status": status,
        "app_policy_version": outcome_ctx.get("app_policy_version"),
        "endpoint_registry_version": endpoint_registry_version,
        "routing_policy_version": routing_policy_version,
        "workload": workload,
        "workload_source": workload_source,
        "tier_target": None,
        "complexity": outcome_ctx.get("complexity"),
        "prompt_tokens_bucket": None,
        **_policy_features(outcome_ctx),
        "eligible": [],
        "rejected": _rejected_rows(rejected),
        "attempts": list(attempts),
        "chosen": None,
        "scoring": None,
        "circuit_bypass": False,
        "cache_hit": False,
        "cost_usd": 0.0,
        "latency_ms": latency_ms,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "verify_required": None,
        "verify_performed": None,
        "verify_passed": None,
        "verify_mode": None,
        "conflicting_core": conflicting_core,
    }
    if trace is not None:
        row.update(trace_features(trace))
        row.update({
            "cost_usd": cost_usd,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "prompt_tokens_bucket": prompt_bucket(prompt_tokens),
        })
    return row
