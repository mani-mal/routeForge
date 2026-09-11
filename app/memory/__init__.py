"""Persistent memory (Feature 5) -- step-5-class orchestration wiring
`DeterministicMemoryStore` (app/memory/store.py) into the serving pipeline.
CCR-class residency posture (CLAUDE.md rule 6): local-only, TTL-bounded,
tenant-partitioned, sanitized-only, never exported. See
docs/architecture/specs/persistent-memory-residency-spec.md for the binding
guarantees (R1-R7) this module enforces.

Mirrors app/compression/__init__.py's shape: a `_bypass` idiom plus two
call-site functions (`recall_memory` / `write_memory`) that `app/pipeline.py`
calls directly. Fails SAFE everywhere -- memory is an optimization, not a
gate (CLAUDE.md rule 5 is about the POLICY gate; a memory error must never
crash or reject an otherwise-servable request), so every store call is
wrapped and swallowed on error.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.config.schema import MemoryCfg
from app.gateway.backends.base import count_tokens
from app.memory.store import MemoryStore
from app.models import ExecutionPolicyContext, MemoryRecord, Message

log = logging.getLogger("serving.memory")

_MAX_SUMMARY_CHARS = 2000


@dataclass
class RecallResult:
    messages: list[Message]      # prior-context Messages to PREPEND to san_messages
    record: MemoryRecord | None  # None when memory is off or bypassed -- no trace entry


def _bypass(cfg: MemoryCfg, ctx: ExecutionPolicyContext, tokens: int) -> str | None:
    """Mirrors app/compression/__init__.py::_bypass's shape and reasons.

    Deliberately does NOT bypass on `ctx.onprem_only` (R3): the store has no
    cloud path at all, so an onprem_only request uses memory exactly like any
    other request -- residency is enforced at the recall call site (never
    `to_cloud=True`), not by refusing memory to onprem_only tenants.

    `cfg.bypass_on.user_prompt` is intentionally IGNORED here: it protects
    compression's "live prompt" from being compressed, a concept that has no
    memory analogue (there is no live prompt to protect from a recall/write).
    `MemoryCfg.bypass_on` reuses `BypassCfg`'s shape only for parity with
    compression's one bypass shape (see MemoryCfg's docstring) -- this
    resolves Task 1's open note.
    """
    if not cfg.enabled:
        return "disabled"
    if cfg.bypass_on.high_risk and ctx.risk_class == "high":
        return "high_risk"
    if cfg.bypass_on.restricted_sensitivity and ctx.sensitivity == "restricted":
        return "restricted_sensitivity"
    if tokens < cfg.min_tokens:
        return "below_min_tokens"
    return None


def recall_memory(store: MemoryStore | None, cfg: MemoryCfg, ctx: ExecutionPolicyContext,
                   san_messages: list[Message], *, tenant_id: str, application_id: str,
                   session_id: str) -> RecallResult:
    """READ hook. Call AFTER `san_messages` is built (post-gate, sanitized)
    and BEFORE `compress()` -- so recalled tokens are compressed and priced
    like any other request tokens, never a separate untracked token path.

    Returns prior-context `Message`s (most-recent-first, as the store already
    orders them) for the caller to PREPEND to `san_messages`, plus a
    counts-only `MemoryRecord` for the trace. `([], None)` when memory is off
    or bypassed -- deliberately no trace entry at all in that case (nothing
    to report), distinct from "recalled zero items" which still returns a
    record with `recalled_count=0`.
    """
    if store is None:
        return RecallResult([], None)
    tokens_before = sum(count_tokens(m.content) for m in san_messages)
    if _bypass(cfg, ctx, tokens_before) is not None:
        return RecallResult([], None)

    t0 = time.monotonic()
    try:
        # NEVER to_cloud=True (R3) -- recall is always local, unconditionally,
        # regardless of ctx.onprem_only. The store itself has no cloud path;
        # this call-site discipline is the enforcement point the spec names.
        recalled = store.recall(tenant_id=tenant_id, application_id=application_id,
                                session_id=session_id, max_items=cfg.max_recall,
                                to_cloud=False)
    except Exception:
        log.exception("memory recall failed; skipping (fail-safe, not a gate)")
        return RecallResult([], None)
    latency_ms = (time.monotonic() - t0) * 1000

    tokens_recalled = sum(count_tokens(t) for t in recalled)
    rec = MemoryRecord(recalled_count=len(recalled), tokens_recalled=tokens_recalled,
                       recall_stayed_local=True, retrievals_to_cloud=0,
                       latency_ms=latency_ms)
    if not recalled:
        return RecallResult([], rec)
    prior = [Message(role="user", content=text) for text in recalled]
    return RecallResult(prior, rec)


def write_memory(store: MemoryStore | None, cfg: MemoryCfg, ctx: ExecutionPolicyContext,
                  sanitized: str, verified_text: str, *, tenant_id: str,
                  application_id: str, session_id: str) -> None:
    """WRITE hook. Call ONLY after step-9 `verify()` has passed -- a
    rejected/failed request must write nothing.

    R1 (sanitized-only) enforcement point: `sanitized` is the gate's redacted
    input and `verified_text` is the post-verify output text -- this is the
    ONLY material the stored summary is ever built from. NEVER pass
    `result.text` (the pre-verify raw generation), `raw_completion`, or the
    raw request here; both arguments the caller supplies are already
    sanitized by construction at their respective call sites in
    app/pipeline.py.
    """
    if store is None:
        return
    tokens = count_tokens(sanitized)
    if _bypass(cfg, ctx, tokens) is not None:
        return
    summary = _summarize(sanitized, verified_text)
    try:
        store.write(summary, tenant_id=tenant_id, application_id=application_id,
                   session_id=session_id)
    except Exception:
        log.exception("memory write failed; skipping (fail-safe, not a gate)")


def _summarize(sanitized: str, verified_text: str) -> str:
    """Boring, bounded concatenation of two already-sanitized strings (R1).
    Capped so a single pathologically long exchange can't grow one store
    entry unbounded -- R5's TTL bounds retention over TIME, this bounds one
    entry's SIZE."""
    text = f"user: {sanitized}\nassistant: {verified_text}"
    return text[:_MAX_SUMMARY_CHARS]
