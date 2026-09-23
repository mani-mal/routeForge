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

import re

# --- constants --------------------------------------------------------

STATUSES = ("ok", "backend_failed", "verify_failed", "no_eligible", "context_overflow")
VERIFY_MODES = ("not_required", "verified", "failed", "waived_stream", "incremental")
BUCKETS = ("<1k", "1k-4k", "4k-16k", "16k-64k", ">64k")

# Client-declared workload tag: lower-cased, 1-64 chars, alnum plus
# `._-`, first char alnum. A 65-char (or longer) tag, or anything with
# whitespace/markup, fails to match and is dropped rather than truncated.
_WORKLOAD_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


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


def outcome_context(ctx) -> dict:
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
    }


# --- trace parsing (build_ok_outcome helpers) --------------------------

def _router_hops(trace: list[dict]) -> list[dict]:
    return [e for e in trace if e.get("step") == "router"]


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
    response, defensive for a hand-built fixture)."""
    if entry is None:
        return None
    if not entry.get("required"):
        return "not_required"
    reason = entry.get("reason") or ""
    if reason.startswith("waived"):
        return "waived_stream"
    if reason == "incremental":
        return "incremental"
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
    `policy.endpoint_registry_version`, and the `step == "router"` /
    `step == "gateway"` / `step == "verify"` trace entries. Never reads
    `resp["choices"]`, `resp["raw_completion"]`, `resp["sanitized_prompt"]`,
    or any `reason` field.

    `complexity` is left `None`: it is computed inside `Pipeline.run`
    (`comp.tokens_after / 2000`) but never placed on `resp`, so it is not a
    content-free key this function can read -- a later task may thread it
    through separately.
    """
    trace = resp.get("trace") or []
    usage = resp.get("usage") or {}
    cost = resp.get("cost") or {}
    policy = resp.get("policy") or {}

    router_hops = _router_hops(trace)
    chosen_hops = [h for h in router_hops if h.get("chosen") is not None]
    chosen = chosen_hops[-1]["chosen"] if chosen_hops else None
    first_router = router_hops[0] if router_hops else None
    scoring = first_router.get("scoring") if first_router else None
    if scoring:
        eligible = [c["name"] for c in scoring.get("candidates", [])]
    else:
        eligible = [chosen] if chosen is not None else []
    tier_target = first_router.get("tier_target") if first_router else None
    circuit_bypass = any(h.get("circuit_bypass") for h in router_hops)
    rejected = _rejected_rows(first_router["rejected"]) if first_router else []
    attempts = _attempts(trace)

    verify_entry = next((e for e in trace if e.get("step") == "verify"), None)
    prompt_tokens = usage.get("prompt_tokens", 0)

    return {
        "request_id": request_id,
        "application_id": application_id,
        "trace_id": resp.get("trace_id"),
        "status": "ok",
        "app_policy_version": outcome_ctx.get("app_policy_version"),
        "endpoint_registry_version": policy.get("endpoint_registry_version"),
        "routing_policy_version": routing_policy_version,
        "workload": workload,
        "workload_source": workload_source,
        "tier_target": tier_target,
        "complexity": None,
        "prompt_tokens_bucket": prompt_bucket(prompt_tokens),
        "sensitivity": outcome_ctx.get("sensitivity"),
        "risk_class": outcome_ctx.get("risk_class"),
        "onprem_only": outcome_ctx.get("onprem_only"),
        "detected_entity_types": list(outcome_ctx.get("detected_entity_types") or []),
        "eligible": eligible,
        "rejected": rejected,
        "attempts": attempts,
        "chosen": chosen,
        "scoring": scoring,
        "circuit_bypass": circuit_bypass,
        "cost_usd": cost.get("total_usd", 0.0),
        "latency_ms": sum(e.get("latency_ms", 0.0) for e in trace),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": usage.get("completion_tokens", 0),
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
                           latency_ms: float = 0.0) -> dict:
    """A `routing_outcomes` row for a request that never reached (or never
    completed) a served response. Fills what it is given -- `status`, the
    ids/versions, the policy-context features (known once the gate ran,
    regardless of what happened after), and whatever `attempts`/`rejected`
    the caller collected before the failure -- and leaves everything that
    presumes a served response (`chosen`, `scoring`, `eligible`,
    `prompt_tokens`/`completion_tokens`, `cost_usd`, the `verify_*` fields,
    `tier_target`, `complexity`, `prompt_tokens_bucket`) null or empty.
    `rejected` is normalized through the same `{name, code}`-only rule
    `build_ok_outcome` uses, regardless of what shape the caller's entries
    carry.
    """
    return {
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
        "complexity": None,
        "prompt_tokens_bucket": None,
        "sensitivity": outcome_ctx.get("sensitivity"),
        "risk_class": outcome_ctx.get("risk_class"),
        "onprem_only": outcome_ctx.get("onprem_only"),
        "detected_entity_types": list(outcome_ctx.get("detected_entity_types") or []),
        "eligible": [],
        "rejected": _rejected_rows(rejected),
        "attempts": list(attempts),
        "chosen": None,
        "scoring": None,
        "circuit_bypass": False,
        "cost_usd": 0.0,
        "latency_ms": latency_ms,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "verify_required": None,
        "verify_performed": None,
        "verify_passed": None,
        "verify_mode": None,
    }
