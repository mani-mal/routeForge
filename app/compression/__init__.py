"""Step 5 — Conditional Context Optimization (Headroom). Runs AFTER the gate
(sees only sanitized content) and BEFORE the router (which prices tokens_after).
Default compressor is offline/deterministic; real headroom-ai is opt-in behind
the [compression] extra. Fails OPEN — compression is an optimization, so a
compressor error falls back to uncompressed and the request still succeeds."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.compression.ccr import CCRStore
from app.compression.compressors import Compressor, DeterministicCompressor
from app.config.schema import ContextOptimizationCfg
from app.gateway.backends.base import count_tokens
from app.models import CompressionRecord, ExecutionPolicyContext, Message

log = logging.getLogger("serving.compression")


@dataclass
class CompressionResult:
    messages: list[Message]
    tokens_before: int
    tokens_after: int
    record: CompressionRecord


def select_compressor(cfg: ContextOptimizationCfg) -> Compressor:
    if cfg.mode == "library":
        try:
            from app.compression.headroom import HeadroomCompressor
            return HeadroomCompressor()
        except Exception:  # extra not installed / model unavailable → offline default
            return DeterministicCompressor()
    return DeterministicCompressor()


def _effective_enabled(cfg, ctx) -> bool:
    """Per-app override wins over the live global toggle. None on the context
    means 'inherit', so we fall back to the co_cfg the caller passed in — which
    is the LIVE `app.state.co_cfg` on both the direct and per-hop paths, so the
    inherit case still tracks a runtime POST /api/controls flip."""
    override = getattr(ctx, "compression_override", None)
    return cfg.enabled if override is None else override


def _bypass(cfg, ctx, tokens_before) -> str | None:
    if not _effective_enabled(cfg, ctx):
        return "disabled"
    if ctx.content_integrity == "exact":
        # Data-fidelity control (design P1a §8), not a privacy one: an app
        # whose every byte is load-bearing (a CAD table, a contract, code)
        # gets NO transformation at all, not even a lossless-looking one.
        # Checked before every other bypass so it can never be shadowed by
        # e.g. `min_tokens` deciding first for the wrong reason, and it is
        # absolute -- unlike `compression_override`, no per-app "force on"
        # setting may override it.
        return "content_integrity_exact"
    if cfg.bypass_on.high_risk and ctx.risk_class == "high":
        return "high_risk"
    if cfg.bypass_on.restricted_sensitivity and ctx.sensitivity == "restricted":
        return "restricted_sensitivity"
    if tokens_before < cfg.min_tokens:
        return "below_min_tokens"
    return None


def compress(messages: list[Message], ctx: ExecutionPolicyContext,
             cfg: ContextOptimizationCfg, *,
             ccr: CCRStore | None = None) -> CompressionResult:
    t0 = time.monotonic()
    tokens_before = sum(count_tokens(m.content) for m in messages)

    reason = _bypass(cfg, ctx, tokens_before)
    if reason is not None:
        rec = CompressionRecord(enabled=_effective_enabled(cfg, ctx), applied=False,
                                bypass_reason=reason, tokens_before=tokens_before,
                                tokens_after=tokens_before, reduction_pct=0.0,
                                latency_ms=(time.monotonic() - t0) * 1000)
        return CompressionResult(messages, tokens_before, tokens_before, rec)

    # Compress history/tool output; never the live user prompt (last message)
    # when user_prompt bypass is set.
    protect_last = cfg.bypass_on.user_prompt
    compressor = select_compressor(cfg)
    ccr_stored = False
    try:
        out: list[Message] = []
        for i, m in enumerate(messages):
            is_last = i == len(messages) - 1
            if protect_last and is_last:
                out.append(m)
                continue
            if m.content is None:  # e.g. an assistant tool_calls turn — nothing to compress
                out.append(m)
                continue
            compressed = compressor.compress_text(m.content)
            if ccr is not None and cfg.ccr.enabled and compressed != m.content:
                ccr.store(m.content)  # local original, TTL-bounded, never telemetry
                ccr_stored = True
            out.append(Message(role=m.role, content=compressed))
    except Exception:
        log.exception("compression failed; falling back to uncompressed")
        rec = CompressionRecord(enabled=_effective_enabled(cfg, ctx), applied=False,
                                bypass_reason="compressor_error",
                                tokens_before=tokens_before, tokens_after=tokens_before,
                                compressor=None, latency_ms=(time.monotonic() - t0) * 1000)
        return CompressionResult(messages, tokens_before, tokens_before, rec)

    tokens_after = sum(count_tokens(m.content) for m in out)
    reduction = 0.0 if tokens_before == 0 else max(
        0.0, (tokens_before - tokens_after) / tokens_before * 100)
    applied = tokens_after < tokens_before
    rec = CompressionRecord(
        enabled=_effective_enabled(cfg, ctx), applied=applied,
        bypass_reason=None if applied else "no_reduction",
        tokens_before=tokens_before, tokens_after=tokens_after,
        reduction_pct=round(reduction, 2),
        compressor=compressor.name if applied else None,
        ccr_stored=ccr_stored, ccr_retrievals=0, ccr_retrievals_to_cloud=0,
        latency_ms=(time.monotonic() - t0) * 1000)
    return CompressionResult(out, tokens_before, tokens_after, rec)
